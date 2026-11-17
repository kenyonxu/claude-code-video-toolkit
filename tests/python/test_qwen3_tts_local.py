"""qwen3_tts local provider branch: guards, payload and result handling.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py -v

Every cloud call is faked. Nothing here contacts 127.0.0.1 or any other host.
"""
from __future__ import annotations

import base64
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402


class LocalSetupGuardTests(unittest.TestCase):
    def test_setup_cloud_local_exits_2_with_hint_and_no_cloud_setup(self):
        argv = ["qwen3_tts.py", "--setup", "--cloud", "local"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(
                    qwen3_tts, "setup_runpod",
                    side_effect=AssertionError("setup_runpod must not run")) as runpod, \
                mock.patch.object(
                    qwen3_tts, "setup_modal",
                    side_effect=AssertionError("setup_modal must not run")), \
                mock.patch("cloud_gpu.ProgressReporter"), \
                mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                qwen3_tts.main()
        self.assertEqual(ctx.exception.code, 2)
        runpod.assert_not_called()
        text = err.getvalue()
        self.assertIn("start_gateway.bat", text)
        self.assertIn("LOCAL_TTS_ENDPOINT_URL", text)
        self.assertIn("127.0.0.1:8123", text)


if __name__ == "__main__":
    unittest.main()
