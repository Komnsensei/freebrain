# P1 — The verified action plane

*Companion to `FREE-BRAIN.md` §6.1/§6.2/§8.2. Written 2026-10-07, after the code
landed, so every claim below is a measurement taken from the shipped path rather
than a design intention.*

## Why this phase, and not another

The brief's novelty claim is **memory-bounded execution of model-generated code**.
Before this phase the loop's tools were read-only by construction, so the
completion gate had only ever verified *a fact about the world*, never *a change to
it* — FREE-BRAIN.md §8.2 says so in its own words. Everything downstream already
assumed the missing component:

* the manifest contract (§6.1) — prose only, no schema code;
* the gate chain (§6.2) — prose only, no gate code;
* the 2 GB / 30 s caps, `generated/` dirs, and the drift loop's guard-scoped
  `max_mem` / `sandbox` concepts — specced, never instantiated;
* Q2 (malformed-manifest rejection) and Q3 (isolation escapes) — unmeasurable,
  because there was nothing to measure against.

So the highest-leverage move was not a new feature. It was instantiating the one
component the rest of the design already presumed, in a form small enough to build
with the repo's stdlib-only discipline and reuse its existing semantics
(`loop_guard.fail`, the ledger, the breaker, the two-constructor failure rule).

## What shipped

| File | Role |
| --- | --- |
| `toolchain.py` | The gate chain: `check_manifest` → `check_static` → `RlimitRunner.run` → verify → promote; `run_manifest` / `run_dir`; the ledger record. |
| `toolchain_test.py` | 30 deterministic tests: the manifest-fuzz set (Q2), the escape set (Q3), cap enforcement, ledger, and the tool-scope contract. |
| `agent_runtime.py` | `run_toolchain` registered in `TOOLS` with a declared probe; the system prompt tells the model the tool exists. |

The chain, unchanged from §6.2, now runs:

```
generate (volatile)
  → parse manifest          (schema check; REQUIRED_FIELDS)
  → static analyze code     (ast.parse; dep allowlist; banned imports/calls; 64 KiB cap)
  → sandboxed run           (rlimits + parent-side memory/clock kill; no network)
  → verify output           (exit code + declared stdout/artifact assertions)
  → promote to invariant    (ledger entry with howVerified: "gateChain")
```

Two properties are enforced rather than intended, because the repo's own history
shows both rot silently:

* **One failure constructor.** Every rejection is a `[gate-failed] step=… reason=…`
  string from `loop_guard.fail`. The loop guard counts it; the model never receives
  raw failed output to smooth over (§6.2, "One constructor for every failure").
* **Only the gate chain promotes.** The model can write into
  `generated/<name>/`, but `promoted` and `howVerified` are set by `run_manifest`,
  never read from anything the model writes.

## Enforcement — stated at the strength it actually holds

This is the part worth being pedantic about, because "sandboxed" is the kind of word
that gets over-claimed.

| Bound | Mechanism | Strength | Recorded as |
| --- | --- | --- | --- |
| CPU-seconds | `RLIMIT_CPU` (SIGXCPU) | kernel | — |
| Wall clock | parent kills the process group past `max_seconds` | enforced by parent | `run:timeout` |
| Memory | parent polls child RSS, kills the group on breach | **host-dependent** | `memory_enforcement: "rss-poll"` |
| Output / FS size | `RLIMIT_FSIZE` (SIGXFSZ) | kernel | — |
| Descriptors | `RLIMIT_NOFILE` (64) | kernel | — |
| No fork / no children | `RLIMIT_NPROC` (1) **and** static blocklist | kernel + static | — |
| Network | import blocklist (`socket`, `urllib`, `http`, …) | **not a kernel guarantee** | `network_enforcement: "import-blocklist"` |

Two honest limitations, both discovered by measurement on the target rig and both
propagated into the result record rather than papered over:

1. **Memory is not a kernel cap here.** `RLIMIT_AS` hangs CPython startup on this
   Termux/Android kernel and `RLIMIT_DATA` does not bound `mmap`, so neither is
   used. RSS is polled from the parent instead. That is a real bound with a real
   race window, and the ledger says `rss-poll` rather than implying a guarantee the
   host does not provide.
2. **Network isolation is a blocklist, not a namespace.** `RlimitRunner` cannot
   give kernel-level isolation, so `check_manifest` *refuses* a manifest that
   declares `network: true` instead of silently granting it, and the record names
   the mechanism. The Docker/seccomp runner is the intended second backend;
   `run_manifest` takes a `runner`, so that is a swap, not a rewrite.

`test_the_runner_never_claims_a_kernel_guarantee_it_lacks` asserts this. A backend
that quietly overstates its isolation is the one failure mode here that would
propagate into every downstream Q2/Q3 number.

## The substrate fix that had to come first

Three of the five suites were red on a clean checkout, and the repo's own "all
green" claim was therefore false. One root cause, from a commit that touched only
`brain_cascade.py`: `chain()` began defaulting `skip_empty_local=True` and
live-probing `127.0.0.1:11434`, while the suites passed isolated env dicts that
omitted `BRAIN_SKIP_EMPTY_LOCAL`. With cloud keys present the local hop was dropped
and every assertion expecting `["local", "groq", …]` failed — the tests were never
updated. `run_cascade` had also lost its provider-attribution block, so a
serving-provider field the tests asserted no longer existed.

