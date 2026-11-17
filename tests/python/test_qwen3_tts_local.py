"""qwen3_tts local provider branch: guards, payload and result handling.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py -v

Every cloud call is faked. Nothing here contacts 127.0.0.1 or any other host.
"""
from __future__ import annotations

import base64
import io
import sys
import tempfile
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


def run_generate(text, output, ref_path, cloud_output=None, **kwargs):
    """Run qwen3_tts.generate_audio with a faked cloud call.

    Returns (result, captured, up, dl_r2, dl_url); captured holds the
    provider, payload and timeout seen by the faked call_cloud_endpoint.
    """
    if cloud_output is None:
        cloud_output = {"success": True, "outputs": [{
            "audio_base64": base64.b64encode(b"MP3DATA").decode("ascii"),
            "duration_seconds": 1.25,
        }]}
    captured = {}

    def fake_call(provider, payload, tool_name, timeout, **rest):
        captured["provider"] = provider
        captured["payload"] = payload
        captured["timeout"] = timeout
        return cloud_output, 0.5

    with mock.patch("cloud_gpu.call_cloud_endpoint", side_effect=fake_call), \
            mock.patch.object(qwen3_tts, "upload_to_storage") as up, \
            mock.patch.object(qwen3_tts, "download_from_r2") as dl_r2, \
            mock.patch.object(qwen3_tts, "download_from_url") as dl_url, \
            mock.patch.object(qwen3_tts, "get_r2_payload_config",
                              return_value=None):
        result = qwen3_tts.generate_audio(
            text=text, output_path=output, ref_audio=str(ref_path),
            ref_text=kwargs.pop("ref_text", "参考"), verbose=False, **kwargs)
    return result, captured, up, dl_r2, dl_url


class LocalPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_local_clone_payload_is_base64_only(self):
        result, captured, up, dl_r2, dl_url = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            model="qwen3-tts-1.7B")
        self.assertTrue(result["success"], result)
        payload = captured["payload"]["input"]
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["ref_audio_base64"],
                         base64.b64encode(b"REFERENCE-AUDIO").decode("ascii"))
        self.assertEqual(payload["ref_text"], "参考")
        self.assertEqual(payload["model"], "qwen3-tts-1.7B")
        self.assertNotIn("ref_audio_url", payload)
        self.assertNotIn("r2", payload)
        self.assertNotIn("speaker", payload)
        up.assert_not_called()
        dl_r2.assert_not_called()
        dl_url.assert_not_called()
        self.assertEqual(captured["provider"], "local")

    def test_local_clone_without_model_omits_model(self):
        result, captured, up, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local")
        self.assertTrue(result["success"])
        self.assertNotIn("model", captured["payload"]["input"])

    def test_local_timeout_scales_with_batch(self):
        _, captured, _, _, _ = run_generate(
            ["一", "二", "三", "四", "五"],
            [str(self.out)] * 5, self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [
                {"audio_base64": base64.b64encode(b"M").decode("ascii"),
                 "duration_seconds": 1.0}] * 5})
        self.assertEqual(captured["timeout"], 300 * max(1, 5 // 3 + 1))

    def test_local_sampling_params_forwarded(self):
        _, captured, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            temperature=0.6, top_p=0.9)
        payload = captured["payload"]["input"]
        self.assertEqual(payload["temperature"], 0.6)
        self.assertEqual(payload["top_p"], 0.9)


class LocalResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_local_base64_result_writes_file(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local")
        self.assertTrue(result["success"])
        self.assertEqual(self.out.read_bytes(), b"MP3DATA")

    def test_local_result_with_audio_url_fails_explicitly(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [{
                "audio_url": "https://files.example/x.mp3",
                "duration_seconds": 1.0,
            }]})
        self.assertFalse(result["success"])
        self.assertIn("base64", result["error"])

    def test_local_result_with_r2_key_fails_explicitly(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [{
                "r2_key": "qwen3-tts/out.mp3",
                "duration_seconds": 1.0,
            }]})
        self.assertFalse(result["success"])
        self.assertIn("base64", result["error"])

    def test_local_result_without_audio_fails(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [{"duration_seconds": 1.0}]})
        self.assertFalse(result["success"])


class LocalRejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def reject(self, **kwargs):
        with mock.patch("cloud_gpu.call_cloud_endpoint",
                        side_effect=AssertionError("no HTTP allowed")):
            return qwen3_tts.generate_audio(
                text="你好", output_path=str(self.out), verbose=False, **kwargs)

    def test_local_without_ref_audio_is_rejected_before_http(self):
        result = self.reject(cloud="local", speaker="Ryan")
        self.assertFalse(result["success"])
        self.assertIn("clone", result["error"])

    def test_local_without_ref_text_is_rejected_before_http(self):
        result = self.reject(cloud="local", ref_audio=str(self.ref),
                             ref_text=None)
        self.assertFalse(result["success"])
        self.assertIn("clone", result["error"])

    def test_local_design_instruct_is_rejected_before_http(self):
        result = self.reject(cloud="local", design_instruct="warm narrator",
                             ref_audio=str(self.ref), ref_text="参考")
        self.assertFalse(result["success"])
        self.assertIn("clone", result["error"])


class ModalUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_modal_clone_still_uploads_and_sends_url(self):
        captured = {}

        def fake_call(provider, payload, tool_name, timeout, **rest):
            captured["payload"] = payload
            return {"success": True, "outputs": [{
                "audio_base64": base64.b64encode(b"MP3DATA").decode("ascii"),
                "duration_seconds": 1.0,
            }]}, 0.5

        with mock.patch("cloud_gpu.call_cloud_endpoint",
                        side_effect=fake_call), \
                mock.patch.object(qwen3_tts, "upload_to_storage",
                                  return_value=("https://r2.example/ref.mp3",
                                                "qwen3-tts/input/ref.mp3")) as up, \
                mock.patch.object(qwen3_tts, "get_r2_payload_config",
                                  return_value=None):
            result = qwen3_tts.generate_audio(
                text="你好", output_path=str(self.out), ref_audio=str(self.ref),
                ref_text="参考", verbose=False, cloud="modal")
        self.assertTrue(result["success"])
        up.assert_called_once()
        payload = captured["payload"]["input"]
        self.assertEqual(payload["ref_audio_url"], "https://r2.example/ref.mp3")
        self.assertNotIn("ref_audio_base64", payload)


class CliArgsTests(unittest.TestCase):
    def test_model_is_a_free_string_and_cloud_accepts_local(self):
        argv = ["qwen3_tts.py", "--text", "hi", "--output", "o.mp3",
                "--cloud", "local", "--model", "qwen3-tts-1.7B",
                "--ref-audio", "r.mp3", "--ref-text", "t"]
        with mock.patch.object(sys, "argv", argv):
            args = qwen3_tts.parse_args()
        self.assertEqual(args.model, "qwen3-tts-1.7B")
        self.assertEqual(args.cloud, "local")

    def test_main_rejects_local_without_ref_before_http(self):
        argv = ["qwen3_tts.py", "--cloud", "local", "--text", "hi",
                "--output", "o.mp3"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                qwen3_tts.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("clone", err.getvalue())

    def test_main_rejects_local_design_instruct(self):
        argv = ["qwen3_tts.py", "--cloud", "local", "--text", "hi",
                "--output", "o.wav", "--design-instruct", "warm",
                "--ref-audio", "r.mp3", "--ref-text", "t"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                qwen3_tts.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
