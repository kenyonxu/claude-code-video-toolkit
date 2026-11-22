"""TTS audio pipeline: WAV validation, WAV/MP3 finalization, ffprobe, budget.

Input is always the worker's PCM16 mono 22050 Hz WAV bytes (never a ComfyUI
history entry, never a FLAC intermediate). mp3 is a real FFmpeg libmp3lame
transcode; wav is validated passthrough. Every final file is produced via a
temp name plus os.replace, must pass ffprobe with duration > 0, and every
failure raises AudioError carrying the route's code (generation_failed or
tts_backend_timeout). No TTS business parameters live here (tts.py owns
those).
"""
from __future__ import annotations

import os
import struct
import subprocess
import time
from pathlib import Path

ITEM_MAX_BYTES = 64 * 1024 * 1024          # per worker WAV payload
REQUEST_BUDGET_BYTES = 128 * 1024 * 1024   # all items of one request
WAV_SAMPLE_RATE = 22050
MP3_CODEC = "libmp3lame"
_STDERR_SNIPPET = 200


class AudioError(Exception):
    """Pipeline error with a route-level code."""

    def __init__(self, message: str, code: str = "generation_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


class ByteBudget:
    """Tracks the bytes still allowed for worker WAV payloads in one request."""

    def __init__(self, limit: int = REQUEST_BUDGET_BYTES):
        self.remaining = limit

    def max_bytes_for_next(self) -> int:
        return min(ITEM_MAX_BYTES, max(0, self.remaining))

    def spend(self, n: int) -> None:
        self.remaining -= n


def _remaining(deadline: float, stage: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise AudioError(
            f"clone deadline reached before {stage}",
            code="tts_backend_timeout")
    return left


def parse_wav_header(data: bytes) -> dict:
    """Walk the RIFF chunk list to the fmt chunk; return its PCM fields."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("payload is not a RIFF/WAVE stream")
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk_id == b"fmt ":
            if len(body) < 16:
                raise AudioError("WAV fmt chunk is truncated")
            (audio_format, channels, rate, _byte_rate, _block_align,
             bits) = struct.unpack_from("<HHIIHH", body, 0)
            return {"audio_format": audio_format, "channels": channels,
                    "sample_rate": rate, "bits_per_sample": bits}
        pos += 8 + size + (size & 1)
    raise AudioError("WAV stream has no fmt chunk")


def validate_wav(data: bytes) -> None:
    """Require PCM16, mono, 22050 Hz -- exactly what the worker must emit."""
    fmt = parse_wav_header(data)
    if fmt["audio_format"] != 1 or fmt["bits_per_sample"] != 16:
        raise AudioError(
            "worker WAV must be PCM16 (uncompressed 16-bit), got"
            f" audio_format={fmt['audio_format']}"
            f" bits={fmt['bits_per_sample']}")
    if fmt["channels"] != 1:
        raise AudioError(
            f"worker WAV must be mono, got {fmt['channels']} channels")
    if fmt["sample_rate"] != WAV_SAMPLE_RATE:
        raise AudioError(
            f"worker WAV must be {WAV_SAMPLE_RATE} Hz, got"
            f" {fmt['sample_rate']} Hz")


def ffprobe_duration(path: Path, deadline: float) -> float:
    """Run ffprobe on a file; require returncode 0 and duration > 0."""
    timeout = _remaining(deadline, "ffprobe")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AudioError(
            f"ffprobe timed out after {timeout:.0f}s",
            code="tts_backend_timeout") from e
    except OSError as e:
        raise AudioError(f"ffprobe failed to run: {e}") from e
    if result.returncode != 0:
        raise AudioError(
            "ffprobe rejected the generated audio:"
            f" {result.stderr.strip()[:_STDERR_SNIPPET]}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as e:
        raise AudioError(
            "ffprobe returned no parsable duration:"
            f" {result.stdout.strip()[:_STDERR_SNIPPET]!r}") from e
    if not duration > 0:
        raise AudioError(f"ffprobe reported duration {duration!r}")
    return duration


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _run_ffmpeg(args: list[str], deadline: float, stage: str) -> None:
    timeout = _remaining(deadline, stage)
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AudioError(
            f"{stage} timed out after {timeout:.0f}s",
            code="tts_backend_timeout") from e
    except OSError as e:
        raise AudioError(f"{stage} failed to run: {e}") from e
    if result.returncode != 0:
        raise AudioError(
            f"{stage} failed: {result.stderr.strip()[:_STDERR_SNIPPET]}")


def finalize_output(wav_bytes: bytes, output_format: str, workdir: Path,
                    stem: str, budget: ByteBudget,
                    deadline: float) -> tuple[bytes, float]:
    """Turn one worker WAV payload into verified final bytes + duration."""
    if not wav_bytes:
        raise AudioError("worker returned empty WAV bytes")
    if len(wav_bytes) > budget.max_bytes_for_next():
        raise AudioError(
            f"worker WAV output is {len(wav_bytes)} bytes; the per-item"
            f" limit is {ITEM_MAX_BYTES} bytes and this request has"
            f" {max(0, budget.remaining)} bytes of budget left")
    validate_wav(wav_bytes)
    budget.spend(len(wav_bytes))
    if output_format not in ("mp3", "wav"):
        raise AudioError(f"unsupported output_format: {output_format!r}")
    workdir.mkdir(parents=True, exist_ok=True)
    src_path = workdir / f"{stem}.src.wav"
    _write_atomic(src_path, wav_bytes)
    if output_format == "wav":
        final_path = workdir / f"{stem}.wav"
        os.replace(src_path, final_path)
        duration = ffprobe_duration(final_path, deadline)
        return final_path.read_bytes(), duration
    final_path = workdir / f"{stem}.mp3"
    tmp_path = workdir / f"{stem}.out.mp3"
    _run_ffmpeg(["ffmpeg", "-y", "-i", str(src_path),
                 "-c:a", MP3_CODEC, str(tmp_path)],
                deadline, "FFmpeg MP3 transcode")
    duration = ffprobe_duration(tmp_path, deadline)
    os.replace(tmp_path, final_path)
    src_path.unlink(missing_ok=True)
    return final_path.read_bytes(), duration
