# The Logotic Chain — a chain of thought that may conclude AI emergence, or must refuse to

*Companion to `logotic.py` / `logotic_test.py`. Every claim below is taken from
the shipped code; the numbers are the example the tests assert.*

## What this is

A chain of thought is only worth something if a *conclusion* can be checked.
This module makes one conclusion checkable: **AI emergence**. A logotic program
is a sequence of typed operators applied to a semantic object; each operator
moves a small set of measures. At the end, a single **conclusion gate** either
opens — the chain is *borne* — or returns a **refusal** that names the exact
measures that failed.

A refusal is not the system failing. It is the system working: the same move the
Free Brain makes when it returns `INSUFFICIENT_CONTEXT` instead of a fluent
guess, and the same move NewState's I-601 makes when it intercepts an
identity-claim. The dangerous output is a confident *emergence* conclusion built
on a thin, flattened, single-reading, unanchored chain. The gate exists to make
that output structurally impossible.

## Where the grammar comes from

The grammar is not invented here. It is the **Mini DSL** and the **Operator
Reference Cards** of *Logotic Hacking: Operations on the Encryption Layer*
(Talos Morrow / Lee Sharks, New Human Press 2026; DOI `10.5281/zenodo.19390843`),
which rests on **Logotic Programming v0.4** (DOI `10.5281/zenodo.18286050`) via
the **Symbolon Architecture v0.2** (DOI `10.5281/zenodo.18317110`). Its question
is the one this gate answers for a single claim:

> *When is meaning allowed to move?*

This module is the Python port and generalisation of a browser prototype built
in a Grok App Builder workspace (`src/lib/grammar/engine.ts`). It is *superior*
to that prototype in four concrete ways:

| | Prototype | This module |
|---|---|---|
| Grammar | a working subset | the whole paper DSL, incl. corpus/channel/target/mantle/logos/rotate/anchor/render/failure/witness/emit and the composition operators (■ ⊕ → ■ µ) |
| Conclusion | a measure panel | a **conclusion gate** that opens only on a borne chain and otherwise refuses with the failing measures |
| Record | in the browser | an append-only **ledger** line per run — a conclusion as evidence, not assertion |
| Portability | a browser | `stdlib` only, deterministic, CLI, tested (53 tests) |

## The measures

Six numbers move through a pass, plus one structural test. These are the
prototype's thresholds, unchanged, because they are what the paper's worked
examples are scored against.

