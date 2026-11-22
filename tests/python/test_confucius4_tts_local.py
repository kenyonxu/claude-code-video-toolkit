"""confucius4_tts: local clone-only CLI payload, guards and results.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_confucius4_tts_local.py -v

Every cloud call is faked. Nothing here contacts 127.0.0.1.
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

import cloud_gpu  # noqa: E402
import confucius4_tts as c4  # noqa: E402


def run_generate(text, output, ref_path, cloud_output=None, **kwargs):
    if cloud_output is None:
        cloud_output = {"success": True, "outputs": [{
            "audio_base64": base64.b64encode(b"MP3DATA").decode("ascii"),
            "duration_seconds": 1.25,
        }]}
    captured = {}

    def fake_call(provider, payload, tool_name, timeout, **rest):
        captured["provider"] = provider
        captured["payload"] = payload
        captured["tool_name"] = tool_name
        captured["timeout"] = timeout
        return cloud_output, 0.5

    with mock.patch("cloud_gpu.call_cloud_endpoint",
                    side_effect=fake_call):
        result = c4.generate_audio(
            text=text, output_path=output, ref_audio=str(ref_path),
            verbose=False, **kwargs)
    return result, captured


class CliArgsTests(unittest.TestCase):
    def test_defaults_are_local_confucius4(self):
        with mock.patch.object(sys, "argv", [
                "confucius4_tts.py", "--text", "hi", "--ref-audio", "r.mp3",
                "--output", "o.mp3"]):
            args = c4.parse_args()
        self.assertEqual(args.cloud, "local")
        self.assertEqual(args.model, "confucius4-tts")
        self.assertEqual(args.lang, "zh")
        self.assertEqual(args.format, "mp3")
        self.assertEqual(args.timeout, 300)

    def test_cloud_has_no_non_local_choice(self):
        with mock.patch.object(sys, "argv", [
                "confucius4_tts.py", "--cloud", "modal", "--text", "hi",
                "--ref-audio", "r.mp3", "--output", "o.mp3"]):
            with self.assertRaises(SystemExit):
                c4.parse_args()

    def test_lang_only_accepts_the_14_codes(self):
        with mock.patch.object(sys, "argv", [
                "confucius4_tts.py", "--lang", "ZH", "--text", "hi",
                "--ref-audio", "r.mp3", "--output", "o.mp3"]):
            with self.assertRaises(SystemExit):
                c4.parse_args()


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_payload_is_clone_base64_without_qwen_fields(self):
        result, captured = run_generate(
            "你好", str(self.out), self.ref, model="confucius4-tts")
        self.assertTrue(result["success"], result)
        self.assertEqual(captured["provider"], "local")
        self.assertEqual(captured["tool_name"], "confucius4_tts")
        payload = captured["payload"]["input"]
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["language"], "zh")
        self.assertEqual(payload["output_format"], "mp3")
        self.assertEqual(payload["model"], "confucius4-tts")
        self.assertEqual(payload["ref_audio_base64"],
                         base64.b64encode(b"REFERENCE-AUDIO").decode("ascii"))
        for forbidden in ("ref_text", "ref_audio_url", "r2", "speaker",
                          "instruct", "temperature", "top_p"):
            self.assertNotIn(forbidden, payload)

    def test_default_model_omits_model_field(self):
        _, captured = run_generate("你好", str(self.out), self.ref,
                                   model=None)
        self.assertNotIn("model", captured["payload"]["input"])

    def test_timeout_scales_with_batch(self):
        _, captured = run_generate(
            ["一", "二", "三", "四", "五"], [str(self.out)] * 5, self.ref,
            cloud_output={"success": True, "outputs": [
                {"audio_base64": base64.b64encode(b"M").decode("ascii"),
                 "duration_seconds": 1.0}] * 5})
        self.assertEqual(captured["timeout"], 300 * max(1, 5 // 3 + 1))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def reject(self, **kwargs):
        with mock.patch("cloud_gpu.call_cloud_endpoint",
                        side_effect=AssertionError("no HTTP allowed")):
            return c4.generate_audio(
                text="你好", output_path=str(self.out), verbose=False,
                **kwargs)

    def test_missing_ref_audio_rejected_before_http(self):
        result = self.reject()
        self.assertFalse(result["success"])
        self.assertIn("--ref-audio", result["error"])

    def test_unknown_model_rejected_before_http(self):
        result = self.reject(ref_audio=str(self.ref),
                             model="qwen3-tts-1.7B")
        self.assertFalse(result["success"])
        self.assertIn("confucius4-tts", result["error"])

    def test_non_local_cloud_rejected(self):
        result = self.reject(ref_audio=str(self.ref), cloud="modal")
        self.assertFalse(result["success"])
        self.assertIn("--cloud local", result["error"])

    def test_missing_ref_file_rejected(self):
        result = self.reject(ref_audio="does-not-exist.mp3")
        self.assertFalse(result["success"])


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_base64_result_writes_file_without_r2(self):
        result, _ = run_generate("你好", str(self.out), self.ref)
        self.assertTrue(result["success"])
        self.assertEqual(self.out.read_bytes(), b"MP3DATA")
        self.assertEqual(result["output"], str(self.out))
        self.assertIn("duration_seconds", result)

    def test_external_upload_and_download_functions_are_never_called(self):
        with mock.patch("file_transfer.upload_to_storage") as upload, \
                mock.patch("file_transfer.download_from_r2") as download_r2, \
                mock.patch("file_transfer.download_from_url") as download_url:
            result, _ = run_generate("你好", str(self.out), self.ref)
        self.assertTrue(result["success"])
        upload.assert_not_called()
        download_r2.assert_not_called()
        download_url.assert_not_called()

    def test_result_with_url_or_r2_key_fails_explicitly(self):
        for extra in ({"audio_url": "https://files.example/x.mp3"},
                      {"r2_key": "confucius/out.mp3"}):
            with self.subTest(extra=extra):
                result, _ = run_generate(
                    "你好", str(self.out), self.ref,
                    cloud_output={"success": True, "outputs": [
                        dict(extra, duration_seconds=1.0)]})
                self.assertFalse(result["success"])
                self.assertIn("base64", result["error"])

    def test_result_count_mismatch_fails(self):
        result, _ = run_generate(
            "你好", str(self.out), self.ref,
            cloud_output={"success": True, "outputs": []})
        self.assertFalse(result["success"])


class EnvMappingTests(unittest.TestCase):
    def test_tool_is_registered_for_local(self):
        self.assertEqual(cloud_gpu._LOCAL_ENV_VARS["confucius4_tts"],
                         "LOCAL_TTS_ENDPOINT_URL")


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["confucius4_tts.py"] + argv), \
                mock.patch("sys.stderr", err):
            try:
                c4.main()
                return 0, err.getvalue()
            except SystemExit as e:
                return e.code, err.getvalue()

    def test_missing_ref_audio_exits_1(self):
        code, text = self.run_main(["--text", "hi", "--output", "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("--ref-audio", text)

    def test_unknown_model_exits_1(self):
        code, text = self.run_main(["--text", "hi", "--ref-audio", "r.mp3",
                                    "--output", "o.mp3",
                                    "--model", "qwen3-tts-1.7B"])
        self.assertEqual(code, 1)
        self.assertIn("confucius4-tts", text)


if __name__ == "__main__":
    unittest.main()