Fixed by restoring the attribution and pinning the environment in the suites. This
wasn't cleanup — it was a prerequisite: P1 cannot be verified by a harness whose
green light is already broken.

### A second leak, found by the verification itself

Running `python3 loop_audit.py --goals` from the repo root appended **144 rows of
`provider: "stub"` test data to the published
`freebrain-residence/evidence/q1-evidence.jsonl`** — the exact leak `test_support.py`
exists to stop, but on the one path that guard did not cover: `loop_audit.py` is a
CLI, not a suite, so nothing isolated its residence, and `agent_runtime._emit_step`
records every step. Q1's evidence *is* the deliverable, and a stubbed-provider
throughput row is not a measurement of anything.

Fixed at the cause: `loop_audit.py` now calls `test_support.isolate_residence()` at
import, so a stub audit cannot touch the published log. Verified: the file is
byte-identical before and after a `--goals` run, and `--register` is unaffected
(the thresholds file and `SELF_IMPROVEMENT_LOG.md` are cwd-relative, not
residence-relative — confirmed by running it from a temp cwd). The 144 accidental
rows were reverted.

## Verification (run 2026-10-07)

```
loop_guard_test        exit=0  Ran 56 tests  OK
brain_cascade_test     exit=0  Ran 58 tests  OK
agent_runtime_test     exit=0  Ran 37 tests  OK
drift_loop_test        exit=0  Ran 40 tests  OK
toolchain_test         exit=0  Ran 30 tests  OK
```

`toolchain_test` is run under `-W error::ResourceWarning` (the suite must not leak
its own file handles). The Q2/Q3 instrument maps onto the brief's suite table:

| Brief suite | Test class | Coverage |
| --- | --- | --- |
| Manifest fuzz (Q2) | `ManifestGateTest` | malformed / over-privileged / raised-bound / undeclared manifests — all rejected at the schema gate, reason names the field |
| Static gate | `StaticGateTest` | parse failure, every banned import, capability calls, size cap, dep allowlist, non-enabled runtime |
| Escape attempts (Q3) | `SandboxTest` | wall-clock, memory bomb, output flood, declared-network refusal, "output is never instructions", no-overstated-guarantee |
| Verify / promote | `VerifyGateTest`, `LedgerTest` | nonzero exit, stdout assertion, missing artifact, unknown verify key; promotion vs volatile record, unwritable-ledger path |
| Tool scope | `ToolScopeTest` | path traversal, missing dir, canonical failure shape, probe, **loop-call shape** |

End-to-end through the interface the loop actually uses — `TOOLS[name](arg)` with
one argument, as `run_goal` dispatches it — a valid tool-chain promotes and writes:

```json
{"event": "toolchain", "promoted": true, "howVerified": "gateChain",
 "reason": "promote:gateChain", "memory_enforcement": "rss-poll",
 "network_enforcement": "import-blocklist",
 "code_sha256": "5a69585d…"}
```

`test_the_loop_call_shape_resolves_the_process_residence` exists because every other
test passed `env=` explicitly: without it the tested call path and the shipped call
path are two different code paths, and the env one could rot unseen.

## What this does *not* yet close

* **Q2/Q3 are instruments, not results.** The suites prove the gates reject the
  cases we thought of. A 100% rejection rate over a hand-written fuzz set is a
  floor, not a ceiling — the adversarial set needs to grow, and the loop should
  eventually *generate* hostile manifests for the gates to reject.
* **The task suite still measures only read-only tasks.** The codegen tasks that
  would exercise Q2/Q3 in the loop are not wired in yet. (Doc drift found while
  wiring this up: FREE-BRAIN.md §8.2 cites `autonomy_suite.py` for its eight-floor
  report, but that file is not in the repo. The runnable goal-audit instrument is
  `loop_audit.py --goals` — 18 scripted models, 0 false successes, 1 recorded false
  refusal, verified here — and it reports a different, smaller table than §8.2
  claims. The eight-floor numbers have no file that produces them today.)
* **One runtime.** Only `python3` executes; `node`/`rust`/`bash` are allowlisted in
  the schema but rejected by the static gate as `runtime-not-enabled` — a
  structured failure, not a silent skip.

## Next, in dependency order

| Phase | Work | Exit criterion |
| --- | --- | --- |
| **P2** | Wire codegen tasks into the goal-audit suite (`loop_audit.py --goals`, or the missing `autonomy_suite.py` if §8.2's floors are to be made real); emit a real Q2/Q3 metric into the drift ledger per run. | completion rate measured, not asserted |
| **P3** | Second backend: Docker/seccomp runner behind the same `run_manifest(runner=…)` seam, giving kernel-level network + memory isolation. | the `network_enforcement` / `memory_enforcement` fields read `seccomp`/`cgroup` instead of blocklist/rss-poll |
| **P4** | Reuse the RAG citation-fidelity gate for generated artifacts: a claim about output must cite a retrieved source or the run is a structured failure. | the two halves of the repo combine; a fabricated artifact cannot promote |

Cheaper alternative if external leverage is wanted over closing the thesis:
productize `openchat`'s audit as an MCP server so any agent gets cited-or-refuses
answers; and reconcile the doc metric drift (`rag_config.json` /
`SELF_IMPROVEMENT_LOG.md` vs README vs `SKILL.md`) — the repo's own discipline says
an unbacked number is a defect.
