"""local_gateway registry, health aggregation and FastAPI /health tests.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v

Fakes only: temp model trees, scripted client metadata, no network, no real
.env, no real ComfyUI.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import registry  # noqa: E402
from local_gateway.registry import RequiredOption  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_three_first_release_models_with_capabilities(self):
        self.assertEqual({e.key: e.capability for e in registry.MODEL_ENTRIES}, {
            "qwen3-tts-1.7B": "tts",
            "qwen-image-2.1": "image",
            "minimax-h3-fl2va": "video",
        })

    def test_tts_entry_directory_nodes_and_options(self):
        entry = registry.model_by_key("qwen3-tts-1.7B")
        self.assertEqual(entry.required_directories, ("TTS/Qwen3-TTS/",))
        self.assertEqual(entry.required_nodes, (
            "AILab_Qwen3TTSCustomVoice",
            "AILab_Qwen3TTSVoiceClone",
            "AILab_Qwen3TTSVoiceDesign",
            "LoadAudio",
            "SaveAudioAdvanced",
        ))
        for node in ("AILab_Qwen3TTSCustomVoice", "AILab_Qwen3TTSVoiceClone",
                     "AILab_Qwen3TTSVoiceDesign"):
            self.assertIn(RequiredOption(node, "model_size", ("1.7B",)),
                          entry.required_options)
        self.assertIn(RequiredOption("AILab_Qwen3TTSCustomVoice", "speaker", ("Ryan",)),
                      entry.required_options)

    def test_image_entry_files_nodes_and_options(self):
        entry = registry.model_by_key("qwen-image-2.1")
        self.assertEqual(entry.required_files, (
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ))
        self.assertEqual(entry.required_nodes, (
            "UNETLoader", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21",
            "KSampler", "SaveImage", "LoadImage",
        ))
        self.assertIn(RequiredOption(
            "UNETLoader", "unet_name",
            ("qwen_image_2.1_turbo_int8_convrot.safetensors",)), entry.required_options)
        self.assertIn(RequiredOption(
            "CLIPLoader", "clip_name",
            ("qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
             "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors")),
            entry.required_options)
        self.assertIn(RequiredOption(
            "VAELoader", "vae_name", ("qwen_image_2.1_vae_bf16.safetensors",)),
            entry.required_options)
        self.assertIn(RequiredOption("CLIPLoader", "type", ("qwen_image",)),
                      entry.required_options)

    def test_video_entry_keeps_double_extension_and_optional_audio_vae(self):
        entry = registry.model_by_key("minimax-h3-fl2va")
        self.assertIn("vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
                      entry.required_files)
        self.assertEqual(entry.optional_files,
                         ("vae/minimax_h3_audio_vae_fp32.safetensors",))
        self.assertEqual(entry.required_nodes, (
            "UNETLoader", "CLIPLoader", "VAELoader", "EmptyMiniMaxH3LatentAV",
            "MiniMaxH3ImageToVideo", "KSampler", "VAEDecode", "VHS_VideoCombine",
        ))
        self.assertIn(RequiredOption(
            "VHS_VideoCombine", "format", ("video/h264-mp4",)), entry.required_options)
        self.assertIn(RequiredOption("CLIPLoader", "type", ("minimax",)),
                      entry.required_options)

    def test_route_targets_and_empty_implemented_set(self):
        self.assertEqual(registry.ROUTE_TARGETS, {
            "tts": "qwen3-tts-1.7B",
            "image_generate": "qwen-image-2.1",
            "image_edit": "qwen-image-2.1",
            "video": "minimax-h3-fl2va",
        })
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset())

    def test_required_nodes_is_unique_across_entries(self):
        nodes = registry.required_nodes()
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertIn("UNETLoader", nodes)
        self.assertIn("VHS_VideoCombine", nodes)

    def test_model_by_key_unknown_returns_none(self):
        self.assertIsNone(registry.model_by_key("no-such-model"))


if __name__ == "__main__":
    unittest.main()
