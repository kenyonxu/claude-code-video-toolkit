"""voiceover local clone: guards, --qwen-model/--timeout passthrough.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_local.py -v

The qwen3_tts generator is faked; no network, no ElevenLabs SDK.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402
import voiceover  # noqa: E402


class LocalCloneErrorTests(unittest.TestCase):
    def test_cloud_providers_are_unchanged(self):
        self.assertIsNone(voiceover.local_clone_error(
            "qwen3", "modal", None, None))
        self.assertIsNone(voiceover.local_clone_error(
            "qwen3", "runpod", None, None))

    def test_elevenlabs_is_unchanged_even_on_local(self):
        self.assertIsNone(voiceover.local_clone_error(
            "elevenlabs", "local", None, None))

    def test_local_without_ref_audio_or_ref_text_is_clone_only_error(self):
        message = voiceover.local_clone_error("qwen3", "local", None, None)
        self.assertIsNotNone(message)
        self.assertIn("clone", message)
        self.assertIn("--ref-audio", message)
        self.assertIn("--ref-text", message)

    def test_local_with_only_ref_audio_is_still_an_error(self):
        self.assertIsNotNone(voiceover.local_clone_error(
            "qwen3", "local", "ref.mp3", None))

    def test_local_with_both_refs_is_fine(self):
        self.assertIsNone(voiceover.local_clone_error(
            "qwen3", "local", "ref.mp3", "transcript"))

    def test_brand_design_on_local_is_rejected(self):
        message = voiceover.local_clone_error(
            "qwen3", "local", None, None, brand_design=True)
        self.assertIsNotNone(message)
        self.assertIn("design", message.lower())


class QwenPassthroughTests(unittest.TestCase):
    def capture_generate(self, result=None):
        captured = {}
        default = {"success": True, "output": "out.mp3",
                   "duration_seconds": 1.5}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return result if result is not None else default

        return captured, mock.patch.object(qwen3_tts, "generate_audio",
                                           side_effect=fake_generate)

    def test_single_passes_model_and_timeout(self):
        captured, patcher = self.capture_generate()
        with patcher:
            result = voiceover.generate_single_audio_qwen3(
                script="hi", output_path=Path("out.mp3"),
                ref_audio="ref.mp3", ref_text="t", cloud="local",
                model="qwen3-tts-1.7B", timeout=300)
        self.assertTrue(result["success"])
        self.assertEqual(captured["model"], "qwen3-tts-1.7B")
        self.assertEqual(captured["timeout"], 300)
        self.assertEqual(captured["cloud"], "local")
        self.assertEqual(captured["ref_audio"], "ref.mp3")

    def test_single_defaults_match_the_gateway_formula(self):
        captured, patcher = self.capture_generate()
        with patcher:
            voiceover.generate_single_audio_qwen3(
                script="hi", output_path=Path("out.mp3"),
                ref_audio="ref.mp3", ref_text="t", cloud="local")
        self.assertEqual(captured["timeout"], 300)
        self.assertIsNone(captured["model"])

    def test_batch_default_timeout_is_300_not_120(self):
        captured, patcher = self.capture_generate(result={
            "success": True,
            "outputs": [{"output": "a.mp3"}, {"output": "b.mp3"}],
        })
        with patcher:
            results = voiceover.generate_batch_audio_qwen3(
                scripts=["a", "b"],
                output_paths=[Path("a.mp3"), Path("b.mp3")],
                ref_audio="ref.mp3", ref_text="t", cloud="local")
        self.assertEqual(len(results), 2)
        self.assertEqual(captured["timeout"], 300)
        self.assertEqual(captured["text"], ["a", "b"])

    def test_batch_passes_model_and_timeout(self):
        captured, patcher = self.capture_generate()
        with patcher:
            voiceover.generate_batch_audio_qwen3(
                scripts=["a"], output_paths=[Path("a.mp3")],
                ref_audio="ref.mp3", ref_text="t", cloud="local",
                model="qwen3-tts-1.7B", timeout=420)
        self.assertEqual(captured["model"], "qwen3-tts-1.7B")
        self.assertEqual(captured["timeout"], 420)


    def test_single_helper_rejects_missing_refs_before_generator(self):
        captured, patcher = self.capture_generate()
        with patcher as generator:
            result = voiceover.generate_single_audio_qwen3(
                script="hi", output_path=Path("out.mp3"), cloud="local")
        self.assertFalse(result["success"])
        generator.assert_not_called()

    def test_batch_helper_rejects_missing_refs_before_generator(self):
        captured, patcher = self.capture_generate()
        with patcher as generator:
            results = voiceover.generate_batch_audio_qwen3(
                scripts=["a"], output_paths=[Path("a.mp3")], cloud="local")
        self.assertFalse(results[0]["success"])
        generator.assert_not_called()

    def test_scene_directory_helper_rejects_missing_refs_before_io(self):
        captured, patcher = self.capture_generate()
        with patcher as generator:
            results, _, _ = voiceover.process_scene_directory(
                Path("directory-that-must-not-be-read"), cloud="local")
        self.assertFalse(results[0]["success"])
        generator.assert_not_called()


class VoiceoverArgsTests(unittest.TestCase):
    def parse(self, argv):
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv):
            return voiceover.parse_args()

    def test_qwen_model_is_free_string_and_timeout_defaults_300(self):
        args = self.parse(["--provider", "qwen3", "--script", "s.txt",
                           "--output", "o.mp3"])
        self.assertIsNone(args.qwen_model)
        self.assertEqual(args.timeout, 300)
        self.assertEqual(args.cloud, "modal")

    def test_cloud_accepts_local_and_custom_timeout(self):
        args = self.parse(["--provider", "qwen3", "--cloud", "local",
                           "--qwen-model", "qwen3-tts-1.7B", "--timeout",
                           "420", "--script", "s.txt", "--output", "o.mp3",
                           "--ref-audio", "r.mp3", "--ref-text", "t"])
        self.assertEqual(args.cloud, "local")
        self.assertEqual(args.qwen_model, "qwen3-tts-1.7B")
        self.assertEqual(args.timeout, 420)

    def test_elevenlabs_model_argument_is_unchanged(self):
        args = self.parse(["--model", "eleven_v4", "--script", "s.txt",
                           "--output", "o.mp3"])
        self.assertEqual(args.model, "eleven_v4")


class MainGuardTests(unittest.TestCase):
    def run_main(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv), \
                mock.patch("sys.stderr", err):
            try:
                voiceover.main()
            except SystemExit as e:
                return e.code, err.getvalue()
        self.fail("main() must exit for these local misuse cases")

    def test_local_without_refs_exits_1_with_clone_only_error(self):
        code, text = self.run_main([
            "--provider", "qwen3", "--cloud", "local", "--script",
            "does-not-need-to-exist.txt", "--output", "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("clone", text)

    def test_local_design_flag_style_is_covered_by_guard(self):
        # Builtin speaker (no refs) on local is a clone-only error too.
        code, text = self.run_main([
            "--provider", "qwen3", "--cloud", "local", "--speaker", "Ryan",
            "--script", "s.txt", "--output", "o.mp3"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
