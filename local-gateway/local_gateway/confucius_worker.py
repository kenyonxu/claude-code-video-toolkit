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
import logging
import os
import subprocess
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


def create_directory_junction(link: Path, target: Path) -> None:
    """Windows directory junction (no admin rights needed) or a symlink."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, errors="replace", shell=False)
        if result.returncode != 0 or not link.exists():
            raise RuntimeError(
                f"mklink /J {link} -> {target} failed:"
                f" {result.stderr.strip()[:200]}")
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def ensure_checkpoints_link(ccvt_root: Path, confucius_root: Path) -> None:
    """Satisfy ./checkpoints relative paths without touching the Confucius
    checkout or switching the interpreter's working directory."""
    link = ccvt_root / "checkpoints"
    if link.exists():
        return
    create_directory_junction(link, confucius_root / "checkpoints")


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
    config_path = root / "config" / "inference_config.yaml"
    sys.path.insert(0, str(root))  # confuciustts + external/ live in the repo
    try:
        ensure_checkpoints_link(Path.cwd(), root)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    state = WorkerState()
    start_loader(state, config_path, args.device)
    import uvicorn

    uvicorn.run(create_worker_app(state), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
