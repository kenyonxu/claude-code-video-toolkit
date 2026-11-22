"""POST /tts/generate orchestration: validation, preflight, deadline, lock.

Confucius4-TTS clone-only. This module never builds a FastAPI response
(server.py does); it returns a success payload dict or raises TTSRouteError
with the exact code/HTTP-status contract of spec section 7. The total
deadline starts at route entry (before waiting for the lock) and covers
preflight, every per-item multipart POST, the bounded WAV download, ffprobe
and FFmpeg. One lock per gateway process serializes whole clone requests;
/health never takes it (the worker keeps its own generation lock).
"""
from __future__ import annotations

import base64
import binascii
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import audio, registry
from .confucius_client import ConfuciusError
from .config import GatewaySettings

ALLOWED_FIELDS = frozenset({
    "model", "text", "mode", "language", "output_format",
    "ref_audio_base64",
})
# These describe capabilities the Confucius route deliberately does not
# have (Qwen sampling, builtin speakers, transcripts, designed voices):
# they fail as mode_unsupported, not as unknown fields.
MODE_FLAG_FIELDS = frozenset({
    "ref_text", "speaker", "instruct", "temperature", "top_p",
    "custom_voice", "voice_design",
})
EXTERNAL_FIELDS = frozenset({"ref_audio_url", "r2"})
OUTPUT_FORMATS = ("mp3", "wav")
MAX_REF_AUDIO_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 1024
SUPPORTED_LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "es", "id",
                       "it", "ms", "pt", "ru", "th", "vi")
_DATA_URL_PREFIX = "data:"
_MIME_SUFFIXES = {"audio/wav": ".wav", "audio/x-wav": ".wav",
                  "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
                  "audio/flac": ".flac", "audio/x-flac": ".flac"}
_REF_CONTENT_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                      ".flac": "audio/flac"}

ERROR_STATUS = {
    "mode_unsupported": 422,
    "validation_error": 422,
    "unknown_model": 422,
    "external_url_unsupported": 422,
    "dependency_missing": 503,
    "tts_backend_unavailable": 503,
    "tts_backend_timeout": 504,
    "tts_backend_error": 502,
    "generation_failed": 500,
}


