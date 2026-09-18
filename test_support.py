#!/usr/bin/env python3
"""test_support.py — keep the shipped residence out of the tests' blast radius.

WHY THIS EXISTS
---------------
`agent_runtime.residence_path()` resolves the default home as a *relative* path
from the current working directory. So a suite run from the repo root — which is
how every suite is documented to be run — wrote its stub-provider records
straight into the published record:

    freebrain-residence/evidence/q1-evidence.jsonl

Measured 2026-09-18: **3,430 of that file's 5,894 rows were test artifacts**
(`provider: "stub"`), i.e. 58% of the published Q1 evidence log was never a
measurement. Any mean tokens/s taken over it was meaningless, and nothing warned.

Q1's evidence *is* the deliverable, so a test must not be able to append to it.
Each suite calls `isolate_residence()` at import time, which points
`DRIVE_RESIDENCE` at a throwaway temp dir for the life of the process.

An explicit `DRIVE_RESIDENCE` already in the environment still wins, and tests
that set their own home (and restore it) are unaffected — the point is only that
*the default* is never the shipped residence.

Run: imported by the suites; nothing to run here.
"""

import atexit
import os
import shutil
import tempfile

_INSTALLED = "FREE_BRAIN_TEST_RESIDENCE"


def isolate_residence():
    """Point the default residence at a temp dir. Idempotent; safe to call from
    every suite. Returns the residence path now in effect."""
    if not os.environ.get(_INSTALLED):
        os.environ[_INSTALLED] = "1"
        if not os.environ.get("DRIVE_RESIDENCE"):
            path = tempfile.mkdtemp(prefix="freebrain-test-residence-")
            os.environ["DRIVE_RESIDENCE"] = path
            # A test run should leave nothing behind, including its own home.
            atexit.register(shutil.rmtree, path, True)
    return (os.environ.get("DRIVE_RESIDENCE") or "").strip()
