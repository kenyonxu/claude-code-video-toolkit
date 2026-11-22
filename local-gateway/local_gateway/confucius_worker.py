"""Confucius4-TTS worker: loads ConfuciusTTS once; /health and POST /api/tts.

Runs in an isolated environment cloned from the validated confuciustts
conda env (E:/GitHub/_confucius4_tts_venv) with basic
Transformers -- never vLLM, never ComfyUI, never a Qwen workflow. The
gateway process never imports this module at runtime; gateway tests import
it with a fake engine, so every torch-dependent import stays inside
ConfuciusEngine and this module imports cleanly without torch.

Endpoints:
    GET  /health   {"status": "starting"|"ok"|"error", "sample_rate": 22050}
    POST /api/tts  multipart text/lang/reference -> PCM16 mono 22050 Hz WAV
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SAMPLE_RATE = 22050
MAX_TEXT_CHARS = 1024
MAX_REFERENCE_BYTES = 16 * 1024 * 1024
LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "es", "id", "it",
             "ms", "pt", "ru", "th", "vi")


def pcm16_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Encode raw little-endian int16 mono samples as a PCM16 WAV stream."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


class ConfuciusEngine:
    """Thin adapter over confuciustts.cli.inference.ConfuciusTTS.

    All torch/transformers imports live here so importing this module stays
    torch-free (gateway tests substitute a fake engine).
    """

    def __init__(self, config_path: Path, device: str = "cuda"):
        from confuciustts.cli.inference import ConfuciusTTS

        self._model = ConfuciusTTS(config_path=str(config_path), device=device)
        # Upstream inference.py sets this from audio.target_sample_rate.
        self.sample_rate = int(self._model.sample_rate)
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError(
                f"Confucius4-TTS must emit {SAMPLE_RATE} Hz, got"
                f" {self.sample_rate} Hz")

    def generate(self, text: str, lang: str, prompt_path: str) -> bytes:
        import numpy as np

        audio = self._model.generate(text, lang, prompt_path)
        samples = audio.detach().cpu().numpy().reshape(-1)
        samples = np.clip(samples.astype("float64"), -1.0, 1.0)
        pcm = (samples * 32767.0).astype("<i2").tobytes()
        return pcm16_wav_bytes(pcm, self.sample_rate)


@dataclass
class WorkerState:
    status: str = "starting"          # starting | ok | error
    sample_rate: int | None = None
    error: str | None = None          # sanitized, never a traceback
    engine: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _sanitize(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def start_loader(state: WorkerState, config_path: Path,
                 device: str = "cuda") -> threading.Thread:
    """Load the model once in the background; /health serves meanwhile."""

    def run() -> None:
        try:
            engine = ConfuciusEngine(config_path, device)
        except Exception as e:  # loading failure -> error state, no traceback out
            logger.exception("ConfuciusTTS failed to load")
            state.status = "error"
            state.error = _sanitize(e)
            return
        state.engine = engine
        state.sample_rate = engine.sample_rate
        state.status = "ok"

    thread = threading.Thread(target=run, daemon=True, name="confucius-loader")
    thread.start()
    return thread


def create_worker_app(state: WorkerState | None = None) -> FastAPI:
    worker_state = state if state is not None else WorkerState()
    app = FastAPI(title="ccvt confucius4-tts worker", version="0.1.0")
    app.state.worker = worker_state

    @app.get("/health")
    def health() -> JSONResponse:
        payload = {"status": worker_state.status,
                   "sample_rate": worker_state.sample_rate}
        if worker_state.status == "error" and worker_state.error:
            payload["error"] = worker_state.error
        status_code = 200 if worker_state.status == "ok" else 503
        return JSONResponse(content=payload, status_code=status_code)

    @app.post("/api/tts")
    def tts(text: str = Form(...), lang: str = Form(...),
            reference: UploadFile = File(...)) -> Response:
        text = text or ""
        problems: list[str] = []
        if not text.strip():
            problems.append("text must be a non-empty string")
        if len(text) > MAX_TEXT_CHARS:
            problems.append(f"text exceeds {MAX_TEXT_CHARS} characters")
        if lang not in LANGUAGES:
            problems.append(
                f"lang must be one of {', '.join(LANGUAGES)}; got {lang!r}")
        payload = reference.file.read(MAX_REFERENCE_BYTES + 1) or b""
        if not payload:
            problems.append("reference audio is empty")
        if len(payload) > MAX_REFERENCE_BYTES:
            problems.append(
                f"reference audio exceeds {MAX_REFERENCE_BYTES} bytes")
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"error": "; ".join(problems), "code": "validation"})
        if worker_state.status != "ok" or worker_state.engine is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "worker is not ready (status:"
                        f" {worker_state.status})", "code": "not_ready"})
        suffix = Path(reference.filename or "reference.audio").suffix or ".audio"
        with tempfile.NamedTemporaryFile(prefix="ccvt_ref_", suffix=suffix,
                                         delete=False) as tmp:
            tmp.write(payload)
            prompt_path = tmp.name
        try:
            with worker_state.lock:
                wav = worker_state.engine.generate(text, lang, prompt_path)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Confucius generation failed")
            raise HTTPException(
                status_code=500,
                detail={"error": "generation failed on the worker",
                        "code": "generation_failed"})
        finally:
            Path(prompt_path).unlink(missing_ok=True)
        return Response(content=wav, media_type="audio/wav")

    return app


def apply_offline_env(confucius_root: Path | None = None) -> None:
    """Pin repo-local weights and forbid downloads before any HF import."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if confucius_root is not None:
        os.environ["HF_HUB_CACHE"] = str(confucius_root / "models" / "hf")


