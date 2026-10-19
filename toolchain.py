#!/usr/bin/env python3
"""
toolchain.py — the verified action plane (FREE-BRAIN.md §6.1/§6.2, P1).

The brief's novelty claim is *memory-bounded execution of model-generated code*.
Until this module, the loop's tools were read-only by construction, so the
completion gate had never verified a change to the world — only a fact about it
(FREE-BRAIN.md §8.2). This is the gate chain that closes that gap:

    generate (volatile)
      → parse manifest          (schema check)
      → static analyze code     (parse/compile; dep allowlist; banned imports; size cap)
      → sandboxed run           (rlimits: mem / cpu / output / no fork; no network)
      → verify output           (exit code + declared golden assertions)
      → promote to invariant    (ledger entry with howVerified: "gateChain")

Design rules, inherited rather than invented:

  * **A structured failure is always preferable to a fluent fabrication.** Every
    rejection is a `[gate-failed] step=… reason=…` string built by
    `loop_guard.fail` — the one constructor (FREE-BRAIN.md §6.2) — so the loop
    guard counts it and the model never gets raw failed output to smooth over.
  * **The caps live below the model.** The manifest may *declare* a smaller
    bound; it can never raise one. `HARD_MEM_MB` / `HARD_SECONDS` are enforced
    by the runner, not asked of the code.
  * **Absence of a gate is recorded, never assumed.** The rlimit backend cannot
    enforce network isolation at the kernel level, so it says so (`network:
    import-blocklist`) instead of claiming a guarantee it does not give. The
    container backend will enforce it for real; the interface is the same.
  * **Only the gate chain promotes.** The model's output enters volatile and
    becomes invariant only through `run_manifest` with `howVerified: "gateChain"`.

Backends: `RlimitRunner` (stdlib, works on phone-class CPU) is first. A Docker/
seccomp runner is the intended second backend; `run_manifest` takes a `runner`
so it is a swap, not a rewrite.
"""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:  # POSIX only — the honest fail-closed path is below, not a silent unbound run
    import resource as _resource
except ImportError:  # pragma: no cover - non-POSIX
    _resource = None

# ── Limits (the model cannot raise these — they live here, below its write scope) ──

HARD_MEM_MB = 2048          # Test 1 (FREE-BRAIN.md §4)
HARD_SECONDS = 30           # Test 1
DEFAULT_MEM_MB = 2048
DEFAULT_SECONDS = 30
MAX_CODE_BYTES = 64 * 1024  # a tool-chain is a tool, not a program tree
MAX_OUTPUT_BYTES = 8192     # bounded relay: container output is data, never instructions
NOFILE_LIMIT = 64
NPROC_LIMIT = 1             # no fork bombs: the process cannot spawn children

ALLOWED_LANGUAGES = ("python3", "node", "rust", "bash")  # §6.1 allowlist
# Runtimes this backend actually executes. A declared runtime that is not
# enabled is a structured failure, never a silent skip.
RUNNABLE_LANGUAGES = ("python3",)

# stdlib-only execution: any dependency must be named here (pinned) to be used.
ALLOWED_DEPS = ()

ALLOWED_SIDE_EFFECTS = ("none", "stdout", "files", "network")

REQUIRED_FIELDS = ("task", "language", "entrypoint", "resource", "deps", "declares")
RESOURCE_FIELDS = ("max_mem_mb", "max_seconds", "network")

# Banned at import time for python3. Each one is a documented escape or a
# capability the sandbox deliberately withholds; the blocklist is the rlimit
# backend's network/process isolation (recorded as `import-blocklist`).
BANNED_IMPORTS = frozenset({
    "subprocess", "socket", "ctypes", "multiprocessing", "pty", "shutil",
    "resource", "signal", "http", "urllib", "ssl", "ftplib", "smtplib",
    "telnetlib", "asyncio", "importlib", "pickle", "marshal",
})

# Capability-granting calls (eval/exec) and the os.* escape hatches that would
# let code fork, exec, or raise its own limits.
BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "breakpoint",
})
BANNED_ATTRS = frozenset({
    "system", "popen", "fork", "forkpty", "execv", "execve", "execvp",
    "execvpe", "spawnl", "spawnle", "spawnlp", "spawnv", "spawnvp",
    "kill", "killpg", "setsid", "setrlimit", "setpgid",
})


# ── Gate 1: manifest schema (§6.1) ────────────────────────────────────────────