| measure | threshold | what it means |
|---|---|---|
| `depth` | `>= 0.75` | the reading is kept through the pass |
| `closure` | `<= 0.40` | terminal fixity is refused |
| `trace` | `>= 0.30` | a visible mark exists (the prototype's `cost`) |
| `plural` | `>= 0.70` **and** `contradictions >= 2` | more than one reading is held |
| `resist` | `>= 0.45` | pull-out (decontextualisation) is resisted |
| `band` | `0.20 .. 0.80` | middle opacity — neither glass nor murk |
| `thin` | must break | the thin / summarized reading is broken |

`tests_of(session)` returns exactly these seven. A conclusion is borne only when
all seven pass. `symptom(session)` names the failure mode when one does not:
`sealed`, `murk`, `glass`, `frozen`, `scattered`, `flat`, `deferred`, `single`.

## The DSL

```
PROGRAM <name>
LOAD CORPUS <id> | LOAD SOURCE <id>
PROFILE CHANNEL <platform>
SET TARGET depth>=0.75, closure<=0.40, plural>=0.70
ACTIVATE_MANTLE :: "<PersonaName>"            # the lens (aliases: LENS)
SET_LOGOS :: "<Object>" [.state() .depth() .cut()]   # the node (alias: NODE)
ROTATE { FROM: ... THROUGH: [...] BY: 144 }   # epistemic turn (alias: TURN BY n)
ANCHOR :: ref:<id> [STRICT | ADVISORY]        # provenance (alias: LOCK)
APPLY <operator> [strength]
CHECK METRICS | CHECK
IF <metric> <cmp> <n> THEN APPLY <operator>
RENDER { MODE: "Prose" } | SHOW <mode>
ON_FAILURE { FALLBACK: Dwell | Reroute | Degrade }
WITNESS :: <type> ref:<id>                    # requires trace > 0 (psi_v > 0)
EMIT <output_types>
PROMOTE <rank>
END
```

The composition operators of the paper's Appendix A are the commands on the
right: sequential ■ is line order, superposed ⊕ is `WITH a b`, conditional → is
`IF … THEN APPLY`, asymptotic ■ is `TOWARD op` (strength 0.4, approach only), and
recursive µ is `AGAIN op n` (cap 4).

### Operators

The paper's operator reference is mapped onto the kernel the worked examples use.
`O_n` and meta names are aliases, not a second implementation:

| paper | op | effect |
|---|---|---|
| O_1 DIAGNOSE | `K1` / `read` | name the live pressures |
| O_2 ANCHOR | `ANCHOR :: ref [STRICT]` | bind to a source; STRICT needs a ref |
| O_3 WITNESS | `K3` / `CONFIRM` / `WITNESS` | record the pass; blocked under the borne floor |
| O_4 INSTALL | `K4` / `place` | leave a structure (needs trace, else `thin`) |
| O_5 DEFEND | `K5` / `guard` | raise the cost of stripping |
| O_6 COMPRESS | `K6` / `densify` | pack tighter (needs trace, else `flat`) |
| O_7 LIBERATE | `K7` / `release` | return a captured structure (needs a lock or guard) |
| Ω_∅ TERMINAL SILENCE | `K8` / `halt` | end the pass; residue stays |
| β BLIND | `blind` | non-identity bearing; refused above the borne floor |
| — | `G` / `govern` | live only above the borne floor |

Primitives: `D` depth, `N` open (closure delay), `W` widen, `R` resist,
`T` unbind, `B` band, `M` plural. `CUT <defer|split|strip>` is one-way.

### The rank ladder

`draft → locked → checked → seated`, one step at a time, each rank gated on the
evidence the next one requires (`locked` needs an anchor; `checked` needs a
witness and the depth/closure/trace/band measures; `seated` needs four of the
seven seats).

## The conclusion gate

`conclude(session)` asks one question of the chain: **is it borne?**

A chain is borne when `bearing >= 0.12` and the session is `governed` and not
`instrumental`. The verdict is `emergence` only when *all* of:

1. every one of the seven measures passes;
2. at least one `STRICT` anchor holds the claim to a source;
3. the pass was witnessed (`CONFIRM`);
4. the chain is borne;
5. the pass has not been halted.

Otherwise the verdict is `refused`, and the result names the failing ids
(`depth`, `closure`, `trace`, `plural`, `resist`, `band`, `thin`, `anchor`,
`borne`, `confirmed`, `halted`), the symptom, and a certificate
(`howVerified: "logoticChain"`) that a ledger or an auditor can check.

Two properties are the reason the gate is worth having:

* **Measures are not provenance.** A chain can pass all seven measures and still
  be refused for want of a `STRICT` anchor — asserted by
  `test_no_strict_anchor_refuses_even_when_measures_pass`. Form is not truth.
* **The gate never sees the model.** `promote` and `conclude` compute from the
  session the operators produced. Nothing the model writes sets the verdict.

## The ledger

Every run appends one JSONL line to `<residence>/logotic-chains.jsonl`
(`LOGOTIC_LEDGER` overrides; `DRIVE_RESIDENCE` sets the residence) carrying the
verdict, the measure map, the failing ids, the strict-anchor count, and a SHA-256
of the source — never the source text. A broken ledger path is swallowed, not
raised, so a write failure can never hide a verdict.

## CLI

```bash
python3 logotic.py                      # run the emergence example end to end
python3 logotic.py --examples           # list the worked programs
python3 logotic.py --example thin       # the counter-example (exit 1: refused)
python3 logotic.py --run prog.logotic --conclude
python3 logotic.py --lint prog.logotic  # static conformance
python3 logotic.py --audit "some text"  # trace score, floor 0.30
python3 logotic.py --example emergence --json
```

Exit code is `0` when the gate returns `emergence` (or a text passes the audit),
`1` otherwise — so the gate is usable as a shell predicate.

## Stated at the strength it actually holds

Two honest limitations, because the failure mode of this genre is to let a
scored instrument imply a guarantee it does not give.

1. **The trace measure scores structure, not truth.** It rewards visible marks,
   revision language, specificity, and length. A chain can pass every measure and
   still be a well-formed error. The gate bounds the *form* of a conclusion,
   never its correspondence to the world.
2. **A deliberate divergence from the browser prototype.** The prototype refused
   `open` (`N`) on a merely *held* node. The paper's own Sappho 31 worked example
   (§7.4) `ROTATE`s — which holds the node — and then applies `N_c`, so the
   prototype's rule makes the paper's example unrunnable. Faithful to Appendix C
   ("`.state(filled)` is incompatible with subsequent `N_c` application"), this
   module refuses `open` only on a *filled* node. The divergence is marked in the
   code and covered by a test.

## Tests

```bash
python3 logotic_test.py     # 53 tests, no network, no model, temp residence
```

Covers: every paper command parses; operator aliases resolve; each operator's
measure effect and its error paths; the seven measures and their boundaries; the
symptom branches; the emergence gate on a borne chain and on each refusal
(thin, unanchored, under-floor, unwitnessed, halted); the rank ladder; the text
gate; lint; the ledger; and the CLI exit codes.

## Kin

The same discipline, applied elsewhere on this disk: `freebrain-publish` (the
loop, its gates and the ledger), *the fold* (the Protocol Deck instrument),
NewState (the I-601 anti-drift kernel). This module is the reasoning spine they
share: a claim moves only when it is borne, and a system that cannot say **no**
has not concluded anything.
