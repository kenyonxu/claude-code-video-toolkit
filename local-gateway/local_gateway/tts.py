"""POST /tts/generate orchestration: validation, preflight, deadline, lock.

Clone-only. This module never builds a FastAPI response (server.py does);
it returns a success payload dict or raises TTSRouteError with the exact
code/HTTP-status contract of spec section 9. The total deadline starts at
route entry (before waiting for the lock) and covers upload, submit, poll,
view, ffprobe and FFmpeg. One lock per gateway process serializes whole
clone requests; /health never takes it.
"""
from __future__ import annotations

import base64
import binascii
import math
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import audio, registry, tts_workflow
from .comfyui import ComfyUIError, input_names, input_values
from .config import GatewaySettings

ALLOWED_FIELDS = frozenset({
    "model", "text", "mode", "language", "output_format",
    "ref_audio_base64", "ref_text", "temperature", "top_p",
})
MODE_FLAG_FIELDS = frozenset({"custom_voice", "voice_design"})
EXTERNAL_FIELDS = frozenset({"ref_audio_url", "r2"})
OUTPUT_FORMATS = ("mp3", "wav")
MAX_REF_AUDIO_BYTES = 16 * 1024 * 1024
CLONE_NODE = "AILab_Qwen3TTSVoiceClone_Advanced"
LOAD_AUDIO_NODE_NAME = "LoadAudio"
SAVE_NODE_NAME = "SaveAudioAdvanced"
CLONE_REQUIRED_INPUTS = ("target_text", "reference_text", "reference_audio",
                         "language")
_DATA_URL_PREFIX = "data:"
_MIME_SUFFIXES = {"audio/wav": ".wav", "audio/x-wav": ".wav",
                  "audio/mpeg": ".mp3", "audio/mp3": ".mp3"}
_REF_CONTENT_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg"}

ERROR_STATUS = {
    "mode_unsupported": 422,
    "validation_error": 422,
    "unknown_model": 422,
    "external_url_unsupported": 422,
    "dependency_missing": 503,
    "comfyui_unreachable": 503,
    "comfyui_request_timeout": 504,
    "comfyui_timeout": 504,
    "comfyui_error": 502,
    "generation_failed": 500,
}