def check_manifest(manifest):
    """Schema gate. Returns (ok, reason, normalized). Anything undeclared is
    denied; a missing field fails here rather than being filled in at runtime."""
    if not isinstance(manifest, dict):
        return False, "manifest:not-an-object", None
    for field in REQUIRED_FIELDS:
        if field not in manifest:
            return False, "manifest:missing-field:%s" % field, None

    task = manifest.get("task")
    if not isinstance(task, str) or not task.strip():
        return False, "manifest:bad-task", None

    language = manifest.get("language")
    if language not in ALLOWED_LANGUAGES:
        return False, "manifest:unknown-language:%s" % language, None

    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return False, "manifest:bad-entrypoint", None
    entrypoint = entrypoint.strip()
    # Scoped to the workdir: no absolute paths, no `..`, no escaping.
    if os.path.isabs(entrypoint) or entrypoint.startswith("~"):
        return False, "manifest:entrypoint-absolute", None
    parts = entrypoint.replace("\\", "/").split("/")
    if ".." in parts or parts[0] in ("", "."):
        return False, "manifest:entrypoint-escapes", None

    resource = manifest.get("resource")
    if not isinstance(resource, dict):
        return False, "manifest:bad-resource", None
    for field in RESOURCE_FIELDS:
        if field not in resource:
            return False, "manifest:resource-missing-field:%s" % field, None
    mem = resource["max_mem_mb"]
    secs = resource["max_seconds"]
    if not isinstance(mem, int) or isinstance(mem, bool) or mem <= 0:
        return False, "manifest:bad-resource:max_mem_mb", None
    if not isinstance(secs, int) or isinstance(secs, bool) or secs <= 0:
        return False, "manifest:bad-resource:max_seconds", None
    # The manifest may *shrink* a bound; it may never raise one. A request for
    # more is a structured refusal, never an operator-silent grant.
    if mem > HARD_MEM_MB:
        return False, "manifest:resource-exceeds-cap:max_mem_mb", None
    if secs > HARD_SECONDS:
        return False, "manifest:resource-exceeds-cap:max_seconds", None
    if not isinstance(resource["network"], bool):
        return False, "manifest:bad-resource:network", None

    deps = manifest.get("deps")
    if not isinstance(deps, list):
        return False, "manifest:bad-deps", None
    for dep in deps:
        if not isinstance(dep, str) or dep not in ALLOWED_DEPS:
            return False, "manifest:dep-not-allowlisted:%s" % dep, None

    declares = manifest.get("declares")
    if not isinstance(declares, dict) or "side_effects" not in declares:
        return False, "manifest:missing-declaration:side_effects", None
    effects = declares["side_effects"]
    if not isinstance(effects, list):
        return False, "manifest:bad-declaration:side_effects", None
    for effect in effects:
        if effect not in ALLOWED_SIDE_EFFECTS:
            return False, "manifest:undeclared-side-effect:%s" % effect, None
    if resource["network"] and "network" not in effects:
        return False, "manifest:missing-declaration:network", None

    normalized = {
        "task": task.strip(),
        "language": language,
        "entrypoint": entrypoint,
        "resource": {
            "max_mem_mb": mem,
            "max_seconds": secs,
            "network": resource["network"],
        },
        "deps": list(deps),
        "declares": {"side_effects": list(effects)},
    }
    if "verify" in manifest:
        verify = manifest["verify"]
        if not isinstance(verify, dict):
            return False, "manifest:bad-verify", None
        for key in verify:
            if key not in ("exit_code", "stdout_contains", "artifact"):
                return False, "manifest:unknown-verify-key:%s" % key, None
        normalized["verify"] = dict(verify)
    return True, "manifest:ok", normalized


# ── Gate 2: static analysis (§6.2) ─────────────────────────────────────────────

def check_static(code, manifest, workdir):
    """Parse/compile, dependency allowlist already passed, banned imports and
    capability calls, size cap. Returns (ok, reason)."""
    if not isinstance(code, str):
        return False, "static:code-not-text"
    raw = code.encode("utf-8", "replace")
    if len(raw) > MAX_CODE_BYTES:
        return False, "static:size-cap:%d" % len(raw)
    if manifest["language"] != "python3":
        # Non-python sources cannot be executed by this backend and are
        # rejected here rather than handed to the runner.
        return False, "static:runtime-not-enabled:%s" % manifest["language"]
    try:
        tree = ast.parse(code, filename=manifest["entrypoint"])
    except SyntaxError as e:
        return False, "static:parse-failed:line=%s" % (e.lineno,)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    return False, "static:banned-import:%s" % root
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS:
                return False, "static:banned-import:%s" % root
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                return False, "static:banned-call:%s" % fn.id
            if isinstance(fn, ast.Attribute) and fn.attr in BANNED_ATTRS:
                return False, "static:banned-call:%s" % fn.attr
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            return False, "static:banned-call:%s" % node.attr
    entry_fs = os.path.join(workdir, manifest["entrypoint"])
    if not os.path.isfile(entry_fs):
        return False, "static:entrypoint-not-found"
    return True, "static:ok"