class TTSRouteError(Exception):
    """Route-level error carrying a spec section 7 code and HTTP status."""

    def __init__(self, message: str, code: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details
        self.http_status = ERROR_STATUS.get(code, 500)


def _from_confucius(e: ConfuciusError) -> TTSRouteError:
    return TTSRouteError(e.message, e.code, details=e.details)


def deadline_seconds(item_count: int) -> int:
    """270 * max(1, item_count // 3 + 1), chunk size from the registry."""
    entry = registry.model_by_key(registry.ROUTE_TARGETS["tts"])
    chunk = entry.deadline_chunk_seconds if entry else 270
    return chunk * max(1, item_count // 3 + 1)


@dataclass(frozen=True)
class TTSRequest:
    texts: tuple[str, ...]
    languages: tuple[str, ...]
    output_format: str
    model: str
    ref_audio_bytes: bytes
    ref_suffix: str


def _validation(message: str) -> TTSRouteError:
    return TTSRouteError(message, "validation_error")


def _dependency(message: str) -> TTSRouteError:
    return TTSRouteError(message, "dependency_missing")


def parse_request(body) -> TTSRequest:
    """Validate the clone request body (spec section 5). Raises TTSRouteError."""
    if not isinstance(body, dict):
        raise _validation("request body must be a JSON object")
    mode_flags = sorted(set(body) & MODE_FLAG_FIELDS)
    if mode_flags:
        raise TTSRouteError(
            "this route supports voice clone only; field(s)"
            f" {', '.join(mode_flags)} belong to capabilities that are not"
            " implemented on the Confucius4-TTS route (clone needs only"
            " ref_audio_base64 and language)",
            "mode_unsupported")
    external = sorted(set(body) & EXTERNAL_FIELDS)
    if external:
        raise TTSRouteError(
            f"field {external[0]!r} is not supported: the local gateway"
            " clones from ref_audio_base64 and returns base64 only; it"
            " never downloads from or uploads to external storage",
            "external_url_unsupported")
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise _validation(f"unknown request field(s): {', '.join(unknown)}")
    mode = body.get("mode", "clone")
    if mode != "clone":
        raise TTSRouteError(
            f"mode {mode!r} is not supported; this route implements"
            " 'clone' only",
            "mode_unsupported")

    entry = registry.model_by_key(registry.ROUTE_TARGETS["tts"])
    batch_limit = entry.max_batch_items if entry else 8
    text = body.get("text")
    if isinstance(text, str):
        if not text.strip():
            raise _validation("text must be a non-empty string")
        texts = [text]
    elif isinstance(text, list):
        if not 1 <= len(text) <= batch_limit:
            raise _validation(
                f"text must have 1..{batch_limit} items, got {len(text)}")
        texts = list(text)
    else:
        raise _validation(
            "text is required: a non-empty string or a list of them")
    for i, item in enumerate(texts):
        if not isinstance(item, str) or not item.strip():
            raise _validation(f"text[{i}] must be a non-empty string")
        if len(item) > MAX_TEXT_CHARS:
            raise _validation(
                f"text[{i}] exceeds {MAX_TEXT_CHARS} characters"
                f" (got {len(item)})")

    language = body.get("language", "zh")
    if isinstance(language, str):
        languages = [language] * len(texts)
    elif isinstance(language, list):
        if len(language) != len(texts):
            raise _validation(
                f"language list must match text length {len(texts)},"
                f" got {len(language)}")
        languages = list(language)
    else:
        raise _validation("language must be a string or a list of strings")
    for i, item in enumerate(languages):
        if item not in SUPPORTED_LANGUAGES:
            raise _validation(
                f"language[{i}] must be one of"
                f" {', '.join(SUPPORTED_LANGUAGES)}; got {item!r}")

    output_format = body.get("output_format", "mp3")
    if output_format not in OUTPUT_FORMATS:
        raise _validation(
            f"output_format must be one of {OUTPUT_FORMATS},"
            f" got {output_format!r}")

    model = body.get("model")
    if model is None:
        model = registry.ROUTE_TARGETS["tts"]
    elif not isinstance(model, str) or not model:
        raise _validation("model must be a non-empty string")
    else:
        model_entry = registry.model_by_key(model)
        if (model_entry is None or model_entry.capability != "tts"
                or "clone" not in model_entry.modes):
            raise TTSRouteError(
                f"unknown model {model!r}; this route accepts only"
                f" {registry.ROUTE_TARGETS['tts']}",
                "unknown_model")

    ref_b64 = body.get("ref_audio_base64")
    if not isinstance(ref_b64, str) or not ref_b64:
        raise _validation("ref_audio_base64 is required for voice clone")
    ref_suffix = ".mp3"
    payload_text = ref_b64
    if payload_text.startswith(_DATA_URL_PREFIX):
        comma = payload_text.find(",")
        if comma < 0:
            raise _validation("ref_audio_base64 data URL has no comma")
        header = payload_text[len(_DATA_URL_PREFIX):comma]
        params = [p.strip().lower() for p in header.split(";") if p.strip()]
        mime = params[0] if params else ""
        if not mime:
            raise _validation(
                "ref_audio_base64 data URL has no MIME type")
        if "base64" not in params[1:]:
            raise _validation(
                "ref_audio_base64 data URL must declare the base64"
                " parameter")
        payload_text = payload_text[comma + 1:]
        ref_suffix = _MIME_SUFFIXES.get(mime, ".bin")
    try:
        ref_bytes = base64.b64decode(payload_text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise _validation(f"ref_audio_base64 is not valid base64: {e}") from e
    if not ref_bytes:
        raise _validation("ref_audio_base64 decodes to empty audio")
    if len(ref_bytes) > MAX_REF_AUDIO_BYTES:
        raise _validation(
            f"ref_audio_base64 decodes to {len(ref_bytes)} bytes; the limit"
            f" is {MAX_REF_AUDIO_BYTES}")

    return TTSRequest(
        texts=tuple(texts), languages=tuple(languages),
        output_format=output_format, model=model,
        ref_audio_bytes=ref_bytes, ref_suffix=ref_suffix)


def preflight(settings: GatewaySettings, tts_client_factory) -> None:
    """Dependency check (spec section 7); runs before any generation POST."""
    missing_tools = [tool for tool in ("ffmpeg", "ffprobe")
                     if shutil.which(tool) is None]
    if missing_tools:
        raise _dependency(
            "voice clone needs ffmpeg and ffprobe on this gateway process's"
            f" PATH; missing: {', '.join(missing_tools)}")
    try:
        payload = tts_client_factory(settings.tts_worker_url).health()
    except ConfuciusError as e:
        raise _from_confucius(e) from e
    if (payload.get("status") != "ok"
            or payload.get("sample_rate") != 22050):
        raise TTSRouteError(
            "Confucius4-TTS worker is not ready: /health must report"
            " status='ok' and sample_rate=22050; got status="
            f"{payload.get('status')!r}, sample_rate="
            f"{payload.get('sample_rate')!r}", "tts_backend_unavailable")


def _require_remaining(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TTSRouteError(
            f"clone deadline reached at stage {stage}",
            "tts_backend_timeout")


def generate_clone(settings: GatewaySettings, tts_client_factory, body: dict,
                   lock) -> dict:
    """Validate, serialize on `lock`, and run the whole clone request."""
    started = time.monotonic()
    request = parse_request(body)  # 422-class errors raise before the lock
    deadline = started + deadline_seconds(len(request.texts))
    remaining = max(0.0, deadline - time.monotonic())
    if not lock.acquire(timeout=remaining):
        raise TTSRouteError(
            "timed out waiting for the gateway's TTS lock; the clone request"
            " was never submitted", "tts_backend_timeout")
    try:
        _require_remaining(deadline, "lock")
        return _generate_locked(settings, tts_client_factory, request,
                                deadline, started)
    finally:
        lock.release()


def _generate_locked(settings: GatewaySettings, tts_client_factory,
                     request: TTSRequest, deadline: float,
                     started: float) -> dict:
    _require_remaining(deadline, "preflight")
    preflight(settings, tts_client_factory)
    client = tts_client_factory(settings.tts_worker_url)
    ref_name = f"ccvt_ref_{uuid.uuid4().hex}{request.ref_suffix}"
    content_type = _REF_CONTENT_TYPES.get(request.ref_suffix,
                                          "application/octet-stream")
    budget = audio.ByteBudget()
    outputs: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="ccvt_tts_") as tmp:
        workdir = Path(tmp)
        for index, text in enumerate(request.texts):
            _require_remaining(deadline, f"item {index}")
            try:
                wav_bytes = client.generate(
                    text=text, lang=request.languages[index],
                    reference_bytes=request.ref_audio_bytes,
                    filename=ref_name, content_type=content_type,
                    deadline=deadline)
                audio_bytes, duration = audio.finalize_output(
                    wav_bytes, request.output_format, workdir,
                    f"item_{index}", budget, deadline)
            except ConfuciusError as e:
                raise _from_confucius(e) from e
            except audio.AudioError as e:
                raise TTSRouteError(e.message, e.code) from e
            outputs.append({
                "audio_base64":
                    base64.b64encode(audio_bytes).decode("ascii"),
                "duration_seconds": duration,
            })
    payload = {
        "success": True,
        "mode": "clone",
        "model": request.model,
        "processing_time_seconds": round(time.monotonic() - started, 3),
        "outputs": outputs,
    }
    if len(outputs) == 1:
        payload["audio_base64"] = outputs[0]["audio_base64"]
        payload["duration_seconds"] = outputs[0]["duration_seconds"]
    return payload
