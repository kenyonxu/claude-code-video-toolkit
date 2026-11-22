"""audio: WAV header validation, passthrough, MP3 transcode, budget.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v

ffmpeg/ffprobe run as real local subprocesses on tiny generated clips (no
network). The worker WAV input is generated with ffmpeg at 22050 Hz mono.
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

from local_gateway import audio  # noqa: E402

FAR_DEADLINE = 60.0


def make_wav(path: Path, *, rate="22050", channels="1", codec="pcm_s16le"):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=0.3",
         "-ar", rate, "-ac", channels, "-c:a", codec, str(path)],
        check=True, capture_output=True, shell=False)


class ClipMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.wav_path = root / "clip.wav"
        make_wav(cls.wav_path)
        cls.wav_bytes = cls.wav_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def deadline(self):
        return time.monotonic() + FAR_DEADLINE


class ParseWavHeaderTests(ClipMixin, unittest.TestCase):
    def test_worker_wav_parses_to_pcm16_mono_22050(self):
        fmt = audio.parse_wav_header(self.wav_bytes)
        self.assertEqual(fmt["audio_format"], 1)
        self.assertEqual(fmt["channels"], 1)
        self.assertEqual(fmt["sample_rate"], 22050)
        self.assertEqual(fmt["bits_per_sample"], 16)

    def test_not_riff_is_generation_failed(self):
        for bad in (b"", b"NOPE" + b"x" * 20, b"RIFF" + b"\x00" * 4 + b"NOPE"):
            with self.subTest(bad=bad[:8]):
                with self.assertRaises(audio.AudioError) as ctx:
                    audio.parse_wav_header(bad)
                self.assertEqual(ctx.exception.code, "generation_failed")

    def test_validate_wav_rejects_wrong_shape(self):
        variants = [
            ({"channels": "2"}, "mono"),
            ({"rate": "44100"}, "22050"),
            ({"codec": "pcm_f32le"}, "PCM16"),
        ]
        for kwargs, needle in variants:
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "v.wav"
                    make_wav(path, **kwargs)
                    with self.assertRaises(audio.AudioError) as ctx:
                        audio.validate_wav(path.read_bytes())
                self.assertEqual(ctx.exception.code, "generation_failed")
                self.assertIn(needle, ctx.exception.message)


class ByteBudgetTests(unittest.TestCase):
    def test_limits_are_the_spec_values(self):
        self.assertEqual(audio.ITEM_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(audio.REQUEST_BUDGET_BYTES, 128 * 1024 * 1024)

    def test_max_bytes_is_min_of_item_limit_and_remaining(self):
        budget = audio.ByteBudget(limit=100)
        self.assertEqual(budget.max_bytes_for_next(), 100)
        budget.spend(40)
        self.assertEqual(budget.max_bytes_for_next(), 60)
        budget.spend(60)
        self.assertEqual(budget.max_bytes_for_next(), 0)


class FinalizeOutputTests(ClipMixin, unittest.TestCase):
    def finalize(self, output_format, workdir, budget=None, stem="item_0",
                 data=None, deadline=None):
        return audio.finalize_output(
            data if data is not None else self.wav_bytes, output_format,
            workdir, stem, budget or audio.ByteBudget(),
            deadline or self.deadline())

    def test_wav_passthrough_returns_same_bytes_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, duration = self.finalize("wav", Path(tmp))
        self.assertEqual(data, self.wav_bytes)
        self.assertGreater(duration, 0)

    def test_mp3_is_a_real_libmp3lame_transcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            data, duration = self.finalize("mp3", workdir)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                 str(workdir / "item_0.mp3")],
                capture_output=True, text=True, shell=False)
        self.assertEqual(probe.stdout.strip(), "mp3")
        self.assertTrue(data[:3] == b"ID3" or data[:2] == b"\xff\xfb")
        self.assertGreater(duration, 0)

    def test_success_leaves_no_temp_files_in_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self.finalize("mp3", workdir)
            self.finalize("wav", workdir, stem="item_1")
            names = sorted(p.name for p in workdir.iterdir())
        self.assertEqual(names, ["item_0.mp3", "item_1.wav"])

    def test_ffmpeg_failure_leaves_no_final_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with mock.patch.object(
                    subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
                with self.assertRaises(audio.AudioError) as ctx:
                    self.finalize("mp3", workdir)
            self.assertEqual(ctx.exception.code, "tts_backend_timeout")
            self.assertEqual(list(workdir.glob("*.mp3")), [])

    def test_oversize_item_is_generation_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audio, "ITEM_MAX_BYTES", 8):
                with self.assertRaises(audio.AudioError) as ctx:
                    self.finalize("wav", Path(tmp))
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_exhausted_request_budget_is_generation_failed(self):
        budget = audio.ByteBudget(limit=10)
        budget.spend(10)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("wav", Path(tmp), budget=budget)
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_invalid_wav_is_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("wav", workdir, data=b"RIFFnope")
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_expired_deadline_is_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("wav", Path(tmp),
                              deadline=time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")

    def test_unknown_format_is_generation_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("opus", Path(tmp))
        self.assertEqual(ctx.exception.code, "generation_failed")


if __name__ == "__main__":
    unittest.main()