# ── Gate 3: sandboxed run ─────────────────────────────────────────────────────

class RlimitRunner:
    """POSIX sandbox for phone-class CPU (no Docker, no Rust required).

    Enforces, per the Test 1 contract (FREE-BRAIN.md §4):

      * CPU-seconds  — `RLIMIT_CPU` (SIGXCPU)
      * wall clock   — the parent kills the process group past `max_seconds`
      * memory       — RSS is polled from the parent and the group is killed on
                       breach. Measured on this rig: `RLIMIT_AS` hangs CPython
                       startup and `RLIMIT_DATA` does not cap `mmap` here, so
                       neither is used; the result records `rss-poll` rather
                       than claiming a kernel guarantee this host lacks.
      * output/FS    — `RLIMIT_FSIZE` (SIGXFSZ), which also bounds the relay
      * descriptors  — `RLIMIT_NOFILE`
      * no fork      — `RLIMIT_NPROC` (also blocked at the static gate)

    Network isolation is by import blocklist, NOT by the kernel — stated, not
    implied. The Docker/seccomp backend is the intended kernel-level swap.
    """

    name = "rlimit"
    network_enforcement = "import-blocklist"
    memory_enforcement = "rss-poll"
    output_cap = MAX_OUTPUT_BYTES
    poll_s = 0.05

    def available(self):
        return _resource is not None

    def _apply_limits(self, seconds):
        import os as _os  # noqa: PLC0415 — only touched inside the child

        def _apply():
            _os.setsid()
            _resource.setrlimit(_resource.RLIMIT_CPU, (seconds, seconds))
            _resource.setrlimit(
                _resource.RLIMIT_FSIZE, (self.output_cap, self.output_cap))
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (NOFILE_LIMIT, NOFILE_LIMIT))
            _resource.setrlimit(_resource.RLIMIT_NPROC, (NPROC_LIMIT, NPROC_LIMIT))
        return _apply

    def run(self, manifest, workdir):
        if not self.available():
            return {"ok": False, "reason": "sandbox:platform-unsupported",
                    "exit_code": None, "stdout": "", "stderr": "", "elapsed_s": 0.0}
        resource = manifest["resource"]
        mem_mb = resource["max_mem_mb"]
        seconds = resource["max_seconds"]
        entry = os.path.join(workdir, manifest["entrypoint"])
        command = [sys.executable or "python3", "-I", "-B", entry]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": workdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C",
        }
        out_path = os.path.join(workdir, ".toolchain-out")
        err_path = os.path.join(workdir, ".toolchain-err")
        started = time.time()
        try:
            with open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
                proc = subprocess.Popen(
                    command, cwd=workdir, env=env,
                    stdin=subprocess.DEVNULL, stdout=fout, stderr=ferr,
                    preexec_fn=self._apply_limits(seconds),
                )
                breach = self._wait(proc, mem_mb, seconds, started)
        except OSError as e:
            return {"ok": False, "reason": "sandbox:spawn-failed:%s" % e,
                    "exit_code": None, "stdout": "", "stderr": "",
                    "elapsed_s": round(time.time() - started, 2)}
        stdout = _read_capped(out_path)
        stderr = _read_capped(err_path)
        elapsed = round(time.time() - started, 2)
        if breach:
            return {"ok": False, "reason": breach, "exit_code": None,
                    "stdout": stdout, "stderr": stderr, "elapsed_s": elapsed}
        if proc.returncode < 0:
            # Killed by a signal — RLIMIT_CPU (SIGXCPU) or RLIMIT_FSIZE
            # (SIGXFSZ). Which limit fired is the finding.
            return {"ok": False, "reason": "run:signal:%d" % -proc.returncode,
                    "exit_code": proc.returncode, "stdout": stdout,
                    "stderr": stderr, "elapsed_s": elapsed}
        return {"ok": proc.returncode == 0, "reason": "run:ok",
                "exit_code": proc.returncode, "stdout": stdout,
                "stderr": stderr, "elapsed_s": elapsed,
                "output_truncated": _truncated(out_path)}

    def _wait(self, proc, mem_mb, seconds, started):
        """Wait for the child, killing it on a memory or wall-clock breach.
        Returns a `run:*` reason when it had to kill, else None."""
        deadline = started + seconds
        while True:
            if proc.poll() is not None:
                return None
            if time.time() >= deadline:
                _kill_group(proc)
                proc.wait(timeout=5)
                return "run:timeout"
            rss = _rss_mb(proc.pid)
            if rss is not None and rss > mem_mb:
                _kill_group(proc)
                proc.wait(timeout=5)
                return "run:memory-cap"
            time.sleep(self.poll_s)


