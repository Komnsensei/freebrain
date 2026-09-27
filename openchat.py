#!/usr/bin/env python3
"""
openchat.py — the Free Brain openchat server (stdlib only).

One command to chat with the brain, grounded and auditable, and to expose the
index as a plugin for any host.

  python3 openchat.py                 # build index if missing, serve on :8080
  python3 openchat.py --port 3000     # custom port
  python3 openchat.py --build         # (re)build index then exit
  PORT=8080 RAG_CORPUS=docs:src python3 openchat.py  # custom corpus

What you get:
  GET  /              openchat UI (single-page, streaming SSE, grounded by default)
  POST /api/chat      {messages:[{role,content}], stream, use_rag, temperature}
  POST /api/search    {query, k}     -> hybrid BM25+dense hits (plugin indexing)
  POST /api/ask       {question, k, generator} -> grounded answer + audit
  POST /api/build     {}             -> rebuild index
  GET  /api/health    -> {ok, index, providers, version}
  GET  /api/index     -> index status
  POST /v1/chat/completions  OpenAI-compatible (so plugins that expect OpenAI work)
  GET  /.well-known/ai-plugin.json   ChatGPT plugin manifest (CORS)
  GET  /openapi.json                 plugin OpenAPI spec
  GET  /api/contract                 builderbro-rag skill contract

Design: stdlib http.server only, no deps. ThreadingHTTPServer so /api/search
stays snappy while a long /api/chat streams. Every chat can run grounded:
retrieval -> confidence gate -> brain cascade -> audit. If the gate fires or
no brain is reachable, the answer degrades to extractive rather than
hallucinating. Every answer carries its audit (citations, fidelity,
query_support, verdict); a refusal is a first-class, machine-checked outcome.

Smart = grounded, audited, with cascade failover and loop-guard attribution.
Valid = citations point at retrieved chunks only; refusal is measurable.
Plugin = every retrieval surface is also an HTTP tool with OpenAPI + manifest.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Project imports — stdlib-only, offline-safe until a model is asked for.
import rag_core
import rag_eval
import agent_runtime
import brain_cascade
import loop_guard

VERSION = "1.0.0"
DEFAULT_PORT = int(os.environ.get("PORT") or 8080)
DEFAULT_HOST = os.environ.get("HOST") or "0.0.0.0"

# ── Plugin manifests (also importable as JSON) ─────────────────────────────

PLUGIN_MANIFEST = {
    "schema_version": "v1",
    "name_for_human": "Free Brain RAG",
    "name_for_model": "free_brain_rag",
    "description_for_human": "Grounded retrieval over the Free Brain corpus — search, ask with citations, and build the index. Answers are audited; insufficient context returns INSUFFICIENT_CONTEXT instead of guessing.",
    "description_for_model": "Search the Free Brain corpus (FREE-BRAIN.md, AGENT-INTEGRITY.md, QIH, residences), or ask a question and get a grounded answer with citations and audit metadata. Use `search` to retrieve source chunks with spans, `ask` for a cited answer (refuses when context is insufficient). `build` rebuilds the index. Every answer carries `citation_fidelity`, `query_support`, and a hard gate `verdict`.",
    "auth": {"type": "none"},
    "api": {"type": "openapi", "url": "/openapi.json", "is_user_authenticated": False},
    "logo_url": "/logo.png",
    "contact_email": "freebrain@freebuff.com",
    "legal_info_url": "https://freebuff.com/legal",
}

def openapi_spec(host: str):
    base = f"http://{host}" if "://" not in host else host
    return {
        "openapi": "3.0.1",
        "info": {
            "title": "Free Brain — OpenChat + Indexing Plugin",
            "description": "Grounded chat and lexical indexing for the Free Brain corpus. Every factual sentence is citable; `ask` re-derives grounding from retrieved chunks and refuses with INSUFFICIENT_CONTEXT when support is below threshold.",
            "version": VERSION,
        },
        "servers": [{"url": base}],
        "paths": {
            "/api/search": {
                "post": {
                    "operationId": "search",
                    "summary": "Lexical+dense retrieval",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 6}}, "required": ["query"]}}}},
                    "responses": {"200": {"description": "hits with citation, span, score, channels"}},
                }
            },
            "/api/ask": {
                "post": {
                    "operationId": "ask",
                    "summary": "Ground retrieval + gate + generate + audit",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"question": {"type": "string"}, "k": {"type": "integer"}, "generator": {"type": "string", "enum": ["live", "extractive"], "default": "live"}}, "required": ["question"]}}}},
                    "responses": {"200": {"description": "answer, citations, citation_fidelity, query_support, verdict, refused"}},
                }
            },
            "/api/chat": {
                "post": {
                    "operationId": "chat",
                    "summary": "OpenChat — brain cascade with optional grounded context",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"messages": {"type": "array", "items": {"type": "object", "properties": {"role": {"type": "string"}, "content": {"type": "string"}}}}, "stream": {"type": "boolean", "default": False}, "use_rag": {"type": "boolean", "default": True}, "temperature": {"type": "number"}, "max_tokens": {"type": "integer"}}, "required": ["messages"]}}}},
                    "responses": {"200": {"description": "chat/completion with audit when use_rag=true"}},
                }
            },
            "/api/build": {
                "post": {
                    "operationId": "buildIndex",
                    "summary": "Rebuild the hybrid index from the corpus",
                    "responses": {"200": {"description": "chunks, files, config"}},
                }
            },
            "/api/health": {
                "get": {
                    "operationId": "health",
                    "summary": "Server + index + cascade health",
                    "responses": {"200": {"description": "ok, index_chunks, cascade chain"}},
                }
            },
            "/v1/chat/completions": {
                "post": {
                    "operationId": "chatCompletions",
                    "summary": "OpenAI-compatible chat (proxies to the cascade)",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "OpenAI shape, stream via SSE"}},
                }
            },
        },
    }

# ── Index cache (thread-safe, stdlib lock) ────────────────────────────────

_index_lock = threading.Lock()
_cached_index = None
_cached_at = 0.0

def get_index(force_rebuild=False):
    global _cached_index, _cached_at
    with _index_lock:
        if not force_rebuild and _cached_index is not None:
            return _cached_index
        idx = rag_core.RagIndex.load()
        if idx is None or force_rebuild:
            idx = rag_core.build_index(save=True)
        _cached_index = idx
        _cached_at = time.time()
        return idx

def index_status():
    try:
        idx = get_index()
        meta = idx.corpus_meta or {}
        return {
            "ok": True,
            "built": getattr(idx, "_built", None),
            "chunks": len(idx.chunks),
            "files": len(meta.get("files", [])),
            "bytes": meta.get("total_bytes", 0),
            "skipped": len(meta.get("skipped", [])),
            "config": {k: idx.config[k] for k in ("fusion", "chunk_budget_words", "chunk_overlap_words", "dense_weight", "min_query_support", "top_k") if k in idx.config},
            "cached_at": datetime.datetime.fromtimestamp(_cached_at, datetime.timezone.utc).isoformat() if _cached_at else None,
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "reason": str(e)}

# ── Grounded generation helper ────────────────────────────────────────────

def _last_user_text(messages):
    for m in reversed(messages or []):
        if m.get("role") == "user" and (m.get("content") or "").strip():
            return m["content"]
    return ""

def grounded_chat(messages, config, use_rag=True, temperature=None, max_tokens=None):
    """
    Returns (reply_dict, audit_or_none, hits_or_none, support_or_none).
    When use_rag is true, retrieves on last user message, gates on
    query_support, injects numbered sources, generates via cascade, then audits.
    Falls back to extractive generator when cascade is unavailable.
    """
    if not use_rag:
        raw = agent_runtime.chat_stream(config, messages, temperature=temperature, max_tokens=max_tokens) if config else {"content": "", "elapsed": 0, "tokens": 0}
        return raw, None, None, None

    query = _last_user_text(messages)
    idx = get_index()
    hits = idx.search(query, k=idx.config.get("top_k", 6)) if query else []
    support = rag_core.query_support(query, hits, index=idx, config=idx.config) if query else 0.0
    thr = idx.config.get("min_query_support", 0.0)
    if thr and support < thr and query:
        # Pre-generation refusal — do not spend a model call hallucinating.
        audit = rag_core.refusal_audit(query, hits, support, thr, "insufficient_query_support:%.3f<%.2f" % (support, thr))
        audit["query_support"] = round(support, 4)
        return {"content": rag_core.REFUSAL_TOKEN, "elapsed": 0.0, "tokens": 0, "refused": True, "provider": "gate", "early_stop": False}, audit, hits, support

    context = rag_core.build_context(hits)
    grounded_messages = list(messages)
    # Inject as a system-adjacent user block so the model sees numbered sources.
    if context:
        grounded_messages = [
            {"role": "system", "content": rag_core.RAG_SYSTEM_PROMPT},
        ] + grounded_messages[:-1] + [
            {"role": "user", "content": "SOURCES:\n%s\n\nQUESTION: %s" % (context, query)},
        ] if grounded_messages and grounded_messages[-1].get("role") == "user" else grounded_messages

    # Try live brain, fall back to extractive when no provider serves.
    # Keep this fast — a slow provider must not keep the HTTP client waiting
    # for headers. Cap per-call budget and fall back on any error/timeout.
    try:
        agent_runtime.load_env_file()
        cfg = dict(config or agent_runtime.load_config())
        # Short wall-clock budget for interactive chat (distinct from per-read).
        # 25s is enough for a live answer, short enough to keep the UI snappy.
        cfg.setdefault("stream_budget_ms", 25000)
        # Cap per-hop socket timeout for hosted providers so one hung endpoint
        # does not stall the whole cascade before fallback.
        cfg["timeout_ms"] = min(int(cfg.get("timeout_ms", 180000)), 15000)
        reply = agent_runtime.chat_stream(cfg, grounded_messages, temperature=temperature, max_tokens=max_tokens)
        reply["hits"] = hits
    except Exception as e:
        # No brain reachable — answer extractively rather than error.
        gen = rag_eval.ExtractiveGenerator()
        text = gen(rag_core.build_prompt(query, hits)) if query else rag_core.REFUSAL_TOKEN
        reply = {"content": text, "elapsed": 0.0, "tokens": len(text.split()), "provider": "extractive-fallback", "fallback_error": "%s: %s" % (type(e).__name__, e)}

    # Audit whatever was generated.
    audit = rag_core.audit_answer(reply.get("content") or "", hits, idx.config)
    audit["query_support"] = round(support, 4)
    audit["question"] = query
    return reply, audit, hits, support

# ── HTML UI (single-file, no build step) ─────────────────────────────────

HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Free Brain — OpenChat</title>
<style>
:root{--bg:#0b0f14;--panel:#111821;--border:#1e2a3a;--text:#e6edf3;--muted:#8b9bb0;--accent:#3b82f6;--accent2:#22c55e;--warn:#f59e0b;--err:#ef4444;--code:#0f172a}
*{box-sizing:border-box}html,body{height:100%;margin:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans,system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial}
a{color:var(--accent)}header{position:sticky;top:0;z-index:2;background:rgba(11,15,20,.9);backdrop-filter:blur(8px);border-bottom:1px solid var(--border);padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.02em}.pill{border:1px solid var(--border);background:var(--panel);border-radius:999px;padding:4px 10px;font-size:12px;color:var(--muted)}
.pill.ok{border-color:rgba(34,197,94,.35);color:#bbf7d0}.pill.bad{border-color:rgba(239,68,68,.35);color:#fecaca}
.wrap{max-width:980px;margin:0 auto;padding:14px;display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:900px){.wrap{grid-template-columns:1.75fr .95fr}}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.panel h3{margin:0;padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}
.chat{display:flex;flex-direction:column;height:min(72vh,760px)}
.log{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:92%;padding:10px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word;border:1px solid transparent}
.msg.user{align-self:flex-end;background:#1a2740;border-color:#203355}
.msg.assistant{align-self:flex-start;background:#0f1a14;border-color:#1b2f23}
.msg.system{align-self:center;background:#1a1a12;border-color:#2f2a1b;color:#ffe9a8;font-size:12px}
.meta{font-size:11px;color:var(--muted);margin-top:6px;display:flex;gap:8px;flex-wrap:wrap}
.meta span{border:1px solid var(--border);background:rgba(255,255,255,.03);padding:2px 6px;border-radius:999px}
.sources{font-size:12px;color:var(--muted);border-top:1px dashed var(--border);margin-top:8px;padding-top:6px}
.sources a{color:var(--muted);text-decoration:underline}
.composer{display:flex;gap:8px;padding:10px;border-top:1px solid var(--border);background:rgba(255,255,255,.01)}
.composer textarea{flex:1;min-height:44px;max-height:120px;resize:vertical;background:var(--code);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:10px 12px;outline:none}
.composer button, .btn{background:var(--accent);color:white;border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer}
.btn.secondary{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn:disabled{opacity:.6;cursor:not-allowed}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.kv{display:grid;grid-template-columns:110px 1fr;gap:6px;font-size:12px;padding:8px 12px}
.kv dt{color:var(--muted)} .kv dd{margin:0}
pre{margin:0;padding:10px 12px;overflow:auto;background:var(--code);border-top:1px solid var(--border);max-height:280px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.toggle{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
input[type="checkbox"]{accent-color:var(--accent)}
.hits{display:flex;flex-direction:column;gap:8px;padding:8px 12px;max-height:52vh;overflow:auto}
.hit{border:1px solid var(--border);border-radius:10px;padding:8px 10px;background:rgba(255,255,255,.02)}
.hit .head{font-size:12px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap}
.hit .text{font-size:13px;margin-top:6px;white-space:pre-wrap}
footer{padding:10px 14px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);text-align:center}
</style>
</head>
<body>
<header>
  <div class="brand">Free Brain — OpenChat</div>
  <span id="health" class="pill">checking…</span>
  <span id="index-pill" class="pill">index…</span>
  <span style="margin-left:auto" class="row">
    <label class="toggle"><input id="useRag" type="checkbox" checked/> grounded (RAG)</label>
    <label class="toggle"><input id="doStream" type="checkbox" checked/> stream</label>
    <a class="pill" href="/openapi.json" target="_blank">openapi</a>
    <a class="pill" href="/.well-known/ai-plugin.json" target="_blank">plugin</a>
  </span>
</header>

<div class="wrap">
  <div class="panel chat">
    <h3>Chat — grounded answers with citations, or INSUFFICIENT_CONTEXT</h3>
    <div id="log" class="log">
      <div class="msg system">This brain is local-first: it tries Ollama/vLLM on 127.0.0.1, then free hosted providers if keys are set, then falls back to extractive retrieval. With grounded on, answers cite sources [n] that map to the hits on the right.</div>
    </div>
    <div class="composer">
      <textarea id="input" placeholder="Ask anything in the corpus — e.g. 'What is the phase-clock law?' or 'What tool can sync the residence?' — Shift+Enter for newline, Enter to send"></textarea>
      <button id="send">Send</button>
    </div>
    <div style="display:flex;gap:8px;padding:0 10px 10px 10px;flex-wrap:wrap">
      <button class="btn secondary" id="btnHealth">Health</button>
      <button class="btn secondary" id="btnBuild">Rebuild index</button>
      <button class="btn secondary" id="btnClear">Clear</button>
      <span id="status" style="align-self:center;color:var(--muted);font-size:12px"></span>
    </div>
  </div>

  <div style="display:flex;flex-direction:column;gap:12px">
    <div class="panel">
      <h3>Indexing — plugin retrieval</h3>
      <div class="kv" id="indexBox"></div>
      <div class="row" style="padding:8px 12px">
        <input id="q" placeholder="search query — e.g. dispatch-graph hashing" style="flex:1;background:var(--code);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:8px 10px"/>
        <button class="btn" id="btnSearch">Search</button>
      </div>
      <div id="hits" class="hits"><div style="color:var(--muted);font-size:12px;padding:6px">Results appear here with citation [n], source, span, and channel.</div></div>
      <pre><code id="searchJson" style="white-space:pre-wrap"></code></pre>
    </div>
    <div class="panel">
      <h3>Last audit — smart & valid</h3>
      <pre><code id="audit">No chat yet.</code></pre>
      <div class="row" style="padding:8px 12px">
        <button class="btn secondary" id="btnContract">Contract</button>
        <button class="btn secondary" id="btnEval">Eval</button>
      </div>
      <pre><code id="extra"></code></pre>
    </div>
  </div>
</div>

<footer>Grounded on <code id="footFiles">—</code> files • <span id="footModel">brain: local → cascade → extractive fallback</span> • refusal is a correct outcome</footer>

<script>
const $ = s => document.querySelector(s);
const log = $('#log'), input = $('#input'), send = $('#send'), statusEl=$('#status');
const useRag = $('#useRag'), doStream = $('#doStream'), auditEl=$('#audit'), extraEl=$('#extra');
const qEl=$('#q'), hitsEl=$('#hits'), searchJson=$('#searchJson'), indexBox=$('#indexBox');
let history = []; // {role, content}

function el(tag, cls, text){ const n=document.createElement(tag); if(cls) n.className=cls; if(text!=null) n.textContent=text; return n; }
function addMsg(role, text, meta){
  const d=el('div','msg '+role); d.textContent=text;
  if(meta){ const m=el('div','meta'); for(const [k,v] of Object.entries(meta)){ const s=el('span','',k+': '+v); m.appendChild(s);} d.appendChild(m); }
  log.appendChild(d); log.scrollTop=log.scrollHeight; return d;
}
function setHealth(ok, detail){
  const pill=document.getElementById('health'); pill.textContent = ok? 'brain: ok' : 'brain: degraded';
  pill.className='pill '+(ok?'ok':'bad'); if(detail) pill.title=detail;
}
async function fetchJSON(url, opts){
  const r=await fetch(url, opts); const t=await r.text(); let j; try{ j=JSON.parse(t);}catch{ j={raw:t} } if(!r.ok) throw new Error(j.reason||j.error||t.slice(0,300)); return j;
}
async function refreshHealth(){
  try{
    const h=await fetchJSON('/api/health'); setHealth(h.ok, JSON.stringify(h,null,1).slice(0,600));
    const i=h.index||{}; document.getElementById('index-pill').textContent = `index: ${i.chunks||0} chunks / ${i.files||0} files`;
    indexBox.innerHTML = `<dt>chunks</dt><dd>${i.chunks||0}</dd><dt>files</dt><dd>${i.files||0}</dd><dt>fusion</dt><dd>${(i.config&&i.config.fusion)||'-'}</dd><dt>budget</dt><dd>${(i.config&&i.config.chunk_budget_words)||'-'} / overlap ${(i.config&&i.config.chunk_overlap_words)||'-'}</dd><dt>support gate</dt><dd>${(i.config&&i.config.min_query_support)||0}</dd>`;
    document.getElementById('footFiles').textContent = `${i.files||0}`;
    extraEl.textContent = JSON.stringify(h,null,1);
  }catch(e){ setHealth(false, String(e)); statusEl.textContent='health: '+e.message; }
}
async function doSearch(){
  const q=qEl.value.trim(); if(!q) return;
  hitsEl.innerHTML='<div style="color:var(--muted);font-size:12px">searching…</div>';
  try{
    const j=await fetchJSON('/api/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})});
    searchJson.textContent = JSON.stringify(j,null,1);
    hitsEl.innerHTML='';
    (j.hits||[]).forEach(h=>{
      const box=el('div','hit');
      const head=el('div','head'); head.textContent=`[${h.rank}] ${h.source} ${h.heading? '§ '+h.heading:''}  span ${h.span[0]}-${h.span[1]}  ${h.channels}  score ${h.score}`;
      const text=el('div','text'); text.textContent=h.text.slice(0,420);
      box.appendChild(head); box.appendChild(text); hitsEl.appendChild(box);
    });
    if(!(j.hits||[]).length) hitsEl.innerHTML='<div style="color:var(--muted);font-size:12px">No hits — corpus has no lexical overlap with this query.</div>';
  }catch(e){ hitsEl.innerHTML='<div style="color:var(--err)">'+e.message+'</div>'; }
}
async function sendChat(){
  const text=input.value.trim(); if(!text) return;
  input.value=''; history.push({role:'user', content:text});
  addMsg('user', text);
  statusEl.textContent='thinking…'; send.disabled=true;
  const payload = {messages: history, stream: doStream.checked, use_rag: useRag.checked};
  let assistantNode = addMsg('assistant','', null);
  let full = ''; let lastAudit=null;
  try{
    if(payload.stream){
      const resp = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(!resp.ok){ const t=await resp.text(); throw new Error(t.slice(0,500)); }
      const reader = resp.body.getReader(); const dec=new TextDecoder();
      let buf='';
      while(true){
        const {value, done}=await reader.read(); if(done) break;
        buf+=dec.decode(value,{stream:true});
        let idx;
        while((idx=buf.indexOf('\n\n'))!==-1){
          const chunk=buf.slice(0,idx); buf=buf.slice(idx+2);
          const line=chunk.split('\n').find(l=>l.startsWith('data:')); if(!line) continue;
          const data=line.slice(5).trim(); if(data==='[DONE]'){ break; }
          try{
            const j=JSON.parse(data);
            if(j.content){ full+=j.content; assistantNode.firstChild && (assistantNode.firstChild.textContent=full); assistantNode.textContent=full; log.scrollTop=log.scrollHeight; }
            if(j.audit) lastAudit=j.audit;
            if(j.error) throw new Error(j.error);
          }catch(_e){ if(data.startsWith('{')) throw _e; }
        }
      }
      // final audit frame may arrive as a separate JSON after stream
      if(lastAudit) auditEl.textContent = JSON.stringify(lastAudit,null,1);
    } else {
      const j=await fetchJSON('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      full=j.content||j.answer||''; assistantNode.textContent=full;
      lastAudit=j.audit||j; auditEl.textContent=JSON.stringify(lastAudit,null,1);
    }
    history.push({role:'assistant', content: full});
    // render audit meta
    if(lastAudit){
      const meta = {verdict: lastAudit.verdict||lastAudit.strict_verdict, support: lastAudit.query_support, fidelity: lastAudit.citation_fidelity, refused: String(!!lastAudit.refused)};
      const m=el('div','meta'); for(const [k,v] of Object.entries(meta)){ const s=el('span','',k+': '+v); m.appendChild(s);} assistantNode.appendChild(m);
      if(lastAudit.sources && lastAudit.sources.length){
        const src=el('div','sources'); src.textContent='sources: '+(lastAudit.sources.map(s=>`${s.citation} ${s.source} [${s.span[0]}-${s.span[1]}]`).join('  •  ')); assistantNode.appendChild(src);
      } else if(lastAudit.hits){
        const src=el('div','sources'); src.textContent='sources: '+(lastAudit.hits||[]).slice(0,4).map(h=>`[${h.rank}] ${h.source} [${h.span[0]}-${h.span[1]}]`).join('  •  ')); assistantNode.appendChild(src);
      }
    }
    statusEl.textContent='ok';
  }catch(e){
    assistantNode.textContent='[error] '+e.message;
    statusEl.textContent='error: '+e.message;
  }finally{ send.disabled=false; log.scrollTop=log.scrollHeight; }
}

send.addEventListener('click', sendChat);
input.addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); sendChat(); }});
document.getElementById('btnSearch').addEventListener('click', doSearch);
qEl.addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(); });
document.getElementById('btnHealth').addEventListener('click', refreshHealth);
document.getElementById('btnClear').addEventListener('click', ()=>{ history=[]; log.querySelectorAll('.msg:not(.system)').forEach(n=>n.remove()); auditEl.textContent='No chat yet.'; });
document.getElementById('btnBuild').addEventListener('click', async()=>{
  statusEl.textContent='rebuilding index…';
  try{ const j=await fetchJSON('/api/build',{method:'POST'}); statusEl.textContent=`indexed ${j.chunks} chunks from ${j.files} files`; refreshHealth(); }catch(e){ statusEl.textContent='build: '+e.message; }
});
document.getElementById('btnContract').addEventListener('click', async()=>{
  extraEl.textContent = JSON.stringify(await fetchJSON('/api/contract'),null,1);
});
document.getElementById('btnEval').addEventListener('click', async()=>{
  extraEl.textContent='evaluating…';
  extraEl.textContent = JSON.stringify(await fetchJSON('/api/eval'),null,1);
});
refreshHealth();
</script>
</body>
</html>
"""

