"""Original VoiceClone Advanced prompt template, patching and history lookup.

Pure data functions: no network, no filesystem writes. The template lives in
workflows/tts_voice_clone.json next to this file and is re-loaded (deep, via
json round-trip on every call) so callers can never mutate shared state.
Node ids are stable; build_clone_prompt only touches whitelisted inputs.
"""
from __future__ import annotations

import json
from pathlib import Path

LOAD_AUDIO_NODE = "1"
TTS_NODE = "2"
SAVE_NODE = "3"

TEMPLATE_PATH = Path(__file__).resolve().parent / "workflows" / "tts_voice_clone.json"
OUTPUT_FORMATS = ("mp3", "wav")

# Fixed VoiceClone Advanced values (spec section 6.1). Sampling params are
# deliberately absent: they are patched only when the caller provides them.
FIXED_TTS_INPUTS = {
    "model_size": "1.7B",
    "max_new_tokens": 2048,
    "top_k": 50,
    "repetition_penalty": 1.0,
    "attention": "auto",
    "device": "auto",
    "precision": "bf16",
    "unload_models": True,
    "seed": -1,
    "x_vector_only": False,
}


def load_template() -> dict:
    """Return a fresh copy of the API prompt template."""
    with TEMPLATE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def build_clone_prompt(*, text: str, ref_text: str, reference_audio: str,
                       language: str, output_format: str, output_prefix: str,
                       temperature: float | None = None,
                       top_p: float | None = None) -> dict:
    """Patch one clone prompt; only whitelisted inputs are modified.

    SaveAudioAdvanced.format stays a flat string: mp3 adds the separate
    format.quality="V0" input, flac (the WAV intermediate) never writes a
    quality key. do_sample flips to true only when a sampling param is
    explicitly provided, and only provided values are patched in.
    """
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"unsupported output_format: {output_format!r}")
    if not output_prefix.strip():
        raise ValueError("output_prefix must be a non-empty unique per-item prefix")
    prompt = load_template()
    prompt[LOAD_AUDIO_NODE]["inputs"]["audio"] = reference_audio
    tts_inputs = prompt[TTS_NODE]["inputs"]
    tts_inputs["target_text"] = text
    tts_inputs["reference_text"] = ref_text
    tts_inputs["language"] = language
    if temperature is not None or top_p is not None:
        tts_inputs["do_sample"] = True
        if temperature is not None:
            tts_inputs["temperature"] = temperature
        if top_p is not None:
            tts_inputs["top_p"] = top_p
    save_inputs = prompt[SAVE_NODE]["inputs"]
    save_inputs["filename_prefix"] = output_prefix
    if output_format == "mp3":
        save_inputs["format"] = "mp3"
        save_inputs["format.quality"] = "V0"
    else:
        save_inputs["format"] = "flac"
        save_inputs.pop("format.quality", None)
    return prompt


def locate_history_audio(entry: dict, save_node_id: str = SAVE_NODE) -> dict:
    """Return the single audio metadata dict from a finished history entry.

    Reads entry["outputs"][save_node_id]["audio"][0]. The dict must carry
    string filename/subfolder/type keys (subfolder may be empty); anything
    else (missing outputs, empty list, several entries, non-string keys)
    raises ValueError, which the route maps to generation_failed.
    """
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    node_out = outputs.get(save_node_id) if isinstance(outputs, dict) else None
    audio = node_out.get("audio") if isinstance(node_out, dict) else None
    if not isinstance(audio, list) or len(audio) != 1:
        raise ValueError(
            f"expected exactly one audio entry on output node {save_node_id!r},"
            f" got {audio!r}")
    meta = audio[0]
    if not isinstance(meta, dict):
        raise ValueError(f"audio metadata is not an object: {meta!r}")
    for key in ("filename", "subfolder", "type"):
        if not isinstance(meta.get(key), str):
            raise ValueError(f"audio metadata misses string {key!r}: {meta!r}")
    if not meta["filename"] or not meta["type"]:
        raise ValueError(f"audio metadata has empty filename/type: {meta!r}")
    return meta
