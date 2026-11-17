"""TTS audio pipeline: bounded view download, ffprobe validation, WAV transcode.

No TTS business parameters here (tts.py owns those). This module turns one
finished ComfyUI history entry into verified final bytes plus a duration:
MP3 passes through untouched, WAV comes from a FLAC intermediate transcoded
with an FFmpeg argument array (shell=False). Every stage consumes the
request deadline; every failure raises ComfyUIError with the spec's code.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import tts_workflow
from .comfyui import ComfyUIError

VIEW_MAX_BYTES = 64 * 1024 * 1024          # per /view file
REQUEST_BUDGET_BYTES = 128 * 1024 * 1024   # all outputs of one request
_STDERR_SNIPPET = 200


class ViewBudget:
    """Tracks the bytes still allowed for /view downloads in one request."""

    def __init__(self, limit: int = REQUEST_BUDGET_BYTES):
        self.remaining = limit

    def max_bytes_for_next(self) -> int:
        return min(VIEW_MAX_BYTES, max(0, self.remaining))

    def spend(self, n: int) -> None:
        self.remaining -= n


def _remaining(deadline: float, stage: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ComfyUIError(
            f"clone deadline reached before {stage}", code="comfyui_timeout")
    return left


def download_history_audio(client, history_entry: dict, save_node_id: str,
                           budget: ViewBudget, deadline: float) -> bytes:
    """Locate the audio metadata in the history entry and view() it bounded."""
    meta = tts_workflow.locate_history_audio(history_entry, save_node_id)
    _remaining(deadline, "audio download")
    data = client.view(meta["filename"], meta["subfolder"], meta["type"],
                       max_bytes=budget.max_bytes_for_next())
    budget.spend(len(data))
    return data


def ffprobe_duration(path: Path, deadline: float) -> float:
    """Run ffprobe on a file; require returncode 0 and duration > 0."""
    timeout = _remaining(deadline, "ffprobe")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ComfyUIError(
            f"ffprobe timed out after {timeout:.0f}s",
            code="comfyui_timeout") from e
    except OSError as e:
        raise ComfyUIError(
            f"ffprobe failed to run: {e}", code="generation_failed") from e
    if result.returncode != 0:
        raise ComfyUIError(
            f"ffprobe rejected the generated audio:"
            f" {result.stderr.strip()[:_STDERR_SNIPPET]}",
            code="generation_failed")
    try:
        duration = float(result.stdout.strip())
    except ValueError as e:
        raise ComfyUIError(
            f"ffprobe returned no parsable duration:"
            f" {result.stdout.strip()[:_STDERR_SNIPPET]!r}",
            code="generation_failed") from e
    if not duration > 0:
        raise ComfyUIError(
            f"ffprobe reported duration {duration!r}",
            code="generation_failed")
    return duration


def flac_to_wav(flac_path: Path, wav_path: Path, deadline: float) -> None:
    """Transcode FLAC bytes to pcm_s16le WAV with an argument array."""
    timeout = _remaining(deadline, "FFmpeg WAV transcode")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(flac_path), "-c:a", "pcm_s16le",
             str(wav_path)],
            capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ComfyUIError(
            f"FFmpeg WAV transcode timed out after {timeout:.0f}s",
            code="comfyui_timeout") from e
    except OSError as e:
        raise ComfyUIError(
            f"FFmpeg failed to run: {e}", code="generation_failed") from e
    if result.returncode != 0 or not wav_path.is_file():
        raise ComfyUIError(
            f"FFmpeg WAV transcode failed:"
            f" {result.stderr.strip()[:_STDERR_SNIPPET]}",
            code="generation_failed")


def finalize_output(client, history_entry: dict, output_format: str,
                    workdir: Path, stem: str, budget: ViewBudget,
                    deadline: float) -> tuple[bytes, float]:
    """Download, finalize and verify one item's audio; returns (bytes, duration)."""
    raw = download_history_audio(client, history_entry,
                                 tts_workflow.SAVE_NODE, budget, deadline)
    if output_format == "mp3":
        final_path = workdir / f"{stem}.mp3"
        final_path.write_bytes(raw)
    else:
        flac_path = workdir / f"{stem}.flac"
        wav_path = workdir / f"{stem}.wav"
        flac_path.write_bytes(raw)
        flac_to_wav(flac_path, wav_path, deadline)
        final_path = wav_path
    duration = ffprobe_duration(final_path, deadline)
    return final_path.read_bytes(), duration