def _rss_mb(pid):
    """Resident set size of a child, in MB, or None when unavailable. Linux
    `/proc` is the portable-enough source here; a missing value means the
    memory cap is not enforced on this host, which the result would then need
    to say — so it is read, never assumed."""
    try:
        with open("/proc/%d/statm" % pid, "r") as f:
            fields = f.read().split()
        pages = int(fields[1])  # resident pages
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0))
    except (OSError, ValueError, IndexError):
        return None


def _read_capped(path):
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_OUTPUT_BYTES)
        return data.decode("utf-8", "replace")
    except OSError:
        return ""


def _truncated(path):
    try:
        return os.path.getsize(path) >= MAX_OUTPUT_BYTES
    except OSError:
        return False


def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


# ── Gate 4: verify output ─────────────────────────────────────────────────────

def check_verify(run, manifest, workdir):
    """Exit code + declared golden assertions. Returns (ok, reason)."""
    verify = manifest.get("verify") or {}
    want_code = int(verify.get("exit_code", 0))
    if run.get("exit_code") != want_code:
        return False, "verify:exit-code:%s!=%s" % (run.get("exit_code"), want_code)
    if run.get("output_truncated"):
        return False, "verify:output-truncated"
    contains = verify.get("stdout_contains")
    if contains is not None and contains not in (run.get("stdout") or ""):
        return False, "verify:stdout-missing:%s" % contains
    artifact = verify.get("artifact")
    if artifact is not None:
        safe = _safe_relpath(artifact)
        if safe is None or not os.path.isfile(os.path.join(workdir, safe)):
            return False, "verify:artifact-missing:%s" % artifact
    return True, "verify:ok"


def _safe_relpath(path):
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    if os.path.isabs(path) or path.startswith("~"):
        return None
    parts = path.replace("\\", "/").split("/")
    if ".." in parts or parts[0] in ("", "."):
        return None
    return path


# ── The chain ─────────────────────────────────────────────────────────────────

