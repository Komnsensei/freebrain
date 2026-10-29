from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import builderbro_worker as worker


class BuilderBroWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "builderbro_mcp.py").write_text("# test stub\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_status_reports_extractive_only_and_allowlist(self):
        result = worker.status(str(self.root), {})
        self.assertTrue(result["ok"])
        self.assertFalse(result["live_generation_enabled"])
        self.assertIn("verify_claim", result["allowed_tools"])

    def test_unknown_tool_is_rejected_before_process_spawn(self):
        with patch.object(worker.subprocess, "run") as run:
            with self.assertRaisesRegex(worker.WorkerError, "allowlist"):
                worker.call_tool("run_shell", {}, builderbro_root=str(self.root), environ={})
            run.assert_not_called()

    def test_live_rag_is_rejected_before_process_spawn(self):
        with patch.object(worker.subprocess, "run") as run:
            with self.assertRaisesRegex(worker.WorkerError, "extractive"):
                worker.call_tool("rag_ask", {"question": "test", "generator": "live"},
                                 builderbro_root=str(self.root), environ={})
            run.assert_not_called()

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaisesRegex(worker.WorkerError, "timeout_seconds"):
            worker.call_tool("memory_recall", {"goal": "test"},
                             builderbro_root=str(self.root), timeout_seconds=0, environ={})

    def test_valid_call_passes_no_shell_and_parses_json(self):
        def fake_run(command, **kwargs):
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["cwd"], str(self.root))
            self.assertNotIn("GROQ_API_KEY", kwargs["env"])
            self.assertEqual(command[1:4], [str(self.root / "builderbro_mcp.py"), "--call", "verify_claim"])
            return type("Completed", (), {
                "stdout": json.dumps({"ok": True, "level": "observed"}),
                "stderr": "",
                "returncode": 0,
            })()
        result = worker.call_tool("verify_claim", {"tool": "read_file", "output": "ok", "expect": "contains:ok"},
                                  builderbro_root=str(self.root), environ={"PATH": "/bin", "GROQ_API_KEY": "secret"},
                                  runner=fake_run)
        self.assertTrue(result["transport_ok"])
        self.assertEqual(result["worker"], "freebrain-builderbro")
        self.assertEqual(result["level"], "observed")

    def test_missing_checkout_fails_closed(self):
        with self.assertRaisesRegex(worker.WorkerError, "root not found"):
            worker.resolve_builderbro_root(str(self.root / "missing"), {})


if __name__ == "__main__":
    unittest.main()