class TTSRouteError(Exception):
    """Route-level error carrying a spec section 9 code and HTTP status."""

    def __init__(self, message: str, code: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details
        self.http_status = ERROR_STATUS.get(code, 500)


def _from_comfyui(e: ComfyUIError) -> TTSRouteError:
    code = e.code
    if code in ("execution_error", "node_missing"):
        code = "generation_failed"
    return TTSRouteError(e.message, code, details=e.details)


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
    ref_text: str
    temperature: float | None
    top_p: float | None


@dataclass(frozen=True)
class Preflight:
    language_choices: tuple[str, ...]


def _validation(message: str) -> TTSRouteError:
    return TTSRouteError(message, "validation_error")


def _dependency(message: str) -> TTSRouteError:
    return TTSRouteError(message, "dependency_missing")


def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def parse_request(body) -> TTSRequest:
    """Validate the clone request body (spec section 5). Raises TTSRouteError."""
    if not isinstance(body, dict):
        raise _validation("request body must be a JSON object")
    mode_flags = sorted(set(body) & MODE_FLAG_FIELDS)
    if mode_flags:
        raise TTSRouteError(
            "this gateway supports voice clone only; custom voice and voice"
            f" design are not implemented locally (field(s):"
            f" {', '.join(mode_flags)})",
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
            f"mode {mode!r} is not supported; this gateway implements"
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
        for i, item in enumerate(text):
            if not isinstance(item, str) or not item.strip():
                raise _validation(f"text[{i}] must be a non-empty string")
        texts = list(text)
    else:
        raise _validation(
            "text is required: a non-empty string or a list of them")

    language = body.get("language", "Auto")
    if isinstance(language, str):
        if not language.strip():
            raise _validation("language must be a non-empty string")
        languages = [language] * len(texts)
    elif isinstance(language, list):
        if len(language) != len(texts):
            raise _validation(
                f"language list must match text length {len(texts)},"
                f" got {len(language)}")
        for i, item in enumerate(language):
            if not isinstance(item, str) or not item.strip():
                raise _validation(
                    f"language[{i}] must be a non-empty string")
        languages = list(language)
    else:
        raise _validation("language must be a string or a list of strings")

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
                f"unknown model {model!r}; this route accepts only clone-capable"
                f" TTS models (for example {registry.ROUTE_TARGETS['tts']})",
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
        mime = (payload_text[len(_DATA_URL_PREFIX):comma]
                .split(";", 1)[0].strip().lower())
        payload_text = payload_text[comma + 1:]
        ref_suffix = _MIME_SUFFIXES.get(mime, ".mp3")
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

    ref_text = body.get("ref_text")
    if not isinstance(ref_text, str) or not ref_text.strip():
        raise _validation(
            "ref_text is required: the transcript of the reference audio")

    temperature = body.get("temperature")
    if temperature is not None:
        if not _finite_number(temperature):
            raise _validation("temperature must be a finite number")
        if not 0.1 <= temperature <= 2.0:
            raise _validation("temperature must be within 0.1-2.0")
    top_p = body.get("top_p")
    if top_p is not None:
        if not _finite_number(top_p):
            raise _validation("top_p must be a finite number")
        if not 0.0 <= top_p <= 1.0:
            raise _validation("top_p must be within 0.0-1.0")

    return TTSRequest(
        texts=tuple(texts), languages=tuple(languages),
        output_format=output_format, model=model,
        ref_audio_bytes=ref_bytes, ref_suffix=ref_suffix,
        ref_text=ref_text, temperature=temperature, top_p=top_p)


def preflight(settings: GatewaySettings, client_factory) -> Preflight:
    """Clone-only dependency check (spec section 7); runs before any submit."""
    missing_tools = [tool for tool in ("ffmpeg", "ffprobe")
                     if shutil.which(tool) is None]
    if missing_tools:
        raise _dependency(
            "voice clone needs ffmpeg and ffprobe on this gateway process's"
            f" PATH; missing: {', '.join(missing_tools)}")
    entry = registry.model_by_key(registry.ROUTE_TARGETS["tts"])
    if entry is None or settings.models_root is None:
        raise _dependency(
            "COMFYUI_MODELS_ROOT is not configured; the clone weights"
            " cannot be verified")
    for rel in entry.required_nonempty_directories:
        directory = Path(settings.models_root) / rel
        try:
            nonempty = directory.is_dir() and any(directory.iterdir())
        except OSError:
            nonempty = False
        if not nonempty:
            raise _dependency(
                f"clone weights directory {rel!r} under COMFYUI_MODELS_ROOT"
                " is missing or empty; the AILab node would fall back to"
                " snapshot_download")
    try:
        with client_factory(settings.base_url) as client:
            clone_meta = client.object_info(CLONE_NODE)
            load_meta = client.object_info(LOAD_AUDIO_NODE_NAME)
            save_meta = client.object_info(SAVE_NODE_NAME)
    except ComfyUIError as e:
        if e.code == "node_missing":
            raise _dependency(
                f"ComfyUI lacks a node required by voice clone: {e.message}"
                " Install/enable the AILab Qwen3-TTS custom node package."
            ) from e
        raise _from_comfyui(e) from e
    clone_inputs = input_names(clone_meta)
    for required in CLONE_REQUIRED_INPUTS:
        if required not in clone_inputs:
            raise _dependency(
                f"{CLONE_NODE} does not expose the {required!r} input")
    if "1.7B" not in (input_values(clone_meta, "model_size") or []):
        raise _dependency(f"{CLONE_NODE}.model_size does not offer 1.7B")
    languages = input_values(clone_meta, "language") or []
    if not languages:
        raise _dependency(f"{CLONE_NODE}.language exposes no choices")
    if "audio" not in input_names(load_meta):
        raise _dependency("LoadAudio does not expose the 'audio' input")
    formats = input_values(save_meta, "format") or []
    for needed in ("mp3", "flac"):
        if needed not in formats:
            raise _dependency(
                f"SaveAudioAdvanced.format does not offer {needed!r}")
    return Preflight(language_choices=tuple(str(v) for v in languages))


def _validate_languages(request: TTSRequest, choices: tuple[str, ...]) -> None:
    allowed = set(choices)
    for i, language in enumerate(request.languages):
        if language not in allowed:
            raise _validation(
                f"language[{i}] {language!r} is not offered by {CLONE_NODE}"
                f" (choices: {', '.join(choices)})")


def _require_remaining(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TTSRouteError(
            f"clone deadline reached at stage {stage}", "comfyui_timeout")


def _upload_reference(client, request: TTSRequest) -> str:
    """Upload the reference once; join the server-chosen name/subfolder."""
    filename = f"ccvt_clone_ref_{uuid.uuid4().hex}{request.ref_suffix}"
    content_type = _REF_CONTENT_TYPES.get(request.ref_suffix,
                                          "application/octet-stream")
    response = client.upload(filename, request.ref_audio_bytes, content_type)
    name = response.get("name")
    subfolder = response.get("subfolder", "")
    if not isinstance(name, str) or not name:
        raise TTSRouteError(
            "ComfyUI accepted the reference upload but returned no usable"
            f" name: {str(response)[:200]}",
            "generation_failed")
    if isinstance(subfolder, str) and subfolder:
        return f"{subfolder}/{name}"
    return name


def generate_clone(settings: GatewaySettings, client_factory, body: dict,
                   lock) -> dict:
    """Validate, serialize on `lock`, and run the whole clone request."""
    started = time.monotonic()
    request = parse_request(body)  # 422-class errors raise before the lock
    deadline = started + deadline_seconds(len(request.texts))
    remaining = max(0.0, deadline - time.monotonic())
    if not lock.acquire(timeout=remaining):
        raise TTSRouteError(
            "timed out waiting for the gateway's TTS lock; the clone request"
            " was never submitted", "comfyui_timeout")
    try:
        _require_remaining(deadline, "lock")
        return _generate_locked(settings, client_factory, request, deadline,
                                started)
    finally:
        lock.release()


def _generate_locked(settings: GatewaySettings, client_factory,
                     request: TTSRequest, deadline: float,
                     started: float) -> dict:
    _require_remaining(deadline, "preflight")
    preflight_result = preflight(settings, client_factory)
    _validate_languages(request, preflight_result.language_choices)
    budget = audio.ViewBudget()
    outputs: list[dict] = []
    with client_factory(settings.base_url) as client:
        reference_audio = _upload_reference(client, request)
        with tempfile.TemporaryDirectory(prefix="ccvt_tts_") as tmp:
            workdir = Path(tmp)
            for index, text in enumerate(request.texts):
                _require_remaining(deadline, f"item {index}")
                try:
                    prompt = tts_workflow.build_clone_prompt(
                        text=text, ref_text=request.ref_text,
                        reference_audio=reference_audio,
                        language=request.languages[index],
                        output_format=request.output_format,
                        output_prefix=f"ccvt/tts/{uuid.uuid4().hex}/{index}",
                        temperature=request.temperature,
                        top_p=request.top_p)
                    prompt_id = client.submit(prompt)
                    entry = client.wait(prompt_id, deadline)
                    audio_bytes, duration = audio.finalize_output(
                        client, entry, request.output_format, workdir,
                        f"item_{index}", budget, deadline)
                except ComfyUIError as e:
                    raise _from_comfyui(e) from e
                except ValueError as e:
                    raise TTSRouteError(
                        f"clone audio processing failed: {e}",
                        "generation_failed") from e
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