class ConfigError(Exception):
    """The upstream inference config no longer has the expected checkpoint
    lines; the worker refuses to guess where its tokenizer lives."""


# Upstream's config locates the tokenizer and the w2v-bert statistics
# relative to the interpreter's working directory. Only these two keys are
# rewritten; the checkout itself is never written.
CHECKPOINT_PATH_KEYS = ("tokenizer_path", "w2v_stat")
UPSTREAM_CONFIG = ("config", "inference_config.yaml")


def _checkpoint_line(key: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?P<indent>[ \t]*){key}:[ \t]*(?P<value>\S+)[ \t]*"
        rf"(?P<ending>\r?)$")


def rewrite_checkpoint_paths(source: str, confucius_root: Path) -> str:
    """Point the two checkpoint lines of an inference config at the checkout.

    `source` is the text of upstream ``config/inference_config.yaml``. The
    ``tokenizer_path`` and ``w2v_stat`` values must each be a ``./`` relative
    path on exactly one line; they become absolute POSIX paths under
    `confucius_root`, quoted so that any checkout location stays valid YAML.
    Every other byte is kept. Anything else raises ConfigError, so a changed
    upstream layout fails at startup instead of loading the wrong files.
    """
    root = confucius_root.resolve().as_posix()
    lines = source.split("\n")
    for key in CHECKPOINT_PATH_KEYS:
        pattern = _checkpoint_line(key)
        hits = [i for i, line in enumerate(lines) if pattern.match(line)]
        if len(hits) != 1:
            raise ConfigError(
                f"upstream inference config must declare {key} on exactly"
                f" one line; found {len(hits)}")
        match = pattern.match(lines[hits[0]])
        value = match.group("value")
        if not value.startswith("./"):
            raise ConfigError(
                f"upstream inference config {key} must be a ./ relative"
                f" path; got {value}")
        absolute = f"{root}/{value[2:]}"
        lines[hits[0]] = (f"{match.group('indent')}{key}:"
                          f" {json.dumps(absolute)}{match.group('ending')}")
    return "\n".join(lines)


def write_runtime_config(confucius_root: Path, target_dir: Path) -> Path:
    """Write the rewritten inference config into `target_dir` and return it.

    The checkout is only read; the copy in `target_dir` is what
    ConfuciusTTS loads.
    """
    source_path = confucius_root.joinpath(*UPSTREAM_CONFIG)
    with open(source_path, encoding="utf-8", newline="") as handle:
        rewritten = rewrite_checkpoint_paths(handle.read(), confucius_root)
    target = target_dir / UPSTREAM_CONFIG[-1]
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(rewritten)
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Confucius4-TTS local worker (basic Transformers)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=None,
                        help="Confucius4-TTS checkout; required here or via"
                             " $CONFUCIUS4_TTS_ROOT (never hardcoded in the"
                             " committed launcher)")
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)

    root_value = args.root or os.environ.get("CONFUCIUS4_TTS_ROOT")
    if not root_value:
        parser.error("--root or CONFUCIUS4_TTS_ROOT is required")
    root = Path(root_value)
    if not root.is_dir():
        print(f"Error: Confucius4-TTS root not found: {root}", file=sys.stderr)
        sys.exit(2)
    apply_offline_env(root)
    sys.path.insert(0, str(root))  # confuciustts + external/ live in the repo
    # The rewritten config lives in a process-temporary directory that is
    # removed once uvicorn exits; the checkout itself is only ever read.
    with tempfile.TemporaryDirectory(prefix="ccvt_confucius_") as config_dir:
        try:
            config_path = write_runtime_config(root, Path(config_dir))
        except (OSError, UnicodeError, ConfigError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)

        state = WorkerState()
        start_loader(state, config_path, args.device)
        import uvicorn

        uvicorn.run(create_worker_app(state), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
