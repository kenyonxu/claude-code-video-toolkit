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
    # Directories that must exist AND contain at least one entry (P2 clone
    # weights: an empty dir makes the AILab node fall back to
    # snapshot_download, which the gateway must not trigger).
    required_nonempty_directories: tuple[str, ...] = ()
    optional_files: tuple[str, ...] = ()
    required_nodes: tuple[str, ...] = ()
    required_options: tuple[RequiredOption, ...] = ()
    # Capability metadata (P2): routes and preflight read these instead of
    # hardcoding. CustomVoice/VoiceDesign never appear in modes.
    modes: tuple[str, ...] = ()
    output_formats: tuple[str, ...] = ()
    max_batch_items: int = 0
    deadline_chunk_seconds: int = 0


MODEL_ENTRIES: tuple[ModelEntry, ...] = (
    ModelEntry(
        key="qwen3-tts-1.7B",
        capability="tts",
        required_directories=(),
        required_nonempty_directories=(
            "TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS/Qwen3-TTS-Tokenizer-12Hz",
        ),
        required_nodes=(
            "AILab_Qwen3TTSVoiceClone_Advanced",
            "LoadAudio",
            "SaveAudioAdvanced",
        ),
        required_options=(
            RequiredOption("AILab_Qwen3TTSVoiceClone_Advanced", "model_size",
                           ("1.7B",)),
            RequiredOption("SaveAudioAdvanced", "format", ("mp3", "flac")),
        ),
        modes=("clone",),
        output_formats=("mp3", "wav"),
        max_batch_items=8,
        deadline_chunk_seconds=270,
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

# Routes whose POST endpoint actually works. tts flipped after the three real
# clone generations, health and full regression of the P2 acceptance passed;
# image and video stay false until their own phases.
IMPLEMENTED_ROUTES: frozenset[str] = frozenset({"tts"})


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
