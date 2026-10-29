#!/usr/bin/env python3
"""Fail-closed local adapter from FreeBrain to BuilderBro's verified tools.

This is a subprocess boundary, not a second implementation of BuilderBro policy.
It only invokes the existing builderbro_mcp.py CLI, never a shell or arbitrary
command. Retrieval is extractive-only so this worker cannot trigger paid model
calls. Refusals returned by BuilderBro are valid decisions, not transport errors.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

ALLOWED_TOOLS = frozenset({
    "verify_claim",
    "memory_recall",
    "memory_record",
    "rag_ask",
    "evidence_audit",
})
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 600
MAX_STDOUT_CHARS = 1_000_000


class WorkerError(RuntimeError):
    """A local setup, transport, or contract error."""


def resolve_builderbro_root(
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    base_dir: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    raw = explicit or env.get("BUILDERBRO_ROOT", "").strip()
    if not raw:
        anchor = (base_dir or Path(__file__).resolve().parent).resolve()
        raw = str(anchor.parent / "builderbro-cli")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise WorkerError("BuilderBro root not found; set BUILDERBRO_ROOT to the builderbro-cli checkout")
    if not (root / "builderbro_mcp.py").is_file():
        raise WorkerError("BuilderBro root does not contain builderbro_mcp.py")
    return root


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Pass runtime essentials only; never forward provider or governance secrets."""
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "DRIVE_RESIDENCE",
        "BRO_MEMORY_STORE", "BRO_MCP_RAG_TIMEOUT_S",
    }
    return {key: value for key, value in environ.items() if key in allowed}


def call_tool(
    tool: str,
    arguments: Mapping[str, object],
    *,
    builderbro_root: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> dict:
    env = os.environ if environ is None else environ
    if tool not in ALLOWED_TOOLS:
        raise WorkerError("tool is not on the BuilderBro worker allowlist")
    if not isinstance(arguments, Mapping):
        raise WorkerError("tool arguments must be a JSON object")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise WorkerError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    if tool == "rag_ask":
        generator = arguments.get("generator", "extractive")
        if generator != "extractive":
            raise WorkerError("this zero-cost worker permits only extractive RAG; live generation is disabled")

    root = resolve_builderbro_root(builderbro_root, env)
    payload = json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":"))
    command: Sequence[str] = (
        sys.executable, str(root / "builderbro_mcp.py"),
        "--call", tool, "--json", payload,
    )
    try:
        completed = runner(
            list(command),
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
            env=_child_environment(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerError(f"BuilderBro tool timed out after {timeout_seconds}s") from exc
    except OSError as exc:
        raise WorkerError(f"could not start BuilderBro tool: {type(exc).__name__}") from exc

    stdout = completed.stdout or ""
    if len(stdout) > MAX_STDOUT_CHARS:
        raise WorkerError("BuilderBro response exceeded the 1 MB output limit")
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise WorkerError("BuilderBro returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise WorkerError("BuilderBro response must be a JSON object")
    if completed.returncode != 0:
        raise WorkerError(f"BuilderBro exited with code {completed.returncode}")
    response.setdefault("worker", "freebrain-builderbro")
    response.setdefault("transport_ok", True)
    return response


def status(builderbro_root: str | None = None, environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    root = resolve_builderbro_root(builderbro_root, env)
    return {
        "ok": True,
        "worker": "freebrain-builderbro",
        "schema": "freebrain-builderbro-worker/v1",
        "builderbro_entrypoint": "builderbro_mcp.py",
        "allowed_tools": sorted(ALLOWED_TOOLS),
        "retrieval_mode": "extractive-only",
        "live_generation_enabled": False,
        "root_configured": True,
        "root": str(root),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builderbro-root", help="path to the builderbro-cli checkout")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--status", action="store_true", help="validate local BuilderBro installation")
    parser.add_argument("--call", choices=sorted(ALLOWED_TOOLS), help="call one allowlisted BuilderBro tool")
    parser.add_argument("--json", help="JSON object of tool arguments")
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = status(args.builderbro_root)
        elif args.call:
            try:
                payload = json.loads(args.json or "{}")
            except json.JSONDecodeError as exc:
                raise WorkerError("--json must contain a valid JSON object") from exc
            if not isinstance(payload, dict):
                raise WorkerError("--json must contain a JSON object")
            result = call_tool(args.call, payload, builderbro_root=args.builderbro_root,
                               timeout_seconds=args.timeout)
        else:
            parser.error("choose --status or --call")
            return 2
    except WorkerError as exc:
        print(json.dumps({"ok": False, "worker": "freebrain-builderbro",
                          "error": "worker_refused", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
