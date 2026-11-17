"""Model registry: plain data and pure lookups only.

No filesystem access, no network, no plugin mechanism. File paths are
relative to COMFYUI_MODELS_ROOT with forward slashes; required_options values
are the exact strings ComfyUI lists in the node input's choice list, which
for first-release weights is the bare filename (loader choices are relative
to their own model folder). The P1 node sets are the health baseline only;
P2-P4 must re-verify /object_info before building workflows. Cloud API nodes
never belong here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RequiredOption:
    """A value that must appear in a node input's allowed choices."""

    node: str
    input: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ModelEntry:
    key: str
    capability: str
    required_files: tuple[str, ...] = ()
    required_directories: tuple[str, ...] = ()
    optional_files: tuple[str, ...] = ()
    required_nodes: tuple[str, ...] = ()
    required_options: tuple[RequiredOption, ...] = ()


MODEL_ENTRIES: tuple[ModelEntry, ...] = (
    ModelEntry(
        key="qwen3-tts-1.7B",
        capability="tts",
        required_directories=("TTS/Qwen3-TTS/",),
        required_nodes=(
            "AILab_Qwen3TTSCustomVoice",
            "AILab_Qwen3TTSVoiceClone",
            "AILab_Qwen3TTSVoiceDesign",
            "LoadAudio",
            "SaveAudioAdvanced",
        ),
        required_options=(
            RequiredOption("AILab_Qwen3TTSCustomVoice", "model_size", ("1.7B",)),
            RequiredOption("AILab_Qwen3TTSVoiceClone", "model_size", ("1.7B",)),
            RequiredOption("AILab_Qwen3TTSVoiceDesign", "model_size", ("1.7B",)),
            RequiredOption("AILab_Qwen3TTSCustomVoice", "speaker", ("Ryan",)),
        ),
    ),
    ModelEntry(
        key="qwen-image-2.1",
        capability="image",
        required_files=(
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ),
        required_nodes=(
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "TextEncodeQwenImage21",
            "KSampler",
            "SaveImage",
            "LoadImage",
        ),
        required_options=(
            RequiredOption(
                "UNETLoader", "unet_name",
                ("qwen_image_2.1_turbo_int8_convrot.safetensors",),
            ),
            RequiredOption(
                "CLIPLoader", "clip_name",
                ("qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
                 "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"),
            ),
            RequiredOption(
                "VAELoader", "vae_name",
                ("qwen_image_2.1_vae_bf16.safetensors",),
            ),
            RequiredOption("CLIPLoader", "type", ("qwen_image",)),
        ),
    ),
    ModelEntry(
        key="minimax-h3-fl2va",
        capability="video",
        required_files=(
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            # Double extension is the real on-disk filename; do not "fix" it.
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ),
        optional_files=("vae/minimax_h3_audio_vae_fp32.safetensors",),
        required_nodes=(
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "EmptyMiniMaxH3LatentAV",
            "MiniMaxH3ImageToVideo",
            "KSampler",
            "VAEDecode",
            "VHS_VideoCombine",
        ),
        required_options=(
            RequiredOption(
                "UNETLoader", "unet_name",
                ("minimax_h3_fl2va_pruned_int8_convrot.safetensors",),
            ),
            RequiredOption(
                "CLIPLoader", "clip_name",
                ("qwen3vl_32b_minimax_h3_int8_convrot.safetensors",),
            ),
            RequiredOption(
                "VAELoader", "vae_name",
                ("minimax_h3_video_vae_fp16.safetensors",
                 "minimax_h3_image_vae_fp16.safetensors.safetensors"),
            ),
            RequiredOption("CLIPLoader", "type", ("minimax",)),
            RequiredOption("VHS_VideoCombine", "format", ("video/h264-mp4",)),
        ),
    ),
)

ROUTE_TARGETS: dict[str, str] = {
    "tts": "qwen3-tts-1.7B",
    "image_generate": "qwen-image-2.1",
    "image_edit": "qwen-image-2.1",
    "video": "minimax-h3-fl2va",
}

# Routes whose POST endpoint actually works. P1 implements none of them; a
# later phase flips a route only after its real generation acceptance passes.
IMPLEMENTED_ROUTES: frozenset[str] = frozenset()


def required_nodes() -> tuple[str, ...]:
    """Unique required node names across all entries, first-seen order."""
    seen: dict[str, None] = {}
    for entry in MODEL_ENTRIES:
        for node in entry.required_nodes:
            seen.setdefault(node, None)
    return tuple(seen)


def model_by_key(key: str) -> ModelEntry | None:
    for entry in MODEL_ENTRIES:
        if entry.key == key:
            return entry
    return None
