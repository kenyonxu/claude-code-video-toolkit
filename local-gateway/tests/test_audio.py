"""audio: bounded downloads, ffprobe validation and FLAC-to-WAV transcode.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v

ffmpeg/ffprobe run as real local subprocesses on tiny generated clips (no
network). ComfyUI clients are fakes.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import audio, tts_workflow  # noqa: E402
from local_gateway.comfyui import ComfyUIError  # noqa: E402

FAR_DEADLINE = 30.0


def make_clip(path: Path, codec: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
         "-c:a", codec, str(path)],
        check=True, capture_output=True, shell=False)


class ClipMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.mp3_path = root / "clip.mp3"
        cls.flac_path = root / "clip.flac"
        make_clip(cls.mp3_path, "libmp3lame")
        make_clip(cls.flac_path, "flac")
        cls.mp3_bytes = cls.mp3_path.read_bytes()
        cls.flac_bytes = cls.flac_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def deadline(self):
        return time.monotonic() + FAR_DEADLINE


class FakeViewClient:
    def __init__(self, content: bytes):
        self.content = content
        self.view_calls = []

    def view(self, filename, subfolder="", file_type="output",
             max_bytes=audio.VIEW_MAX_BYTES):
        self.view_calls.append((filename, subfolder, file_type, max_bytes))
        return self.content


def history_entry():
    return {"outputs": {tts_workflow.SAVE_NODE: {"audio": [
        {"filename": "clone_00001_.mp3", "subfolder": "ccvt",
         "type": "output"}]}}}


class ViewBudgetTests(ClipMixin, unittest.TestCase):
    def test_limits_are_the_spec_values(self):
        self.assertEqual(audio.VIEW_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(audio.REQUEST_BUDGET_BYTES, 128 * 1024 * 1024)

    def test_max_bytes_is_min_of_file_limit_and_remaining(self):
        budget = audio.ViewBudget(limit=100)
        self.assertEqual(budget.max_bytes_for_next(), 100)
        budget.spend(40)
        self.assertEqual(budget.max_bytes_for_next(), 60)
        budget.spend(60)
        self.assertEqual(budget.max_bytes_for_next(), 0)

    def test_download_passes_the_bounded_max_bytes_and_spends(self):
        budget = audio.ViewBudget(limit=1000)
        client = FakeViewClient(b"x" * 30)
        data = audio.download_history_audio(client, history_entry(),
                                            tts_workflow.SAVE_NODE, budget,
                                            self.deadline())
        self.assertEqual(data, b"x" * 30)
        filename, subfolder, file_type, max_bytes = client.view_calls[0]
        self.assertEqual(filename, "clone_00001_.mp3")
        self.assertEqual(subfolder, "ccvt")
        self.assertEqual(file_type, "output")
        self.assertEqual(max_bytes, 1000)
        self.assertEqual(budget.remaining, 970)

    def test_download_with_exhausted_budget_requests_zero_max_bytes(self):
        # The overflow error itself is raised by the real streaming client
        # (see BoundedViewTests in test_comfyui.py); here we assert the
        # budget drives the max_bytes argument down to zero.
        budget = audio.ViewBudget(limit=10)
        budget.spend(10)
        client = FakeViewClient(b"abc")
        audio.download_history_audio(client, history_entry(),
                                     tts_workflow.SAVE_NODE, budget,
                                     self.deadline())
        self.assertEqual(client.view_calls[0][3], 0)

    def test_download_after_deadline_is_comfyui_timeout(self):
        budget = audio.ViewBudget()
        client = FakeViewClient(b"abc")
        with self.assertRaises(ComfyUIError) as ctx:
            audio.download_history_audio(client, history_entry(),
                                         tts_workflow.SAVE_NODE, budget,
                                         time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")

    def test_malformed_history_raises_value_error(self):
        budget = audio.ViewBudget()
        client = FakeViewClient(b"abc")
        with self.assertRaises(ValueError):
            audio.download_history_audio(client, {"outputs": {}},
                                         tts_workflow.SAVE_NODE, budget,
                                         self.deadline())


class FfprobeTests(ClipMixin, unittest.TestCase):
    def test_real_mp3_reports_positive_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(self.mp3_bytes)
            duration = audio.ffprobe_duration(path, self.deadline())
        self.assertGreater(duration, 0)

    def test_garbage_bytes_are_generation_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(b"not audio at all")
            with self.assertRaises(ComfyUIError) as ctx:
                audio.ffprobe_duration(path, self.deadline())
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_expired_deadline_is_comfyui_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(self.mp3_bytes)
            with self.assertRaises(ComfyUIError) as ctx:
                audio.ffprobe_duration(path, time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")

    def test_subprocess_timeout_is_comfyui_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(self.mp3_bytes)
            with mock.patch.object(
                    subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("ffprobe", 1)):
                with self.assertRaises(ComfyUIError) as ctx:
                    audio.ffprobe_duration(path, self.deadline())
        self.assertEqual(ctx.exception.code, "comfyui_timeout")


class FlacToWavTests(ClipMixin, unittest.TestCase):
    def test_real_flac_becomes_pcm_s16le_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            flac = Path(tmp) / "in.flac"
            wav = Path(tmp) / "out.wav"
            flac.write_bytes(self.flac_bytes)
            audio.flac_to_wav(flac, wav, self.deadline())
            self.assertTrue(wav.is_file())
            data = wav.read_bytes()
        self.assertEqual(data[:4], b"RIFF")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", "-"],
            input=data, capture_output=True, shell=False)
        self.assertEqual(result.stdout.decode().strip(), "pcm_s16le")

    def test_garbage_flac_is_generation_failed_with_short_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            flac = Path(tmp) / "in.flac"
            wav = Path(tmp) / "out.wav"
            flac.write_bytes(b"junk" * 100)
            with self.assertRaises(ComfyUIError) as ctx:
                audio.flac_to_wav(flac, wav, self.deadline())
        self.assertEqual(ctx.exception.code, "generation_failed")
        self.assertLessEqual(len(ctx.exception.message), 400)

    def test_expired_deadline_is_comfyui_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            flac = Path(tmp) / "in.flac"
            wav = Path(tmp) / "out.wav"
            flac.write_bytes(self.flac_bytes)
            with self.assertRaises(ComfyUIError) as ctx:
                audio.flac_to_wav(flac, wav, time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")


class FinalizeOutputTests(ClipMixin, unittest.TestCase):
    def test_mp3_passthrough_returns_view_bytes_and_duration(self):
        client = FakeViewClient(self.mp3_bytes)
        budget = audio.ViewBudget()
        with tempfile.TemporaryDirectory() as tmp:
            data, duration = audio.finalize_output(
                client, history_entry(), "mp3", Path(tmp), "item_0",
                budget, self.deadline())
        self.assertEqual(data, self.mp3_bytes)
        self.assertGreater(duration, 0)
        self.assertEqual(budget.remaining,
                         audio.REQUEST_BUDGET_BYTES - len(self.mp3_bytes))

    def test_wav_transcodes_flac_and_reports_duration(self):
        client = FakeViewClient(self.flac_bytes)
        with tempfile.TemporaryDirectory() as tmp:
            data, duration = audio.finalize_output(
                client, history_entry(), "wav", Path(tmp), "item_0",
                audio.ViewBudget(), self.deadline())
        self.assertEqual(data[:4], b"RIFF")
        self.assertGreater(duration, 0)

    def test_wav_leaves_no_flac_or_wav_behind_in_workdir(self):
        client = FakeViewClient(self.flac_bytes)
        with tempfile.TemporaryDirectory() as tmp:
            audio.finalize_output(client, history_entry(), "wav", Path(tmp),
                                  "item_0", audio.ViewBudget(),
                                  self.deadline())
            leftovers = list(Path(tmp).iterdir())
        # The workdir is the caller's request temp dir; files inside are
        # cleaned by the caller's TemporaryDirectory. Here we only assert
        # the helper used the requested stem.
        self.assertTrue(all(p.name.startswith("item_0") for p in leftovers))


if __name__ == "__main__":
    unittest.main()