# ── HTTP handler ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, obj, status=200):
        body = json.dumps(obj, indent=1, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, body, ctype="text/plain; charset=utf-8", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def log_message(self, fmt, *args):
        # Quiet except errors; log to stderr in dev.
        if os.environ.get("OPENCHAT_VERBOSE") == "1":
            sys.stderr.write("[openchat] %s\n" % (fmt % args))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError("invalid JSON: %s" % e)

    def _host_header(self):
        # For openapi server URL.
        h = self.headers.get("Host") or f"localhost:{DEFAULT_PORT}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{h}"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html", "/chat"):
            return self._text(HTML_UI, "text/html; charset=utf-8")

        if path in ("/.well-known/ai-plugin.json", "/ai-plugin.json", "/plugin.json", "/.well-known/plugin.json"):
            return self._json(PLUGIN_MANIFEST)

        if path in ("/openapi.json", "/.well-known/openapi.json", "/openapi.yaml"):
            return self._json(openapi_spec(self._host_header()))

        if path == "/api/contract":
            try:
                import importlib.util as iu
                spec = iu.spec_from_file_location("rag_skill", os.path.join(os.path.dirname(__file__), ".agents", "skills", "builderbro-rag", "rag_skill.py"))
                if spec and spec.loader:
                    mod = iu.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return self._json(mod.CONTRACT)
                return self._json({"ok": False, "error": "contract_unavailable"})
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        if path == "/api/health":
            try:
                idx = index_status()
                # cascade chain (no network — just local file + env)
                try:
                    agent_runtime.load_env_file()
                    cfg = agent_runtime.load_config()
                    chain_txt = brain_cascade.format_chain(cfg)
                    cascade_ok = True
                except Exception as e:
                    chain_txt = str(e)
                    cascade_ok = False
                return self._json({
                    "ok": idx.get("ok", False) and True,
                    "version": VERSION,
                    "index": idx,
                    "cascade": {"ok": cascade_ok, "chain": chain_txt},
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        if path in ("/api/index", "/api/index/status", "/api/status"):
            return self._json(index_status())

        if path == "/api/search" and qs.get("q"):
            q = qs["q"][0]
            k = int(qs.get("k", [6])[0])
            try:
                idx = get_index()
                hits = idx.search(q, k=k)
                return self._json({
                    "ok": True, "query": q, "k": k, "n_hits": len(hits),
                    "hits": [
                        {"rank": h["rank"], "citation": "[%d]" % h["rank"], "chunk_id": h["chunk"]["id"], "source": h["chunk"]["source"], "heading": h["chunk"]["heading"], "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]], "score": round(h["score"], 6), "channels": ("both" if h["both"] else "lexical" if h["lexical_only"] else "dense" if h["dense_only"] else "neither"), "text": h["chunk"]["text"]}
                        for h in hits
                    ],
                })
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        if path == "/api/eval":
            try:
                idx = get_index()
                report = rag_eval.evaluate(idx, rag_eval.ExtractiveGenerator(), config=idx.config, generator_name="extractive (reference)")
                return self._json(report)
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        # Static fallback: 404 as JSON so plugins get machine-readable errors.
        return self._json({"ok": False, "error": "not_found", "reason": "no route %r" % path}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # --- search (plugin indexing) ---
        if path in ("/api/search", "/api/index/search"):
            try:
                body = self._read_json()
                q = (body.get("query") or body.get("q") or body.get("question") or "").strip()
                if not q:
                    return self._json({"ok": False, "error": "missing_argument", "reason": "query is required"}, 400)
                k = int(body.get("k") or body.get("top_k") or 6)
                idx = get_index()
                hits = idx.search(q, k=k)
                return self._json({
                    "ok": True, "query": q, "k": k, "n_hits": len(hits),
                    "hits": [
                        {"rank": h["rank"], "citation": "[%d]" % h["rank"], "chunk_id": h["chunk"]["id"], "source": h["chunk"]["source"], "heading": h["chunk"]["heading"], "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]], "score": round(h["score"], 6), "channels": ("both" if h["both"] else "lexical" if h["lexical_only"] else "dense" if h["dense_only"] else "neither"), "text": h["chunk"]["text"]}
                        for h in hits
                    ],
                })
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        # --- ask (grounded) ---
        if path in ("/api/ask", "/api/index/ask"):
            try:
                body = self._read_json()
                question = (body.get("question") or body.get("query") or body.get("q") or "").strip()
                if not question:
                    return self._json({"ok": False, "error": "missing_argument", "reason": "question is required"}, 400)
                k = int(body.get("k") or 6)
                generator_name = (body.get("generator") or "live").strip()
                idx = get_index()
                hits = idx.search(question, k=k)

                # Choose generator
                if generator_name == "extractive":
                    gen = rag_eval.ExtractiveGenerator()
                elif generator_name == "hallucinate":
                    gen = rag_eval.HallucinatingGenerator()
                elif generator_name == "live":
                    try:
                        agent_runtime.load_env_file()
                        cfg = agent_runtime.load_config()
                        def _live(msgs):
                            return agent_runtime.chat(cfg, msgs).get("content", "")
                        gen = _live
                    except Exception as e:
                        return self._json({"ok": False, "error": "generation_failed", "reason": "%s: %s" % (type(e).__name__, e)}, 502)
                else:
                    return self._json({"ok": False, "error": "bad_generator", "reason": "unknown generator %r" % generator_name}, 400)

                audit = rag_core.answer(question, hits, gen, idx.config, index=idx)
                return self._json({
                    "ok": audit["verdict"] == "grounding:ok",
                    "refused": audit["refused"],
                    "answer": audit["answer"],
                    "citations": audit["citations"],
                    "invalid_citations": audit["invalid_citations"],
                    "citation_fidelity": audit["citation_fidelity"],
                    "groundedness": audit["groundedness"],
                    "unsupported_rate": audit["unsupported_rate"],
                    "query_support": audit.get("query_support"),
                    "verdict": audit["verdict"],
                    "strict_verdict": audit["strict_verdict"],
                    "warnings": audit["soft_reasons"],
                    "reasons": audit["reasons"],
                    "sources": [{"citation": "[%d]" % h["rank"], "source": h["chunk"]["source"], "heading": h["chunk"]["heading"], "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]], "chunk_id": h["chunk"]["id"]} for h in hits],
                })
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        # --- build index ---
        if path in ("/api/build", "/api/index/build", "/api/reindex"):
            try:
                idx = get_index(force_rebuild=True)
                meta = idx.corpus_meta or {}
                return self._json({"ok": True, "chunks": len(idx.chunks), "files": len(meta.get("files", [])), "bytes": meta.get("total_bytes", 0), "config": {k: idx.config[k] for k in ("fusion", "chunk_budget_words", "chunk_overlap_words", "dense_weight", "min_query_support") if k in idx.config}})
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        # --- chat (openchat) ---
        if path in ("/api/chat", "/v1/chat/completions", "/api/chat/completions"):
            try:
                body = self._read_json()
                # Accept both OpenAI shape and our shape.
                messages = body.get("messages")
                if not messages:
                    # Some clients send {prompt} or {question}
                    q = body.get("prompt") or body.get("question") or body.get("query") or ""
                    if q:
                        messages = [{"role": "user", "content": q}]
                if not messages:
                    return self._json({"ok": False, "error": "missing_argument", "reason": "messages is required (OpenAI shape: [{role,content}])"}, 400)

                stream = bool(body.get("stream"))
                use_rag = body.get("use_rag")
                if use_rag is None:
                    # OpenAI clients don't send use_rag — default to grounded.
                    use_rag = True
                else:
                    use_rag = bool(use_rag)
                temperature = body.get("temperature")
                max_tokens = body.get("max_tokens") or body.get("maxTokens")
                model = body.get("model")  # accepted but not required — cascade decides

                agent_runtime.load_env_file()
                cfg = agent_runtime.load_config()
                if model:
                    cfg["model"] = model

                reply, audit, hits, support = grounded_chat(messages, cfg, use_rag=use_rag, temperature=temperature, max_tokens=max_tokens)

                # OpenAI-compatible + audit envelope
                is_openai = path.startswith("/v1/")
                if stream:
                    # SSE streaming — send headers first, then chunked deltas.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("Connection", "close")
                    self._cors()
                    self.end_headers()
                    content = reply.get("content", "")
                    if not content:
                        content = rag_core.REFUSAL_TOKEN
                    words = content.split(" ") if content else [rag_core.REFUSAL_TOKEN]
                    for idx_w, w in enumerate(words):
                        piece = (w + " ") if idx_w != len(words) - 1 else w
                        frame = {"choices": [{"delta": {"content": piece}, "index": 0, "finish_reason": None}], "model": cfg.get("model") or "free-brain"}
                        self.wfile.write(("data: %s\n\n" % json.dumps(frame)).encode("utf-8"))
                        try:
                            self.wfile.flush()
                        except Exception:
                            pass
                        time.sleep(0.005)
                    if audit:
                        audit_frame = {"audit": audit, "hits": [{"rank": h["rank"], "source": h["chunk"]["source"], "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]], "heading": h["chunk"]["heading"]} for h in (hits or [])], "query_support": support}
                        self.wfile.write(("data: %s\n\n" % json.dumps(audit_frame)).encode("utf-8"))
                        try:
                            self.wfile.flush()
                        except Exception:
                            pass
                    done = {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}], "model": cfg.get("model") or "free-brain"}
                    self.wfile.write(("data: %s\n\n" % json.dumps(done)).encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    try:
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                else:
                    if is_openai:
                        return self._json({
                            "id": "chatcmpl-%s" % hashlib.blake2b(str(time.time()).encode(), digest_size=6).hexdigest(),
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": cfg.get("model") or "free-brain",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply.get("content", "")}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": reply.get("prompt_tokens_est", 0) or len(str(messages)) // 4, "completion_tokens": reply.get("tokens", 0), "total_tokens": (reply.get("prompt_tokens_est", 0) or 0) + reply.get("tokens", 0)},
                            "audit": audit,
                            "provider": reply.get("provider"),
                        })
                    else:
                        return self._json({
                            "ok": True,
                            "content": reply.get("content", ""),
                            "answer": reply.get("content", ""),
                            "audit": audit,
                            "hits": [{"rank": h["rank"], "source": h["chunk"]["source"], "heading": h["chunk"]["heading"], "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]], "score": round(h["score"], 6), "text": h["chunk"]["text"]} for h in (hits or [])],
                            "query_support": support,
                            "provider": reply.get("provider"),
                            "tokens": reply.get("tokens"),
                            "elapsed": reply.get("elapsed"),
                        })

            except Exception as e:
                # Never leak a traceback as HTML — plugins need JSON.
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        # --- eval passthrough for plugin self-check ---
        if path == "/api/eval":
            try:
                idx = get_index()
                report = rag_eval.evaluate(idx, rag_eval.ExtractiveGenerator(), config=idx.config, generator_name="extractive (reference)")
                return self._json(report)
            except Exception as e:
                return self._json({"ok": False, "error": type(e).__name__, "reason": str(e)}, 500)

        return self._json({"ok": False, "error": "not_found", "reason": "no route %r" % path}, 404)


def main(argv=None):
    p = argparse.ArgumentParser(description="Free Brain openchat — grounded chat + indexing plugin (stdlib only)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port (default $PORT or 8080)")
    p.add_argument("--host", default=DEFAULT_HOST, help="listen host (default 0.0.0.0)")
    p.add_argument("--build", action="store_true", help="(re)build the index then exit")
    p.add_argument("--rebuild", action="store_true", help="force rebuild before serving")
    p.add_argument("--no-open", action="store_true", help="do not print the open URL")
    args = p.parse_args(argv)

    agent_runtime.load_env_file()
    if args.build:
        idx = get_index(force_rebuild=True)
        meta = idx.corpus_meta or {}
        print(json.dumps({"ok": True, "chunks": len(idx.chunks), "files": len(meta.get("files", [])), "config": idx.config}, indent=1))
        return 0

    if args.rebuild:
        get_index(force_rebuild=True)
    else:
        # Warm the cache once so first /api/search isn't cold.
        try:
            get_index()
        except Exception as e:
            print("[openchat] index warm failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)

    addr = (args.host, args.port)
    # Reuse address quickly on restart (phone watchdog etc.)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(addr, Handler)
    url = f"http://localhost:{args.port}" if args.host in ("0.0.0.0", "127.0.0.1") else f"http://{args.host}:{args.port}"
    print(f"[openchat] Free Brain v{VERSION} — openchat + indexing plugin")
    print(f"[openchat] UI:      {url}/")
    print(f"[openchat] health:  {url}/api/health")
    print(f"[openchat] search:  POST {url}/api/search  {{\"query\":\"...\"}}")
    print(f"[openchat] ask:     POST {url}/api/ask     {{\"question\":\"...\"}}")
    print(f"[openchat] chat:    POST {url}/api/chat    {{\"messages\":[...]}}")
    print(f"[openchat] plugin:  {url}/.well-known/ai-plugin.json  +  {url}/openapi.json")
    print(f"[openchat] OpenAI:  POST {url}/v1/chat/completions")
    if idx_info := index_status():
        print(f"[openchat] index:   {idx_info.get('chunks',0)} chunks / {idx_info.get('files',0)} files  fusion={idx_info.get('config',{}).get('fusion')}")
    try:
        cfg = agent_runtime.load_config()
        print(f"[openchat] brain:   {brain_cascade.format_chain(cfg).splitlines()[0]}")
    except Exception:
        pass
    if not args.no_open:
        print(f"[openchat] serving — Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[openchat] bye")
        httpd.shutdown()
    return 0

if __name__ == "__main__":
    sys.exit(main())
