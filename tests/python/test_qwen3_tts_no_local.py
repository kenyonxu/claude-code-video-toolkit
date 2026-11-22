"""qwen3_tts: local is rejected before storage, network or reference upload."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402


class NoLocalRouteTests(unittest.TestCase):
    def reject(self, **kwargs):
        with mock.patch("qwen3_tts.get_r2_payload_config") as r2_config, \
                mock.patch("qwen3_tts.upload_to_storage") as upload, \
                mock.patch("cloud_gpu.call_cloud_endpoint") as call:
            result = qwen3_tts.generate_audio(
                text="hello", output_path="unused.mp3", cloud="local",
                verbose=False, **kwargs)
        self.assertFalse(result["success"])
        self.assertIn("confucius4", result["error"])
        r2_config.assert_not_called()
        upload.assert_not_called()
        call.assert_not_called()

    def test_custom_voice_local_is_rejected_before_r2_or_http(self):
        self.reject()

    def test_clone_local_is_rejected_before_reference_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "ref.wav"
            reference.write_bytes(b"reference")
            self.reject(ref_audio=str(reference), ref_text="hello")


if __name__ == "__main__":
    unittest.main()
