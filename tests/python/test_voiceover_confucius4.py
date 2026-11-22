"""voiceover confucius4: local clone guards, --tts-model passthrough.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_confucius4.py -v

The confucius4_tts generator is faked; no network, no ElevenLabs SDK.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import confucius4_tts  # noqa: E402
import voiceover  # noqa: E402


class LocalCloneErrorTests(unittest.TestCase):
    def test_confucius4_local_with_ref_audio_is_fine(self):
        self.assertIsNone(voiceover.local_clone_error(
            "confucius4", "local", "ref.mp3"))

    def test_confucius4_without_ref_audio_requires_it(self):
        message = voiceover.local_clone_error("confucius4", "local", None)
        self.assertIsNotNone(message)
        self.assertIn("--ref-audio", message)
        self.assertNotIn("--ref-text", message)

    def test_confucius4_non_local_cloud_is_rejected(self):
        message = voiceover.local_clone_error("confucius4", "modal", "r.mp3")
        self.assertIsNotNone(message)
        self.assertIn("--cloud local", message)

    def test_confucius4_brand_design_is_rejected(self):
        message = voiceover.local_clone_error(
            "confucius4", "local", None, brand_design=True)
        self.assertIsNotNone(message)
        self.assertIn("clone only", message)

    def test_qwen3_local_is_rejected(self):
        message = voiceover.local_clone_error(
            "qwen3", "local", "ref.mp3")
        self.assertIsNotNone(message)
        self.assertIn("confucius4", message)

    def test_cloud_providers_are_unchanged(self):
        self.assertIsNone(voiceover.local_clone_error("qwen3", "modal", None))
        self.assertIsNone(voiceover.local_clone_error(
            "elevenlabs", "local", None))


def capture_generate(result=None):
    captured = {}
    default = {"success": True, "output": "out.mp3",
               "duration_seconds": 1.5}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return result if result is not None else default

    return captured, mock.patch.object(confucius4_tts, "generate_audio",
                                       side_effect=fake_generate)


class PassthroughTests(unittest.TestCase):
    def test_single_passes_model_lang_and_timeout(self):
        captured, patcher = capture_generate()
        with patcher:
            result = voiceover.generate_single_audio_confucius4(
                script="hi", output_path=Path("out.mp3"), lang="zh",
                ref_audio="ref.mp3", cloud="local",
                model="confucius4-tts", timeout=420)
        self.assertTrue(result["success"])
        self.assertEqual(captured["model"], "confucius4-tts")
        self.assertEqual(captured["lang"], "zh")
        self.assertEqual(captured["timeout"], 420)
        self.assertEqual(captured["cloud"], "local")
        self.assertEqual(captured["ref_audio"], "ref.mp3")

    def test_single_rejects_missing_ref_before_generator(self):
        captured, patcher = capture_generate()
        with patcher as generator:
            result = voiceover.generate_single_audio_confucius4(
                script="hi", output_path=Path("out.mp3"), cloud="local")
        self.assertFalse(result["success"])
        generator.assert_not_called()

    def test_single_rejects_non_local_cloud(self):
        captured, patcher = capture_generate()
        with patcher as generator:
            result = voiceover.generate_single_audio_confucius4(
                script="hi", output_path=Path("out.mp3"), cloud="modal",
                ref_audio="r.mp3")
        self.assertFalse(result["success"])
        generator.assert_not_called()

    def test_batch_passes_texts_and_model(self):
        captured, patcher = capture_generate(result={
            "success": True,
            "outputs": [{"output": "a.mp3"}, {"output": "b.mp3"}],
        })
        with patcher:
            results = voiceover.generate_batch_audio_confucius4(
                scripts=["a", "b"],
                output_paths=[Path("a.mp3"), Path("b.mp3")],
                lang="zh", ref_audio="ref.mp3", cloud="local",
                model="confucius4-tts", timeout=300)
        self.assertEqual(len(results), 2)
        self.assertEqual(captured["text"], ["a", "b"])
        self.assertEqual(captured["model"], "confucius4-tts")
        self.assertEqual(captured["lang"], "zh")

    def test_scene_directory_rejects_missing_ref_before_io(self):
        captured, patcher = capture_generate()
        with patcher as generator:
            results, _, _ = voiceover.process_scene_directory(
                Path("directory-that-must-not-be-read"),
                provider="confucius4", cloud="local")
        self.assertFalse(results[0]["success"])
        generator.assert_not_called()


class ArgsTests(unittest.TestCase):
    def parse(self, argv):
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv):
            return voiceover.parse_args()

    def test_tts_model_exists_and_qwen_model_is_gone(self):
        args = self.parse(["--provider", "confucius4", "--cloud", "local",
                           "--tts-model", "confucius4-tts", "--timeout",
                           "420", "--script", "s.txt", "--output", "o.mp3",
                           "--ref-audio", "r.mp3"])
        self.assertEqual(args.tts_model, "confucius4-tts")
        self.assertEqual(args.timeout, 420)
        with self.assertRaises(SystemExit):
            self.parse(["--provider", "qwen3", "--qwen-model", "x",
                        "--script", "s.txt", "--output", "o.mp3"])

    def test_provider_accepts_confucius4(self):
        args = self.parse(["--provider", "confucius4", "--cloud", "local",
                           "--script", "s.txt", "--output", "o.mp3",
                           "--ref-audio", "r.mp3"])
        self.assertEqual(args.provider, "confucius4")

    def test_elevenlabs_model_argument_is_unchanged(self):
        args = self.parse(["--model", "eleven_v4", "--script", "s.txt",
                           "--output", "o.mp3"])
        self.assertEqual(args.model, "eleven_v4")


class MainGuardTests(unittest.TestCase):
    def run_main(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv), \
                mock.patch("voiceover.load_dotenv"), \
                mock.patch("sys.stderr", err):
            try:
                voiceover.main()
                return 0, err.getvalue()
            except SystemExit as e:
                return e.code, err.getvalue()

    def test_confucius4_without_ref_audio_exits_1(self):
        code, text = self.run_main([
            "--provider", "confucius4", "--cloud", "local", "--script",
            "s.txt", "--output", "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("--ref-audio", text)

    def test_confucius4_explicit_speaker_exits_1(self):
        code, _ = self.run_main([
            "--provider", "confucius4", "--cloud", "local", "--speaker",
            "Gloria", "--ref-audio", "r.mp3", "--script", "s.txt",
            "--output", "o.mp3"])
        self.assertEqual(code, 1)

    def test_confucius4_ref_text_and_sampling_exit_1(self):
        for extra in (["--ref-text", "transcript"],
                      ["--temperature", "0.7"], ["--top-p", "0.8"]):
            with self.subTest(extra=extra):
                code, _ = self.run_main([
                    "--provider", "confucius4", "--cloud", "local",
                    "--ref-audio", "r.mp3", "--script", "s.txt",
                    "--output", "o.mp3", *extra])
                self.assertEqual(code, 1)

    def test_confucius4_clone_does_not_require_ref_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "script.txt"
            reference = root / "ref.mp3"
            output = root / "out.mp3"
            script.write_text("hello", encoding="utf-8")
            reference.write_bytes(b"reference")
            with mock.patch(
                    "voiceover.generate_single_audio_confucius4",
                    return_value={"success": True, "output": str(output),
                                  "duration_seconds": 1.0}) as generator:
                code, text = self.run_main([
                    "--provider", "confucius4", "--cloud", "local",
                    "--tts-model", "confucius4-tts", "--language", "zh",
                    "--ref-audio", str(reference), "--script", str(script),
                    "--output", str(output)])
        self.assertEqual(code, 0, text)
        generator.assert_called_once()
        self.assertEqual(generator.call_args.kwargs["lang"], "zh")
        self.assertEqual(generator.call_args.kwargs["model"],
                         "confucius4-tts")
        self.assertNotIn("ref_text", generator.call_args.kwargs)

    def test_qwen3_local_exits_1_pointing_at_confucius4(self):
        code, text = self.run_main([
            "--provider", "qwen3", "--cloud", "local", "--ref-audio",
            "r.mp3", "--ref-text", "t", "--script", "s.txt", "--output",
            "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("confucius4", text)

    def test_scene_dry_run_never_calls_the_generator(self):
        """Regression: the scene dry-run call site once kept a stale
        qwen_model= keyword after the --tts-model rename (a TypeError on
        every confucius4/qwen3 scene dry-run). Drive main() end to end and
        prove the dry-run path parses, prints JSON and generates nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scene_dir = root / "scenes"
            scene_dir.mkdir()
            (scene_dir / "01_a.txt").write_text("first scene", encoding="utf-8")
            (scene_dir / "02_b.txt").write_text("second scene", encoding="utf-8")
            reference = root / "ref.mp3"
            reference.write_bytes(b"reference")
            out = io.StringIO()
            err = io.StringIO()
            with mock.patch.object(sys, "argv", [
                    "voiceover.py", "--provider", "confucius4", "--cloud",
                    "local", "--tts-model", "confucius4-tts",
                    "--scene-dir", str(scene_dir), "--dry-run", "--json",
                    "--ref-audio", str(reference)]), \
                    mock.patch("voiceover.load_dotenv"), \
                    mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err), \
                    mock.patch(
                        "voiceover.generate_single_audio_confucius4") as single, \
                    mock.patch(
                        "voiceover.generate_batch_audio_confucius4") as batch:
                try:
                    voiceover.main()
                    code = 0
                except SystemExit as e:
                    code = e.code
            payload = json.loads(out.getvalue())
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["provider"], "confucius4")
        self.assertEqual(len(payload["scenes"]), 2)
        single.assert_not_called()
        batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
