"""rag_core.py — local, dependency-free RAG core (retrieval quality + grounding).

This is the RAG half of BuilderBro. It is deliberately stdlib-only and offline,
matching the rest of the harness (`agent_runtime.py` / `brain_cascade.py` carry
no third-party imports). No paid service, no network, no numpy.

HONEST SCOPE — what the "embedding" stage actually is
-----------------------------------------------------
There is no neural encoder here. `embed()` is a *hashing vectoriser*: each
feature (unigram, bigram, char 4-gram) is hashed into one of `dim` buckets with
a signed contribution, weighted by (1+log tf) * idf, then L2-normalised. This
buys fuzziness (char n-grams absorb morphology and typos) but it is **not**
semantic. A paraphrase with zero lexical overlap is not retrieved. That
limitation is measured, not hidden: `rag_eval.py` reports per-stage metrics and
`rag_diagnose.py` attributes a retrieval miss to this stage when the gold text
*is* present in a chunk but the query still failed to surface it.

The retrieval workhorse is BM25 (lexical, idf-weighted); hashed-dense is fused
in with Reciprocal Rank Fusion so a char-n-gram match can rescue a query whose
exact tokens were stemmed away. Two measured signals beat one guessed one.

Invariants this module enforces
-------------------------------
1. **Every chunk is explicitly cited-able and addressable.** A chunk has a
   stable content-addressed id, a source path, and a real char span into the
   source file. No "the model said so" provenance.
2. **The model's prose is not data.** `audit_answer()` re-derives grounding from
   the answer text and the retrieved chunks. A citation to a chunk that was not
   retrieved is a *fabricated citation* — a hard failure, never smoothed over.
3. **Refusal is a first-class, machine-checkable outcome.** The grounded prompt
   gives an exact refusal token (`INSUFFICIENT_CONTEXT`), so "I don't know" is
   measurable instead of indistinguishable from a confident wrong answer.
4. **The index is derived, never authoritative.** The persisted index holds only
   chunks; BM25 stats and vectors are rebuilt deterministically on load, so a
   stale vector file cannot silently disagree with the chunk text.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

# ── Configuration ─────────────────────────────────────────────────────────────
# One dict, so rag_diagnose.py can sweep parameters and register a measured win
# instead of a guessed one.

DEFAULT_CONFIG = {
    # chunking — the budget/overlap below are the *measured* winners from the
    # first self-building cycle (rag_diagnose.py), not initial guesses. They are
    # also written to rag_config.json at registration, so the two agree whether
    # or not that file is present.
    "chunk_budget_words": 100,   # target chunk size
    "chunk_overlap_words": 20,   # tail of previous chunk repeated into the next
    "min_chunk_words": 12,
    # embedding
    "dim": 512,                  # hashing-vectoriser bucket count
    "char_ngram": 4,             # char n-gram width for the fuzzy feature
    "char_weight": 0.5,          # relative weight of char n-grams
    # retrieval
    "top_k": 6,
    "rrf_k": 60,                 # Reciprocal Rank Fusion constant
    "bm25_k1": 1.2,
    "bm25_b": 0.75,
    "dense_weight": 1.0,         # per-signal weight inside the fusion
    "bm25_weight": 1.0,
    # Fusion mode is a *measured* choice, not a taste: 'rrf' compresses scores
    # into 1/(k+rank) and throws away magnitude (BM25 7.7 vs 5.9 becomes 0.0318
    # vs 0.0304 — a near tie any noise can flip). 'linear' min-max normalises
    # each channel and keeps the margin. rag_eval.py decides which wins.
    "fusion": "linear",
    "index_headings": True,      # index the heading trail with the body text
    # retrieval confidence gate — see query_support(). 0 disables it.
    "min_query_support": 0.55,
    # grounding audit
    "support_threshold": 0.34,   # content-word recall required to call a sentence supported
    "max_unsupported_rate": 0.34,
    "min_citation_fidelity": 0.90,
    "claim_min_content_words": 4,
}

INDEX_VERSION = 1

# Bound on the per-index score memo (`RagIndex._scores`). Two channels times the
# eval questions is far below this, so the sweep never evicts; the cap exists so a
# long-lived index answering unbounded distinct queries cannot grow without limit.
SCORE_MEMO_MAX = 512
# Vector geometry: the config values that shape the *stored* vectors, so a search
# config that moves one of them cannot be honoured over an index built with
# another. See `RagIndex._check_vector_geometry`.
GEOMETRY_KEYS = ("dim", "char_ngram", "char_weight")
DEFAULT_INDEX_DIR = os.path.join("freebrain-residence", "rag")
# A measured tuning win is written here by rag_diagnose.py and overlaid onto
# DEFAULT_CONFIG on every subsequent call. Registration is what closes the
# self-improvement loop: a sweep that cannot change the runtime is a report,
# not an improvement.
REGISTERED_CONFIG_NAME = "rag_config.json"

# The corpus this harness is *about*. Overridable with RAG_CORPUS
# (os.pathsep-separated files or directories).
DEFAULT_CORPUS = (
    "FREE-BRAIN.md",
    "AGENT-INTEGRITY.md",
    "QIH.md",
    "AGENTIC.md",
    "Freebuff Autonomous Agent Builder (1).md",
    "freebrain-residence/README.md",
    "freebrain-residence/instructions.md",
    "qih-residence/README.md",
    "qih-residence/instructions.md",
)

CORPUS_EXTS = (".md", ".txt", ".py", ".json", ".jsonl", ".mjs", ".js")
IGNORE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".drifting", "dist", "src",
    "NewState", "files.zip", "rag-index",
})
MAX_FILE_BYTES = 512 * 1024  # a 332KB cli.mjs is not knowledge, it is noise

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-/]*")
CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'`])")

REFUSAL_TOKEN = "INSUFFICIENT_CONTEXT"
REFUSAL_MARKERS = (
    REFUSAL_TOKEN.lower(),
    "insufficient context",
    "not in the provided context",
    "cannot be answered from",
    "no relevant information",
    "the sources do not",
    "does not appear in the provided",
)

STOPWORDS = frozenset("""
a an the and or but if then else when while for from to of in on at by with
is are was were be been being do does did done have has had having
it its this that these those there here as not no nor so than too very
can could should would may might must will shall
i you he she they we me my your our their them his her
about into over under again further once only own same such both each few
more most other some any all
""".split())

RAG_SYSTEM_PROMPT = (
    "You are a grounded research assistant. Answer ONLY from the numbered "
    "sources in the user's context.\n"
    "Rules:\n"
    "1. Every factual sentence MUST end with the marker of the source(s) it "
    "came from, e.g. 'Measured throughput was 86 tok/s [2].'\n"
    "2. Cite only source numbers that appear in the context. Inventing a "
    "number is a fabrication.\n"
    "3. If the context does not contain the answer, reply with exactly: "
    "%s — and nothing else. Do not guess, do not use outside knowledge.\n"
    "4. Do not restate the sources verbatim for more than one sentence."
    % REFUSAL_TOKEN
)


# ── Tokenisation ──────────────────────────────────────────────────────────────

def stem(token: str) -> str:
    """Conservative suffix stripping, applied to a fixed point.

    Deterministic, and deliberately timid: Porter-class stemming collapses
    distinct technical tokens this corpus leans on ('ledger' vs 'led'). But it
    must be *consistent across word forms* — an earlier version mapped
    'continuous' to 'continuou' while leaving 'continuously' untouched, so a
    question could not match the sentence that answers it. Suffixes are now
    stripped repeatedly until none apply, so every form lands on one key."""
    t = token
    changed = True
    while changed and len(t) > 3:
        changed = False
        # 'ly' must strip to the adjective stem, never further: stripping
        # 'ously' from 'continuously' yields 'continu' and re-introduces the
        # very mismatch this loop exists to remove.
        for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ly", ""),
                             ("ing", ""), ("ed", "")):
            if t.endswith(suffix) and len(t) - len(suffix) >= 3:
                t = t[: -len(suffix)] + repl
                changed = True
                break
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        t = t[:-1]
    return t


COMPOUND_SPLIT_RE = re.compile(r"[-_/.]")


def tokenize(text: str) -> list:
    """Lowercase content tokens, stemmed, stopwords dropped.

    Compound identifiers are indexed **twice** — whole, and as their parts.
    `dispatch-graph hashing` is one distinctive token, but a question will say
    'dispatch graph'; before this change such a query could not match the
    answering chunk at all (the gold chunk did not reach the top 25). Indexing
    both forms keeps the compound's precision while making its components
    findable. Same for `drive_sync`, `resume-on-crash`, and source paths."""
    out = []
    for raw in TOKEN_RE.findall(text.lower()):
        if len(raw) < 2 or raw in STOPWORDS:
            continue
        out.append(stem(raw))
        if COMPOUND_SPLIT_RE.search(raw):
            for part in COMPOUND_SPLIT_RE.split(raw):
                if len(part) >= 2 and part not in STOPWORDS:
                    out.append(stem(part))
    return out


def content_words(text: str) -> set:
    return {t for t in tokenize(text) if t not in STOPWORDS}


def index_text(chunk, config=None):
    """The text used for *indexing* (never for citation or support checking).

    Chunk boundaries fall on blank lines, so a chunk can legitimately start
    mid-sentence ('from latency. Each drift cycle also writes...') and lose the
    topic word that names what it is about. Prepending the heading trail puts
    the topic back into the searchable text while `chunk['text']` stays exactly
    the bytes the span points at — so provenance and support checks still refer
    to real source text."""
    cfg = config or DEFAULT_CONFIG
    if cfg.get("index_headings", True) and chunk.get("heading"):
        return chunk["heading"] + "\n" + chunk["text"]
    return chunk["text"]


# ── Ingestion ─────────────────────────────────────────────────────────────────

def iter_corpus_files(roots, exts=CORPUS_EXTS, ignore_dirs=IGNORE_DIRS):
    """Yield corpus file paths. A root may be a file (taken as-is) or a
    directory (walked, filtered by extension, with the ignore set applied)."""
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in ignore_dirs)
            for fn in sorted(filenames):
                if not fn.endswith(exts):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(path) > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                yield path


def read_source(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def corpus_roots(env=None):
    env = env if env is not None else os.environ
    raw = env.get("RAG_CORPUS")
    if raw:
        return [p for p in raw.split(os.pathsep) if p]
    return list(DEFAULT_CORPUS)


# ── Chunking ──────────────────────────────────────────────────────────────────

def _split_long_block(text, start, budget):
    """Split a single oversized block into word-windows that each fit the
    budget. Without this, one 900-word section becomes one unusable chunk and
    recall@k silently collapses."""
    words = text.split()
    if len(words) <= budget:
        return [(text, start, start + len(text))]
    out = []
    cursor = start
    for i in range(0, len(words), budget):
        piece = " ".join(words[i:i + budget])
        # Locate the piece in the original text to keep char spans real.
        idx = text.find(words[i], max(0, cursor - start))
        piece_start = start + idx if idx >= 0 else cursor
        out.append((piece, piece_start, piece_start + len(piece)))
        cursor = piece_start + len(piece)
    return out


def chunk_document(source, text, config=None, doc_id=None):
    """Heading-aware, overlapping chunking with real char spans.

    Structure: the file is split into blocks at heading and blank-line
    boundaries. Blocks are packed into chunks up to `chunk_budget_words`, and a
    new chunk restarts a few blocks back so the overlap is genuine text, not
    invented context. Each chunk carries its heading trail, so a chunk's
    provenance is human-readable ('FREE-BRAIN.md > §5 > Q4 loop')."""
    cfg = config or DEFAULT_CONFIG
    budget = cfg["chunk_budget_words"]
    overlap = cfg["chunk_overlap_words"]

    source = source.replace(os.sep, "/")
    blocks = []
    heading_stack = []
    buf = []
    buf_start = None
    pos = 0
    for line in text.splitlines():
        line_start = pos
        pos += len(line) + 1
        m = HEADING_RE.match(line)
        if m:
            if buf:
                blocks.append((" > ".join(heading_stack), "\n".join(buf), buf_start, line_start))
                buf, buf_start = [], None
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            continue
        if not line.strip():
            if buf:
                blocks.append((" > ".join(heading_stack), "\n".join(buf), buf_start, line_start))
                buf, buf_start = [], None
            continue
        if buf_start is None:
            buf_start = line_start
        buf.append(line)
    if buf:
        blocks.append((" > ".join(heading_stack), "\n".join(buf), buf_start, len(text)))

    # Normalise blocks: split oversized ones.
    flat = []
    for heading, btext, bstart, bend in blocks:
        if len(btext.split()) > budget:
            for piece, pstart, pend in _split_long_block(btext, bstart, budget):
                flat.append((heading, piece, pstart, pend))
        else:
            flat.append((heading, btext, bstart, bend))

    if not flat:
        return []

    chunks = []
    i = 0
    n = len(flat)
    while i < n:
        words = 0
        j = i
        while j < n and (words == 0 or words + len(flat[j][1].split()) <= budget):
            words += len(flat[j][1].split())
            j += 1
        window = flat[i:j]
        body = "\n\n".join(b[1] for b in window)
        headings = [b[0] for b in window if b[0]]
        heading = headings[0] if headings else ""
        start = window[0][2]
        end = window[-1][3]
        if len(body.split()) >= cfg["min_chunk_words"] or not chunks:
            cid = hashlib.blake2b(
                ("%s|%d|%s" % (source, start, body)).encode("utf-8"), digest_size=10
            ).hexdigest()
            chunks.append({
                "id": cid,
                "source": source,
                "doc_id": doc_id or source,
                "ordinal": len(chunks),
                "heading": heading,
                "char_start": start,
                "char_end": end,
                "n_words": len(body.split()),
                "text": body,
            })
        if j >= n:
            break
        # Step back for overlap: the next chunk re-includes the tail blocks
        # whose combined size is <= overlap, but always advances.
        back = 0
        tail = 0
        k = j - 1
        while k > i and tail + len(flat[k][1].split()) <= overlap:
            tail += len(flat[k][1].split())
            back += 1
            k -= 1
        i = max(i + 1, j - back)
    return chunks


# ── Embedding (hashing vectoriser) ────────────────────────────────────────────

def _hash_feature(feature: str, dim: int):
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, (1.0 if (value // dim) & 1 else -1.0)


def _cfg(config=None):
    """Merge a partial config over the defaults.

    Call sites legitimately pass one-key overrides (e.g. {'min_query_support':
    0.9} in a test, or a single tuned parameter). Treating that as a complete
    config raised KeyError from deep inside the audit — a caller's convenience
    should not be able to corrupt the grounding check.
    """
    if not config:
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    return merged


def _features(tokens, config):
    """Feature bag: unigrams, adjacent bigrams, and char n-grams for long
    tokens. tf counts are returned so the caller can apply (1+log tf)."""
    cfg = config
    counts = {}
    for tok in tokens:
        counts["w:" + tok] = counts.get("w:" + tok, 0) + 1
    for a, b in zip(tokens, tokens[1:]):
        counts["b:%s_%s" % (a, b)] = counts.get("b:%s_%s" % (a, b), 0) + 1
    width = cfg["char_ngram"]
    for tok in tokens:
        if len(tok) >= width + 1:
            for i in range(len(tok) - width + 1):
                g = "c:%s" % tok[i:i + width]
                counts[g] = counts.get(g, 0) + 1
    return counts


def embed(tokens, idf, config):
    """L2-normalised signed hashing vector. `idf` downweights corpus-common
    terms so a query about 'ledger' is not drowned by 'the agent'."""
    dim = config["dim"]
    if not tokens:
        return [0.0] * dim
    vec = [0.0] * dim
    counts = _features(tokens, config)
    default_idf = idf.get("__default__", 1.0)
    for feature, tf in counts.items():
        base = feature.split(":", 1)[1]
        if feature.startswith("c:"):
            weight = config["char_weight"] * idf.get(base, default_idf)
        elif feature.startswith("b:"):
            weight = idf.get(base.split("_")[0], default_idf) * 0.5
        else:
            weight = idf.get(base, default_idf)
        w = (1.0 + math.log(tf)) * weight
        idx, sign = _hash_feature(feature, dim)
        vec[idx] += sign * w
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# ── Index ─────────────────────────────────────────────────────────────────────

class RagIndex:
    """Hybrid index: BM25 over stemmed tokens + hashed dense vectors, fused
    with Reciprocal Rank Fusion. Both signals are kept separately in each hit so
    `rag_diagnose.py` can see *which* channel found a chunk."""

    def __init__(self, chunks, config=None, corpus_meta=None):
        self.config = dict(config or DEFAULT_CONFIG)
        self.chunks = list(chunks)
        self.corpus_meta = corpus_meta or {}
        self._scores = {}
        self._build()

    def _build(self):
        cfg = self.config
        self.tokens = [tokenize(index_text(c)) for c in self.chunks]
        self.tf = []
        df = {}
        for toks in self.tokens:
            counts = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                df[t] = df.get(t, 0) + 1
        n = max(1, len(self.chunks))
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.idf["__default__"] = math.log(1 + (n + 0.5) / 0.5)
        self.avgdl = (sum(len(t) for t in self.tokens) / n) if n else 0.0
        self.vectors = [embed(toks, self.idf, cfg) for toks in self.tokens]

    # -- scoring channels ----------------------------------------------------

    def _memo_key(self, channel, keys, q_tokens, cfg):
        """Memo key for a scoring channel: the query plus the *only* config values
        that channel reads, read from the caller's config.

        Keying on these rather than on the whole config is what makes reuse safe.
        `rag_diagnose.candidate_configs` sweeps fusion / dense_weight / chunk
        geometry, and neither channel reads any of those in a way that changes its
        output, so two candidates over one index recompute identical vectors. A
        key that ignored them would be right here and wrong the moment one of these
        knobs moved; a key on the whole config would never hit.

        `cfg` is where the keyed values are read from, and it is the same object
        the scores are computed with. Keying on `self.config` instead would hand a
        search under a different config the previous config's scores.
        """
        return (channel, tuple(q_tokens)) + tuple(cfg[k] for k in keys)

    def _check_vector_geometry(self, cfg):
        """Refuse a search config whose vector geometry differs from the build
        config.

        `dim` / `char_ngram` / `char_weight` shape the *stored* vectors. Searching
        with different values compares a query vector against vectors built in
        another space, and a smaller `dim` does not even fail — `zip` in
        `_dense_scores` silently shortens the dot product, so the index would
        return confident nonsense. A refusal is the only honest answer, and the
        fix is to rebuild the index rather than to search it with a foreign
        geometry.
        """
        drift = {k: [self.config[k], cfg[k]] for k in GEOMETRY_KEYS
                 if k in cfg and cfg[k] != self.config[k]}
        if drift:
            raise ValueError(
                "search config's vector geometry differs from the index's build "
                "config (%s): %s — rebuild the index for this config instead of "
                "searching one built with another"
                % (json.dumps({k: self.config[k] for k in GEOMETRY_KEYS},
                              sort_keys=True), json.dumps(drift, sort_keys=True)))

    def _bm25_scores(self, q_tokens, cfg):
        k1, b = cfg["bm25_k1"], cfg["bm25_b"]
        key = self._memo_key("bm25", ("bm25_k1", "bm25_b"), q_tokens, cfg)
        cached = self._scores.get(key)
        if cached is not None:
            return cached
        scores = []
        for i, counts in enumerate(self.tf):
            dl = len(self.tokens[i])
            s = 0.0
            for t in q_tokens:
                f = counts.get(t, 0)
                if not f:
                    continue
                idf = self.idf.get(t, self.idf["__default__"])
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (self.avgdl or 1)))
            scores.append(s)
        self._remember(key, scores)
        return scores

    def _dense_scores(self, q_tokens, cfg):
        # `embed` reads dim / char_ngram / char_weight and the index's own idf;
        # those three are the whole of this channel's config dependence, and
        # `_check_vector_geometry` has already refused a cfg that moves them.
        key = self._memo_key("dense", ("dim", "char_ngram", "char_weight"), q_tokens, cfg)
        cached = self._scores.get(key)
        if cached is not None:
            return cached
        qv = embed(q_tokens, self.idf, cfg)
        out = []
        for dv in self.vectors:
            out.append(sum(a * b for a, b in zip(qv, dv)))
        self._remember(key, out)
        return out

    def _remember(self, key, scores):
        """Store a channel's scores, bounded so a long-lived index cannot grow
        without limit. The bound is a memory guard, not a measured threshold: an
        eviction costs a recomputation, never a different answer."""
        if len(self._scores) >= SCORE_MEMO_MAX:
            self._scores.clear()
        self._scores[key] = scores

    @staticmethod
    def _minmax(scores):
        if not scores:
            return []
        lo, hi = min(scores), max(scores)
        if hi - lo < 1e-12:
            return [0.0] * len(scores)
        return [(s - lo) / (hi - lo) for s in scores]

    def search(self, query, k=None, config=None):
        cfg = config or self.config
        k = k or cfg["top_k"]
        mode = cfg.get("fusion", "linear")
        q_tokens = tokenize(query)
        if not q_tokens or not self.chunks:
            return []
        self._check_vector_geometry(cfg)
        bm25 = self._bm25_scores(q_tokens, cfg)
        dense = self._dense_scores(q_tokens, cfg)

        def ranks(scores):
            order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
            return {i: r + 1 for r, i in enumerate(order)}

        bm25_rank = ranks(bm25)
        dense_rank = ranks(dense)
        if mode == "bm25":
            fused_scores = list(bm25)
        elif mode == "linear":
            nb, nd = self._minmax(bm25), self._minmax(dense)
            fused_scores = [cfg["bm25_weight"] * a + cfg["dense_weight"] * b
                            for a, b in zip(nb, nd)]
        else:  # rrf
            fused_scores = [
                cfg["bm25_weight"] / (cfg["rrf_k"] + bm25_rank[i])
                + cfg["dense_weight"] / (cfg["rrf_k"] + dense_rank[i])
                for i in range(len(self.chunks))
            ]

        hits = []
        for i, chunk in enumerate(self.chunks):
            fused = fused_scores[i]
            hits.append({
                "chunk": chunk,
                "score": fused,
                "bm25": bm25[i],
                "dense": dense[i],
                "bm25_rank": bm25_rank[i],
                "dense_rank": dense_rank[i],
                "lexical_only": dense_rank[i] > k and bm25_rank[i] <= k,
                "dense_only": bm25_rank[i] > k and dense_rank[i] <= k,
                "both": bm25_rank[i] <= k and dense_rank[i] <= k,
            })
        hits.sort(key=lambda h: (-h["score"], h["chunk"]["id"]))
        top = hits[:k]
        for n, h in enumerate(top, 1):
            h["rank"] = n
        return top

    # -- persistence ---------------------------------------------------------

    def save(self, directory=None, env=None):
        directory = directory or index_dir(env)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "index.json")
        payload = {
            "version": INDEX_VERSION,
            "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "config": self.config,
            "corpus_meta": self.corpus_meta,
            "chunks": self.chunks,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, sort_keys=True)
        return path

    @staticmethod
    def load(directory=None, env=None):
        directory = directory or index_dir(env)
        path = os.path.join(directory, "index.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("version") != INDEX_VERSION:
            return None
        # Config comes from the *stored* payload so a registered tuning win
        # reproduces the vectors it was measured under.
        return RagIndex(payload["chunks"], payload.get("config"),
                        payload.get("corpus_meta"))


def index_dir(env=None):
    env = env if env is not None else os.environ
    return env.get("RAG_INDEX_DIR") or DEFAULT_INDEX_DIR


def config_path(env=None):
    env = env if env is not None else os.environ
    return env.get("RAG_CONFIG") or REGISTERED_CONFIG_NAME


def active_config(env=None):
    """DEFAULT_CONFIG overlaid with the registered tuning win, if any. Unknown
    keys in the file are ignored so a stale registration cannot inject junk, and
    a corrupt file degrades to the defaults rather than raising mid-retrieval."""
    cfg = dict(DEFAULT_CONFIG)
    path = config_path(env)
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return cfg
    params = payload.get("params", payload) if isinstance(payload, dict) else {}
    for key, value in params.items():
        if key in DEFAULT_CONFIG and isinstance(value, (int, float, str)):
            cfg[key] = value
    return cfg


def register_config(params, source="rag_diagnose.py", path=None, env=None):
    """Persist a tuned parameter set so it takes effect on the next run."""
    path = path or config_path(env)
    payload = {
        "registered": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "params": {k: v for k, v in params.items() if k in DEFAULT_CONFIG},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
    return path


# ── Build ─────────────────────────────────────────────────────────────────────

def ingest(roots, config=None):
    """Read the corpus and produce chunks. Returns (chunks, meta) where meta
    records per-file provenance so an empty or truncated corpus is visible
    rather than looking like a retrieval failure."""
    cfg = config or DEFAULT_CONFIG
    chunks = []
    meta = {"files": [], "skipped": [], "total_bytes": 0}
    for path in iter_corpus_files(roots):
        try:
            text = read_source(path)
        except OSError as e:
            meta["skipped"].append({"path": path, "reason": str(e)})
            continue
        if not text.strip():
            meta["skipped"].append({"path": path, "reason": "empty"})
            continue
        meta["files"].append({"path": path.replace(os.sep, "/"),
                              "bytes": len(text.encode("utf-8"))})
        meta["total_bytes"] += len(text.encode("utf-8"))
        chunks.extend(chunk_document(path, text, cfg))
    return chunks, meta


def build_index(roots=None, config=None, env=None, save=False):
    cfg = dict(config) if config is not None else active_config(env)
    roots = roots if roots is not None else corpus_roots(env)
    chunks, meta = ingest(roots, cfg)
    index = RagIndex(chunks, cfg, meta)
    if save:
        index.save(env=env)
    return index


# ── Grounded generation ───────────────────────────────────────────────────────

def build_context(hits, max_chars=None):
    """Numbered context block. The numbering IS the citation namespace: a marker
    [n] is only meaningful against this exact list, which is why the auditor
    checks against `hits` and never against anything the model produced."""
    lines = []
    total = 0
    for h in hits:
        c = h["chunk"]
        label = "%s [%d]" % (c["source"], h["rank"])
        header = "SOURCE [%d] %s (lines/span %d-%d)%s" % (
            h["rank"], c["source"], c["char_start"], c["char_end"],
            ("  § %s" % c["heading"]) if c["heading"] else "",
        )
        block = "%s\n%s\n\n" % (header, c["text"])
        if max_chars and total + len(block) > max_chars and lines:
            break
        lines.append(block)
        total += len(block)
        _ = label
    return "".join(lines).rstrip()


# MEASURED NEGATIVE RESULT — do not "clean up" this list by removing
# interrogatives. Adding what/which/who/how/why/where to STOPWORDS looks
# obviously right (a wh-word carries no retrieval information) and it slightly
# *improved* MRR (0.729 -> 0.735), but it destroyed the confidence gate's ability
# to separate answerable from unanswerable questions: minimum positive support
# fell to 0.267 while the strongest unanswerable question scored 0.512, so no
# threshold could work (refusal accuracy 1.000 -> 0.941, with a false refusal on
# a question the corpus answers).
#
# The reason is worth keeping: a common query word like 'who' occurs all over the
# corpus, so it contributed spurious support to *unanswerable* questions and was
# propping up the observed margin. The gate's measured separation therefore
# depends on the query-term set in a way this corpus cannot justify, and the
# honest status is "calibrated on 17 items, fragile" — not "fixed". See SKILL.md.


def query_support(query, hits, index=None, config=None):
    """How much of the question's *information* the retrieved context actually
    contains, on a 0..1 scale.

    This exists because lexical retrieval always returns *something*: ask about
    the weather in Lisbon and BM25 still hands back six chunks, because 'during',
    'run' and 'P0' match. Measured before this gate existed, the pipeline
    answered 3/3 unanswerable questions — a false-answer rate of 1.00 — which is
    the worst possible grounding failure and invisible to every retrieval metric.

    Weighting by idf is what makes it work: a term absent from the whole corpus
    ('lisbon') carries the maximum idf, so its absence dominates the ratio, while
    a question that only reuses corpus-common words is not punished for it.
    Returns 0..1 (the idf-weighted fraction of query terms found in the context).
    """
    cfg = _cfg(config)
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return 0.0
    context = set()
    for h in hits:
        context |= set(tokenize(h["chunk"]["text"]))
    if not context:
        return 0.0
    default_idf = 1.0
    if index is not None:
        default_idf = index.idf.get("__default__", 1.0)

        def weight(tok):
            return index.idf.get(tok, default_idf)
    else:
        def weight(tok):
            return 1.0
    total = sum(weight(t) for t in q_tokens)
    if total <= 0:
        return 0.0
    found = sum(weight(t) for t in q_tokens if t in context)
    return found / total


def refusal_audit(question, hits, support, threshold, reason):
    """A refusal is a first-class result with the same shape as a normal audit,
    so downstream code and the ledger cannot special-case it away."""
    return {
        "question": question,
        "answer": REFUSAL_TOKEN,
        "n_context": len(hits),
        "citations": [],
        "citation_markers": 0,
        "invalid_citations": [],
        "citation_fidelity": 1.0,
        "claims": 0,
        "unsupported_claims": [],
        "uncited_claims": [],
        "weak_claims": [],
        "unsupported_rate": 0.0,
        "groundedness": 1.0,
        "refused": True,
        "query_support": round(support, 4),
        "verdict": "grounding:ok",
        "hard_reasons": [],
        "soft_reasons": [],
        "strict_verdict": "grounding:ok",
        "reasons": [reason],
    }


def build_prompt(question, hits):
    system = {"role": "system", "content": RAG_SYSTEM_PROMPT}
    user = {
        "role": "user",
        "content": "SOURCES:\n%s\nQUESTION: %s" % (build_context(hits), question),
    }
    return [system, user]


def answer(question, hits, generate, config=None, index=None):
    """Call the injected generator on a grounded prompt, then audit it.
    `generate(messages) -> str` is injected so the whole grounding path is
    testable with zero network (and so the live path reuses the cascade).

    The confidence gate runs *before* generation: if the retrieved context does
    not contain the question's information, the model is never asked, and the
    refusal is recorded as a result rather than hoping the model behaves."""
    cfg = _cfg(config)
    support = query_support(question, hits, index=index, config=cfg)
    threshold = cfg.get("min_query_support", 0.0)
    if threshold and support < threshold:
        return refusal_audit(
            question, hits, support, threshold,
            "insufficient_query_support:%.3f<%.2f" % (support, threshold))
    raw = generate(build_prompt(question, hits))
    audit = audit_answer(raw or "", hits, cfg)
    audit["question"] = question
    audit["query_support"] = round(support, 4)
    return audit


# ── Grounding audit — the measurement that matters ────────────────────────────

def split_sentences(text):
    """Sentence split. Deliberately simple and documented as imperfect: it does
    not know abbreviations, so 'e.g. this' may over-split. Over-splitting makes
    the auditor *stricter* (more sentences to support), never looser.

    One correction is mandatory, though. Writing 'claim. [2]' is common in model
    output, and a naive split puts the marker at the head of the *next* fragment,
    detaching every citation from the sentence it supports and making the audit
    report unsupported claims that are in fact perfectly cited. Leading markers
    are therefore re-attached to the preceding sentence."""
    parts = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        parts.extend(p.strip() for p in SENTENCE_SPLIT_RE.split(para) if p.strip())
    merged = []
    for part in parts:
        if merged and CITATION_RE.match(part):
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return merged


_ALPHA_TOKEN_RE = re.compile(r"^[a-z]+$")
# Structural punctuation marks a fragment as data rather than prose: JSON braces,
# markdown/pipe tables, and LaTeX/math line breaks. A bare '{' or '\' in an
# English sentence is vanishingly rare in this corpus; in a JSON schema dump or a
# display equation it is ubiquitous. Alphabetic-word density alone was not enough
# — '{"step_id": 2, "action": "build"}' is 4 alpha words in 6 tokens and slipped
# through until this rule was added.
STRUCTURAL_RE = re.compile(r"[{}|\\]")


def is_prose_like(text, min_letters=20):
    """Is this fragment a prose claim at all?

    The corpus contains JSON blobs, tables and code fences. Counting a JSON
    schema dump as an unsupported *claim* measures the corpus format, not
    grounding — and it is how this audit first reported a 0.64 unsupported rate
    for a generator that only ever copied real sentences. A fragment must be
    mostly alphabetic words, with no structural punctuation, to be graded as a
    factual claim; structured data is neither a claim nor support for one."""
    if STRUCTURAL_RE.search(text):
        return False
    if sum(1 for ch in text if ch.isalpha()) < min_letters:
        return False
    toks = TOKEN_RE.findall(text.lower())
    if not toks:
        return False
    alpha = sum(1 for t in toks if _ALPHA_TOKEN_RE.match(t))
    return (alpha / float(len(toks))) >= 0.6


def parse_citations(text):
    """All citation indices in order of appearance, with duplicates preserved
    for the second pass (fidelity counts markers, fabrication counts markers)."""
    found = []
    for m in CITATION_RE.finditer(text):
        for piece in m.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit():
                found.append(int(piece))
    return found


def is_refusal(text):
    low = text.lower()
    return any(marker in low for marker in REFUSAL_MARKERS)


def _sentence_support(sentence, hit):
    """Lexical entailment proxy: content-word recall of the sentence against the
    chunk, with a numeric-agreement bonus. This is the weakest link in the
    grounding chain and is labelled as such — it can be fooled by a sentence
    that copies vocabulary while inverting meaning."""
    words = content_words(sentence)
    if not words:
        return 1.0
    chunk_words = content_words(hit["chunk"]["text"])
    if not chunk_words:
        return 0.0
    recall = len(words & chunk_words) / float(len(words))
    # Numbers are the highest-value claims to check (a hallucinated '86 tok/s'
    # with correct surrounding vocabulary must not pass as supported).
    nums = {t for t in words if any(ch.isdigit() for ch in t)}
    if nums:
        chunk_nums = {t for t in chunk_words if any(ch.isdigit() for ch in t)}
        recall = recall * (len(nums & chunk_nums) / float(len(nums)))
    return recall


def audit_answer(text, hits, config=None):
    """Machine-checked grounding report. Cheap, deterministic, and adversarial:
    it assumes the answer may be fabricated and tries to prove it."""
    cfg = _cfg(config)
    text = (text or "").strip()
    n_context = len(hits)
    markers = parse_citations(text)
    # A marker that appears verbatim in the retrieved text is a *quotation* from
    # the source (the corpus is full of '[1]' style references), not a citation
    # the model invented. Counting it as fabricated would punish quoting.
    context_text = "\n".join(h["chunk"]["text"] for h in hits)
    quoted = {i for i in set(markers) if "[%d]" % i in context_text}
    valid = [i for i in markers if 1 <= i <= n_context or i in quoted]
    invalid = [i for i in markers if not (1 <= i <= n_context) and i not in quoted]
    fidelity = (len(valid) / float(len(markers))) if markers else (0.0 if not is_refusal(text) else 1.0)

    refused = is_refusal(text)
    min_words = cfg["claim_min_content_words"]
    claims, unsupported, uncited, weakly = 0, [], [], []

    # Pass 1 — structure. Decide which fragments are prose claims, and let a
    # citation that sits *outside* a prose sentence attach to the nearest
    # preceding claim. This matters for real model output, which writes:
    #
    #     The law scales subjective time by the frequency ratio:
    #     \[ dτ = (Ω₀/Ω(x)) dt \]
    #     [4]
    #
    # A naive per-fragment audit calls the sentence uncited and reports a
    # well-grounded answer as a grounding failure. A reader attaches [4] to the
    # sentence above the equation, and so does this. The marker still has to
    # resolve to a real retrieved source, so nothing is excused by attaching it.
    claim_list = []  # [sentence, [citation indices attached from outside it]]
    for fragment in split_sentences(text):
        if is_refusal(fragment):
            continue
        body = CITATION_RE.sub("", fragment).strip()
        words = content_words(body)
        if len(words) >= min_words and is_prose_like(body):
            claim_list.append([fragment, []])
        else:
            # Framing, boilerplate, JSON, table rows, display equations, and
            # bare '[n]' lines are not claims; their markers belong upstream.
            cites = parse_citations(fragment)
            if cites and claim_list:
                claim_list[-1][1].extend(cites)

    # Pass 2 — grade each claim against the sources it cites.
    for sentence, attached in claim_list:
        body = CITATION_RE.sub("", sentence).strip()
        idxs = ([i for i in parse_citations(sentence) if 1 <= i <= n_context]
                + [i for i in attached if 1 <= i <= n_context])
        claims += 1
        if not idxs:
            uncited.append(sentence)
            continue
        best = max(_sentence_support(body, hits[i - 1]) for i in idxs)
        if best >= cfg["support_threshold"]:
            pass
        elif best > 0:
            weakly.append({"sentence": sentence, "support": round(best, 3), "citations": idxs})
        else:
            unsupported.append({"sentence": sentence, "support": 0.0, "citations": idxs})

    bad = len(unsupported) + len(uncited)
    unsupported_rate = (bad / float(claims)) if claims else 0.0
    groundedness = 1.0 - unsupported_rate if claims else (1.0 if refused else 0.0)

    # Two layers, because they are not equally trustworthy.
    #
    # HARD (exact, decidable): did a citation resolve to a chunk that was
    # actually retrieved? A marker either points at a real source or it does
    # not. There is no grey area and no false positives.
    #
    # SOFT (approximate): is each claim's *meaning* present in the source it
    # cites? This is a lexical proxy. Measured against the live brain, a
    # correctly-cited paraphrase scores unsupported_rate 0.60 — the model writes
    # 'converts objective clock time into subjective time' where the source
    # writes an equation, so the vocabulary genuinely does not overlap. Reporting
    # that as a hallucination would be wrong; reporting it as nothing would hide
    # a real gap. So it is measured, surfaced, and kept out of the hard gate.
    hard_reasons = []
    soft_reasons = []
    if invalid:
        hard_reasons.append("fabricated_citation:%s"
                            % ",".join(str(i) for i in sorted(set(invalid))))
    if markers and fidelity < cfg["min_citation_fidelity"]:
        hard_reasons.append("citation_fidelity:%.2f<%.2f"
                            % (fidelity, cfg["min_citation_fidelity"]))
    if not markers and claims and not refused:
        hard_reasons.append("no_citations")
    if refused and markers and invalid:
        hard_reasons.append("refusal_with_fabricated_citation")
    if claims and unsupported_rate > cfg["max_unsupported_rate"]:
        soft_reasons.append("unsupported_rate:%.2f>%.2f"
                            % (unsupported_rate, cfg["max_unsupported_rate"]))

    return {
        "answer": text,
        "n_context": n_context,
        "citations": sorted(set(valid)),
        "citation_markers": len(markers),
        "invalid_citations": sorted(set(invalid)),
        "citation_fidelity": round(fidelity, 4),
        "claims": claims,
        "unsupported_claims": unsupported,
        "uncited_claims": uncited,
        "weak_claims": weakly,
        "unsupported_rate": round(unsupported_rate, 4),
        "groundedness": round(groundedness, 4),
        "refused": refused,
        # verdict = the hard gate: no fabricated or missing citations.
        "verdict": "grounding:ok" if not hard_reasons else "grounding:fail",
        "hard_reasons": hard_reasons,
        "soft_reasons": soft_reasons,
        # strict = hard gate AND the entailment proxy agrees. Stricter than the
        # gate on purpose: the eval reports both so the paraphrase penalty stays
        # visible instead of being tuned away.
        "strict_verdict": ("grounding:ok" if not (hard_reasons or soft_reasons)
                           else "grounding:fail"),
        "reasons": hard_reasons + soft_reasons,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_build(args):
    index = build_index(config=None, env=None, save=True)
    path = index.save(env=None)
    print("index: %s" % path)
    print("files: %d  chunks: %d  bytes: %d"
          % (len(index.corpus_meta.get("files", [])), len(index.chunks),
             index.corpus_meta.get("total_bytes", 0)))
    if index.corpus_meta.get("skipped"):
        print("skipped: %d" % len(index.corpus_meta["skipped"]))
    return 0


def _cmd_search(args):
    index = RagIndex.load() or build_index()
    for h in index.search(args.search, k=args.k):
        c = h["chunk"]
        print("[%d] %.4f (bm25#%d dense#%d) %s %s"
              % (h["rank"], h["score"], h["bm25_rank"], h["dense_rank"],
                 c["id"][:10], c["source"]))
        print("    %s" % c["text"][:160].replace("\n", " "))
    return 0


def _cmd_ask(args):
    index = RagIndex.load() or build_index()
    hits = index.search(args.ask, k=args.k)
    if not hits:
        print("%s" % REFUSAL_TOKEN)
        return 1
    import brain_cascade  # imported here so the core stays import-free offline
    import agent_runtime
    config = agent_runtime.load_config()

    def generate(messages):
        return agent_runtime.chat(config, messages).get("content", "")

    audit = answer(args.ask, hits, generate)
    print(audit["answer"])
    print("\n--- grounding audit ---")
    print("verdict:            %s %s" % (audit["verdict"], audit["reasons"] or ""))
    print("citation fidelity:  %.2f (%d markers)" % (audit["citation_fidelity"], audit["citation_markers"]))
    print("groundedness:       %.2f (%d claims)" % (audit["groundedness"], audit["claims"]))
    print("unsupported:        %d" % len(audit["unsupported_claims"]))
    print("fabricated cites:   %s" % (audit["invalid_citations"] or "none"))
    _ = brain_cascade
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Local RAG core (retrieval + grounded generation)")
    sub = p.add_subparsers(dest="cmd")
    p.add_argument("--build", action="store_true", help="ingest + chunk + index the corpus, then save")
    p.add_argument("--search", metavar="QUERY", help="hybrid retrieval only (no model call)")
    p.add_argument("--ask", metavar="QUESTION", help="retrieve, generate, and audit grounding (needs a brain)")
    p.add_argument("-k", type=int, default=None, help="top-k override")
    args = p.parse_args(argv)
    _ = sub
    if args.build:
        return _cmd_build(args)
    if args.search:
        return _cmd_search(args)
    if args.ask:
        return _cmd_ask(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
