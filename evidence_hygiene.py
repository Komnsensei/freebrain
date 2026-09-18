#!/usr/bin/env python3
"""
evidence_hygiene.py — separate real measurements from test artifacts in the Q1
evidence log.

WHY THIS EXISTS
---------------
`freebrain-residence/evidence/q1-evidence.jsonl` is the auto-captured Q1 evidence
log: one JSONL record per model step, with the endpoint that served it. It is
also, unavoidably, the file a test run writes to — the default residence is a
relative path resolved from the cwd, so running a suite from the repo root
appends stub-provider records straight into the published record.

Measured 2026-09-18: of 5,894 rows, **5,788 were test artifacts** (98%). The
stub replies carry a real-looking `provider: local` and a plausible
`tokens_per_s` (160 tok/s from an in-process stub), so nothing about them looks
wrong in the file — and any throughput figure computed over it is fiction.

`test_support.py` now stops the leak. This tool cleans up what the leak left, and
is safe to re-run at any time.

HOW IT CLASSIFIES
-----------------
It marks a row as an artifact only on **positively identified stub markers**:

  * `provider: "stub"` (the stub provider names itself)
  * an endpoint that only a test stub uses (`http://stub/v1`, `http://x`,
    `http://127.0.0.1:1/v1`)
  * a model id that only a test stub uses (`stub`, `stub-model`, `m`)

It deliberately does NOT use "endpoint is not one I recognise" as an artifact
test. A brand-new real backend must never be quarantined by a tool that simply
hasn't heard of it. Unknown rows stay in the evidence log: false-keeping a stub
is a small, visible error, and false-quarantining a measurement is a silent one.

USAGE
-----
    python3 evidence_hygiene.py                    # report only (default)
    python3 evidence_hygiene.py --split            # move artifacts aside
    python3 evidence_hygiene.py --file PATH        # a different evidence log

`--split` never deletes: artifacts move to `test-artifacts.jsonl` next to the
evidence log, appended, with their original bytes.
"""

import argparse
import collections
import json
import os
import sys

DEFAULT_RESIDENCE = os.environ.get("DRIVE_RESIDENCE") or "freebrain-residence"
DEFAULT_FILE = os.path.join(DEFAULT_RESIDENCE, "evidence", "q1-evidence.jsonl")

STUB_PROVIDERS = {"stub"}
STUB_MODELS = {"stub", "stub-model", "m", ""}
STUB_ENDPOINTS = {"http://stub/v1", "http://stub", "http://x", "http://127.0.0.1:1/v1"}


def classify(rec):
    """Return 'artifact' or 'measurement', plus the marker that decided it."""
    if (rec.get("provider") or "") in STUB_PROVIDERS:
        return "artifact", "provider=stub"
    for key in ("provider_url", "url"):
        ep = (rec.get(key) or "").strip()
        if ep in STUB_ENDPOINTS or ep.startswith("http://stub"):
            return "artifact", "stub endpoint"
    if (rec.get("model") or "") in STUB_MODELS:
        return "artifact", "stub model"
    return "measurement", ""


def load(path):
    rows, broken = [], []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                rows.append((json.loads(s), s, i))
            except ValueError:
                broken.append((s, i))
    return rows, broken


def report(path):
    rows, broken = load(path)
    verdicts = collections.Counter()
    markers = collections.Counter()
    by_model = collections.Counter()
    for rec, _s, _i in rows:
        v, why = classify(rec)
        verdicts[v] += 1
        if v == "artifact":
            markers[why] += 1
        else:
            by_model[(rec.get("provider"), rec.get("model"))] += 1

    total = len(rows)
    print(f"evidence log : {path}")
    print(f"rows         : {total}" + (f"   (+{len(broken)} unparseable)" if broken else ""))
    print()
    print(f"  measurements : {verdicts['measurement']}"
          + (f"   ({100.0 * verdicts['measurement'] / total:.1f}%)" if total else ""))
    print(f"  artifacts    : {verdicts['artifact']}"
          + (f"   ({100.0 * verdicts['artifact'] / total:.1f}%)" if total else ""))
    if markers:
        print("\n  artifact markers:")
        for m, n in markers.most_common():
            print(f"    {n:>6}  {m}")
    if by_model:
        print("\n  measurements by provider/model:")
        for (p, m), n in by_model.most_common():
            print(f"    {n:>6}  {p} / {m}")
    return verdicts["measurement"], verdicts["artifact"]


def split(path):
    rows, broken = load(path)
    artifacts = os.path.join(os.path.dirname(path), "test-artifacts.jsonl")
    keep, move = [], []
    for rec, s, _i in rows:
        (move if classify(rec)[0] == "artifact" else keep).append((s, rec))

    if not move:
        print("nothing to split — the evidence log is already clean")
        return 0

    # Artifacts are preserved, not deleted: they are the record of a bug.
    with open(artifacts, "a", encoding="utf-8") as f:
        for s, _r in move:
            f.write(s + "\n")
    with open(path, "w", encoding="utf-8") as f:
        for s, _r in keep:
            f.write(s + "\n")

    print(f"moved   {len(move):>6} artifact rows -> {artifacts}")
    print(f"kept    {len(keep):>6} measurement rows in {path}")
    if broken:
        print(f"warning: {len(broken)} unparseable lines were written back verbatim"
              " — inspect them", file=sys.stderr)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Q1 evidence-log hygiene")
    p.add_argument("--file", default=DEFAULT_FILE,
                   help="evidence log (default: %(default)s)")
    p.add_argument("--split", action="store_true",
                   help="move test artifacts to test-artifacts.jsonl (default: report only)")
    args = p.parse_args(argv)

    if not os.path.isfile(args.file):
        print(f"no evidence log at {args.file}", file=sys.stderr)
        return 2
    if args.split:
        return split(args.file)
    report(args.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