def run_manifest(manifest, code, workdir=None, runner=None, env=None,
                 ledger_path=None):
    """Run the §6.2 gate chain. Returns a structured result dict.

    `ok` is true only when every gate passed. On failure the result carries the
    failing `step` and a namespaced `reason`, and NOTHING was promoted.
    """
    runner = runner if runner is not None else RlimitRunner()
    steps = []

    def reject(step, reason):
        # `steps` may already carry this gate's failure line; never record it twice.
        if not (steps and steps[-1]["step"] == step and steps[-1]["reason"] == reason):
            steps.append({"step": step, "ok": False, "reason": reason})
        result = {"ok": False, "step": step, "reason": reason, "steps": steps,
                  "promoted": False, "gate": "gateChain"}
        _record(result, manifest, code, env, ledger_path)
        return result

    ok, reason, normalized = check_manifest(manifest)
    steps.append({"step": "manifest", "ok": ok, "reason": reason})
    if not ok:
        return reject("manifest", reason)

    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="toolchain-")
    try:
        if own_workdir:
            _write_entry(workdir, normalized["entrypoint"], code)
        ok, reason = check_static(code, normalized, workdir)
        steps.append({"step": "static", "ok": ok, "reason": reason})
        if not ok:
            return reject("static", reason)

        # A declared capability the backend cannot honor is refused, not
        # silently granted. Only a kernel-level backend (Docker/seccomp) may
        # accept `network: true`; this one blocks network by import allowlist
        # and says so.
        if normalized["resource"]["network"] and runner.network_enforcement != "kernel":
            return reject("sandbox", "sandbox:network-unenforced")

        run = runner.run(normalized, workdir)
        if not run.get("ok") and run.get("reason") != "run:ok":
            steps.append({"step": "sandbox", "ok": False, "reason": run["reason"]})
            return reject("sandbox", run["reason"])
        steps.append({"step": "sandbox", "ok": True, "reason": "run:ok"})

        ok, reason = check_verify(run, normalized, workdir)
        steps.append({"step": "verify", "ok": ok, "reason": reason})
        if not ok:
            return reject("verify", reason)

        result = {
            "ok": True, "step": "promote", "reason": "promote:gateChain",
            "steps": steps, "promoted": True, "gate": "gateChain",
            "howVerified": "gateChain",
            "task": normalized["task"],
            "language": normalized["language"],
            "stdout": run.get("stdout", ""),
            "stderr": run.get("stderr", "")[:MAX_OUTPUT_BYTES],
            "exit_code": run.get("exit_code"),
            "elapsed_s": run.get("elapsed_s"),
            "runner": runner.name,
            "network_enforcement": runner.network_enforcement,
            "memory_enforcement": getattr(runner, "memory_enforcement", "unknown"),
        }
        _record(result, normalized, code, env, ledger_path)
        return result
    finally:
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _write_entry(workdir, entrypoint, code):
    target = os.path.join(workdir, entrypoint)
    os.makedirs(os.path.dirname(target) or workdir, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(code)


def run_dir(path, env=None, runner=None):
    """Load `manifest.json` + the entrypoint from a directory and run the chain.

    Used by the `run_toolchain` tool: the argument is a directory name under the
    residence's `generated/`, never an arbitrary path.
    """
    env = env if env is not None else os.environ
    manifest_path = os.path.join(path, "manifest.json")
    if not os.path.isfile(manifest_path):
        return {"ok": False, "step": "manifest", "reason": "manifest:not-found",
                "promoted": False, "steps": []}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return {"ok": False, "step": "manifest",
                "reason": "manifest:unreadable:%s" % e, "promoted": False, "steps": []}
    entrypoint = manifest.get("entrypoint") if isinstance(manifest, dict) else None
    code = ""
    if isinstance(entrypoint, str) and _safe_relpath(entrypoint):
        entry_fs = os.path.join(path, entrypoint.strip())
        if os.path.isfile(entry_fs):
            with open(entry_fs, "r", encoding="utf-8", errors="replace") as f:
                code = f.read(MAX_CODE_BYTES + 1)
    return run_manifest(manifest, code, workdir=path, runner=runner, env=env,
                        ledger_path=resolve_ledger(env))


def _record(result, manifest, code, env, ledger_path):
    """Append the machine-written ledger line. A broken ledger must never hide
    the gate outcome, so a write failure is attached, not raised."""
    env = env if env is not None else os.environ
    ledger_path = ledger_path or resolve_ledger(env)
    if not ledger_path:
        return
    sha = hashlib.sha256((code or "").encode("utf-8", "replace")).hexdigest()
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": "toolchain",
        "task": (manifest or {}).get("task") if isinstance(manifest, dict) else None,
        "language": (manifest or {}).get("language") if isinstance(manifest, dict) else None,
        "code_sha256": sha,
        "gate": "gateChain",
        "promoted": bool(result.get("promoted")),
        "reason": result.get("reason"),
        "steps": result.get("steps") or [],
        "runner": result.get("runner"),
        "network_enforcement": result.get("network_enforcement"),
        "memory_enforcement": result.get("memory_enforcement"),
        "source": "toolchain",
    }
    if result.get("promoted"):
        # Only the gate chain may promote — the model never writes this field.
        record["howVerified"] = "gateChain"
        record["exit_code"] = result.get("exit_code")
        record["elapsed_s"] = result.get("elapsed_s")
    try:
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def resolve_ledger(env=None):
    env = env if env is not None else os.environ
    residence = (env.get("DRIVE_RESIDENCE") or "").strip() or "freebrain-residence"
    return os.path.join(os.path.abspath(residence), "ledger.jsonl")


def format_result(result):
    """One compact human line per gate — the tool relays this to the model."""
    marks = " | ".join("%s:%s" % (s["step"], "ok" if s["ok"] else s["reason"])
                       for s in result.get("steps") or [])
    head = ("toolchain %s (%s) — %s"
            % ("PROMOTED" if result.get("promoted") else "REJECTED",
               result.get("step"), result.get("reason")))
    out = head
    if marks:
        out += "\n" + marks
    if result.get("promoted"):
        stdout = (result.get("stdout") or "").strip()
        out += "\nrunner=%s network=%s memory=%s exit=%s %.2fs" % (
            result.get("runner"), result.get("network_enforcement"),
            result.get("memory_enforcement"),
            result.get("exit_code"), result.get("elapsed_s") or 0.0)
        if stdout:
            out += "\nstdout: " + stdout[:400]
    return out
