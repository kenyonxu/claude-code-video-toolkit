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

import tempfile  # noqa: E402

from local_gateway import config, health  # noqa: E402
from local_gateway.comfyui import ComfyUIError  # noqa: E402
from local_gateway import registry  # noqa: E402
from local_gateway.registry import RequiredOption  # noqa: E402

from unittest import mock  # noqa: E402

from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_three_first_release_models_with_capabilities(self):
        self.assertEqual({e.key: e.capability for e in registry.MODEL_ENTRIES}, {
            "qwen3-tts-1.7B": "tts",
            "qwen-image-2.1": "image",
            "minimax-h3-fl2va": "video",
        })

    def test_tts_entry_is_clone_only(self):
        entry = registry.model_by_key("qwen3-tts-1.7B")
        self.assertEqual(entry.required_directories, ())
        self.assertEqual(entry.required_nonempty_directories, (
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ))
        self.assertEqual(entry.required_nodes, (
            "AILab_Qwen3TTSVoiceClone_Advanced",
            "LoadAudio",
            "SaveAudioAdvanced",
        ))
        self.assertEqual(entry.required_options, (
            RequiredOption("AILab_Qwen3TTSVoiceClone_Advanced", "model_size",
                           ("1.7B",)),
            RequiredOption("SaveAudioAdvanced", "format", ("mp3", "flac")),
        ))
        self.assertEqual(entry.modes, ("clone",))
        self.assertEqual(entry.output_formats, ("mp3", "wav"))
        self.assertEqual(entry.max_batch_items, 8)
        self.assertEqual(entry.deadline_chunk_seconds, 270)

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
        # Task 9 (real acceptance) flips this to frozenset({"tts"}) and
        # updates this assertion in the same commit.
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset())

    def test_required_nodes_is_unique_across_entries(self):
        nodes = registry.required_nodes()
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertIn("UNETLoader", nodes)
        self.assertIn("VHS_VideoCombine", nodes)

    def test_model_by_key_unknown_returns_none(self):
        self.assertIsNone(registry.model_by_key("no-such-model"))


FULL_STATS = {
    "system": {
        "os": "nt",
        "ram_total": 68611555328,
        "ram_free": 34000000000,
        "comfyui_version": "0.45.0",
        "python_version": "3.12",
        "argv": ["python", "main.py", "--listen", "0.0.0.0", "--port", "9188"],
        "exec_path": "C:/python/python.exe",
    },
    "devices": [
        {"name": "NVIDIA GeForce RTX 4090", "type": "cuda",
         "vram_total": 25769803776, "vram_free": 24000000000},
    ],
}


def object_info_fixture(**overrides):
    base = {
        "UNETLoader": {"input": {"required": [
            ["unet_name", ["qwen_image_2.1_turbo_int8_convrot.safetensors",
                           "minimax_h3_fl2va_pruned_int8_convrot.safetensors"]],
            ["weight_dtype", ["default", "fp8_e4m3fn"]],
        ]}},
        "CLIPLoader": {"input": {"required": [
            ["clip_name", ["qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
                           "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
                           "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"]],
            ["type", ["stable_diffusion", "qwen_image", "minimax"]],
        ]}},
        "VAELoader": {"input": {"required": [
            ["vae_name", ["qwen_image_2.1_vae_bf16.safetensors",
                          "minimax_h3_video_vae_fp16.safetensors",
                          "minimax_h3_image_vae_fp16.safetensors.safetensors"]],
        ]}},
        "AILab_Qwen3TTSVoiceClone_Advanced": {"input": {"required": [
            ["model_size", ["0.6B", "1.7B"]],
            ["language", ["Auto", "English", "Chinese"]],
            ["target_text", "STRING"],
            ["reference_text", "STRING"],
            ["reference_audio", "AUDIO"],
        ]}},
        "LoadAudio": {"input": {"required": [["audio", "AUDIO", {"tooltip": "x"}]]}},
        "SaveAudioAdvanced": {"input": {"required": [
            ["format", "COMFY_DYNAMICCOMBO_V3", {"options": [
                {"key": "flac", "inputs": {"required": {}}},
                {"key": "mp3", "inputs": {"required": {}}},
                {"key": "opus", "inputs": {"required": {}}},
            ]}],
        ]}},
        "TextEncodeQwenImage21": {"input": {"required": [
            ["width", "INT"], ["resolution", ["1024", "1280"]],
        ]}},
        "KSampler": {"input": {"required": [["seed", "INT"]]}},
        "SaveImage": {"input": {"required": [["filename_prefix", "STRING"]]}},
        "LoadImage": {"input": {"required": [["image", ["example.png"]]]}},
        "EmptyMiniMaxH3LatentAV": {"input": {"required": [["length", "INT"]]}},
        "MiniMaxH3ImageToVideo": {"input": {"required": [["denoise", "FLOAT"]]}},
        "VAEDecode": {"input": {"required": [["samples", "LATENT"]]}},
        "VHS_VideoCombine": {"input": {"required": [
            ["format", ["video/h264-mp4", "video/webm"]],
        ]}},
    }
    runtime = {}
    for node, meta in base.items():
        required = {}
        for spec in meta["input"]["required"]:
            required[spec[0]] = [spec[1], *spec[2:]]
        runtime[node] = {"input": {"required": required}}
    runtime.update(overrides)
    return runtime


def build_model_tree(root, *, include=("tts", "image", "video"),
                     optional_audio_vae=False, empty_tts_dirs=False):
    if "tts" in include:
        for rel in ("TTS/Qwen3-TTS-12Hz-1.7B-Base",
                    "TTS/Qwen3-TTS-Tokenizer-12Hz"):
            d = root / rel
            d.mkdir(parents=True, exist_ok=True)
            if not empty_tts_dirs:
                (d / "weights.safetensors").write_bytes(b"x")
    if "image" in include:
        for rel in (
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
    if "video" in include:
        for rel in (
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
    if optional_audio_vae:
        audio_vae = root / "vae" / "minimax_h3_audio_vae_fp32.safetensors"
        audio_vae.parent.mkdir(parents=True, exist_ok=True)
        audio_vae.write_bytes(b"x")


class FakeClient:
    """Scripted stand-in for ComfyUIClient: metadata in, no sockets."""

    def __init__(self, object_info=None, stats=None, exc=None):
        self.object_info_data = object_info if object_info is not None else {}
        self.stats = stats if stats is not None else FULL_STATS
        self.exc = exc
        self.object_info_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def system_stats(self):
        if self.exc is not None:
            raise self.exc
        return self.stats

    def object_info(self, node_name=None):
        self.object_info_calls.append(node_name)
        if node_name not in self.object_info_data:
            raise ComfyUIError(f"Invalid node class {node_name!r} requested",
                               code="node_missing")
        return self.object_info_data[node_name]


class HealthAggregationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings_for(self, root, base_url=None):
        env = {"COMFYUI_MODELS_ROOT": str(root)}
        if base_url is not None:
            env["COMFYUI_BASE_URL"] = base_url
        return config.load_settings(env=env)

    def test_full_ready_returns_200_with_whitelisted_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            client = FakeClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["models_ready"])
        self.assertTrue(payload["comfyui"]["reachable"])
        self.assertEqual(payload["comfyui"]["version"], "0.45.0")
        self.assertEqual(payload["comfyui"]["gpu"]["name"], "NVIDIA GeForce RTX 4090")
        self.assertEqual(payload["comfyui"]["gpu"]["vram_total"], 25769803776)
        self.assertEqual(payload["comfyui"]["ram"]["total"], 68611555328)
        self.assertEqual(set(payload["comfyui"]),
                         {"base_url", "reachable", "version", "gpu", "ram"})
        self.assertNotIn("argv", payload["comfyui"])
        self.assertEqual(set(payload["models"]),
                         {"qwen3-tts-1.7B", "qwen-image-2.1", "minimax-h3-fl2va"})
        self.assertTrue(all(m["ready"] for m in payload["models"].values()))
        self.assertEqual(payload["models"]["minimax-h3-fl2va"]["optional_files_present"],
                         ["vae/minimax_h3_audio_vae_fp32.safetensors"])
        self.assertEqual(payload["models"]["qwen-image-2.1"]["optional_files_present"], [])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["routes"], {
            "tts": {"implemented": False, "available": False},
            "image_generate": {"implemented": False, "available": False},
            "image_edit": {"implemented": False, "available": False},
            "video": {"implemented": False, "available": False},
        })

    def test_missing_video_files_report_relative_paths_and_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("tts", "image"))
            client = FakeClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["models_ready"])
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["files_ready"])
        self.assertTrue(video["nodes_ready"])
        self.assertEqual(video["missing_files"], [
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ])
        file_errors = [e for e in payload["errors"] if e["code"] == "model_file_missing"]
        self.assertEqual(len(file_errors), 4)
        self.assertEqual(set(file_errors[0]),
                         {"code", "scope", "message", "action"})
        self.assertIn("minimax-h3-fl2va", file_errors[0]["message"])

    def test_missing_tts_weight_directories_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("image", "video"))
            client = FakeClient(object_info=object_info_fixture())
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["files_ready"])
        self.assertEqual(tts["missing_files"], [
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ])

    def test_empty_tts_weight_directory_is_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, empty_tts_dirs=True)
            client = FakeClient(object_info=object_info_fixture())
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["files_ready"])
        self.assertEqual(tts["missing_files"], [
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ])

    def test_missing_node_and_loader_value_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture()
            meta.pop("VHS_VideoCombine")
            meta["CLIPLoader"] = {"input": {"required": [
                ["clip_name", ["qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
                               "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
                               "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"]],
                ["type", ["stable_diffusion", "qwen_image"]],
            ]}}
            client = FakeClient(object_info=meta)
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["nodes_ready"])
        self.assertEqual(video["missing_nodes"], ["VHS_VideoCombine"])
        self.assertEqual(video["missing_loader_values"], ["CLIPLoader.type=minimax"])
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("node_missing", codes)
        self.assertIn("loader_option_missing", codes)
        # The image model is unaffected: its CLIPLoader.type value still exists.
        self.assertTrue(payload["models"]["qwen-image-2.1"]["ready"])

    def test_tts_model_size_enum_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture(
                AILab_Qwen3TTSVoiceClone_Advanced={"input": {"required": [
                    ["model_size", ["0.6B"]],
                    ["language", ["Auto", "English", "Chinese"]],
                    ["target_text", "STRING"],
                    ["reference_text", "STRING"],
                    ["reference_audio", "AUDIO"],
                ]}},
            )
            client = FakeClient(object_info=meta)
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["nodes_ready"])
        self.assertEqual(tts["missing_loader_values"], [
            "AILab_Qwen3TTSVoiceClone_Advanced.model_size=1.7B",
        ])

    def test_comfyui_unreachable_still_reports_file_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(
                exc=ComfyUIError("connection refused", code="comfyui_unreachable"))
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        self.assertFalse(payload["comfyui"]["reachable"])
        for model in payload["models"].values():
            self.assertTrue(model["files_ready"])
            self.assertFalse(model["nodes_ready"])
            self.assertEqual(model["missing_nodes"], [])
            self.assertEqual(model["missing_loader_values"], [])
        codes = [e["code"] for e in payload["errors"]]
        self.assertEqual(codes, ["comfyui_unreachable"])
        action = payload["errors"][0]["action"]
        self.assertIn("9188", action)
        self.assertIn("COMFYUI_BASE_URL", action)

    def test_comfyui_http_error_maps_to_comfyui_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(exc=ComfyUIError("HTTP 500 from ComfyUI",
                                                 code="comfyui_error"))
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertIn("comfyui_error", [e["code"] for e in payload["errors"]])
        self.assertFalse(payload["comfyui"]["reachable"])

    def test_object_info_http_error_is_unverified_not_a_missing_node(self):
        class BrokenClient(FakeClient):
            def object_info(self, node_name=None):
                if node_name == "VHS_VideoCombine":
                    raise ComfyUIError("HTTP 500 for VHS_VideoCombine",
                                       code="comfyui_error")
                return super().object_info(node_name)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = BrokenClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["nodes_ready"])
        self.assertEqual(video["missing_nodes"], [])
        self.assertEqual(video["missing_loader_values"], [])
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("comfyui_error", codes)
        self.assertNotIn("node_missing", codes)

    def test_missing_models_root_config_leaves_missing_files_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            settings = config.load_settings(env={})
            client = FakeClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(settings,
                                                           lambda url: client)
        self.assertEqual(status, 503)
        for model in payload["models"].values():
            self.assertFalse(model["files_ready"])
            self.assertEqual(model["missing_files"], [])
            self.assertTrue(model["nodes_ready"])  # ComfyUI checks still ran
        self.assertIn("config_missing", [e["code"] for e in payload["errors"]])

    def test_invalid_base_url_skips_the_client_entirely(self):
        def never(url):
            raise AssertionError("client_factory must not run with an invalid base URL")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = health.build_health_payload(
                self.settings_for(root, base_url="http://10.9.9.9:9188"), never)
        self.assertEqual(status, 503)
        self.assertFalse(payload["comfyui"]["reachable"])
        self.assertIn("config_invalid", [e["code"] for e in payload["errors"]])
        for model in payload["models"].values():
            self.assertFalse(model["nodes_ready"])
            self.assertEqual(model["missing_nodes"], [])

    def test_unexpected_exception_is_internal_error_without_traceback(self):
        def boom(url):
            raise ZeroDivisionError("division by zero")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = health.build_health_payload(self.settings_for(root), boom)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(len(payload["errors"]), 1)
        error = payload["errors"][0]
        self.assertEqual(error["code"], "internal_error")
        self.assertIn("ZeroDivisionError", error["message"])
        self.assertNotIn("Traceback", str(payload))
        self.assertEqual(set(payload["models"]),
                         {"qwen3-tts-1.7B", "qwen-image-2.1", "minimax-h3-fl2va"})
        self.assertTrue(all(not r["available"] for r in payload["routes"].values()))

    def test_request_timeout_error_action_does_not_say_start_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(
                exc=ComfyUIError("read timed out", code="comfyui_request_timeout"))
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("comfyui_request_timeout", codes)
        error = next(e for e in payload["errors"]
                     if e["code"] == "comfyui_request_timeout")
        self.assertNotIn("Start ComfyUI", error["action"])
        self.assertIn("timeout", error["message"].lower())

    def test_dependencies_block_reports_both_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            client = FakeClient(object_info=object_info_fixture())
            with mock.patch("shutil.which", return_value="/fake/bin/ffmpeg"):
                payload, status = health.build_health_payload(
                    self.settings_for(root), lambda url: client)
        self.assertEqual(status, 200)
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": True})
        self.assertTrue(payload["ready"])

    def test_missing_ffprobe_is_dependency_missing_and_blocks_ready(self):
        def fake_which(name):
            return "/fake/bin/ffmpeg" if name == "ffmpeg" else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            client = FakeClient(object_info=object_info_fixture())
            with mock.patch("shutil.which", side_effect=fake_which):
                payload, status = health.build_health_payload(
                    self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": False})
        self.assertTrue(payload["models_ready"])  # models describe weights only
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("dependency_missing", codes)
        action = next(e for e in payload["errors"]
                      if e["code"] == "dependency_missing")["action"]
        self.assertIn("ffprobe", action)
        self.assertTrue(all(not r["available"] for r in payload["routes"].values()))

    def test_fallback_skeleton_lists_are_independent_per_model(self):
        def boom(url):
            raise ZeroDivisionError("division by zero")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, _ = health.build_health_payload(self.settings_for(root), boom)
        models = list(payload["models"].values())
        self.assertGreaterEqual(len(models), 2)
        first, second = models[0], models[1]
        self.assertIsNot(first["missing_files"], second["missing_files"])
        first["missing_files"].append("leak")
        self.assertEqual(second["missing_files"], [])
        self.assertIn("dependencies", payload)

    def test_object_info_is_fetched_once_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(object_info=object_info_fixture())
            health.build_health_payload(self.settings_for(root),
                                        lambda url: client)
        self.assertTrue(client.object_info_calls)
        self.assertEqual(len(client.object_info_calls),
                         len(set(client.object_info_calls)))
        self.assertEqual(set(client.object_info_calls),
                         set(registry.required_nodes()))


class ServerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_app(self, root, client):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": str(root)})
        return server.create_app(settings=settings, client_factory=lambda url: client)

    def test_health_200_when_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(object_info=object_info_fixture())
            with TestClient(self.make_app(root, client)) as tc:
                response = tc.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ready"], True)
        self.assertIs(body["models_ready"], True)
        self.assertEqual(set(body["routes"]),
                         {"tts", "image_generate", "image_edit", "video"})
        self.assertTrue(all(not r["implemented"] and not r["available"]
                            for r in body["routes"].values()))
        self.assertEqual(body["errors"], [])

    def test_health_503_with_structured_errors_when_model_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("tts", "image"))
            client = FakeClient(object_info=object_info_fixture())
            with TestClient(self.make_app(root, client)) as tc:
                response = tc.get("/health")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertIs(body["ready"], False)
        self.assertNotEqual(body["errors"], [])
        for error in body["errors"]:
            self.assertEqual(set(error), {"code", "scope", "message", "action"})

    def test_creating_the_app_makes_no_network_request_and_does_not_read_real_env(self):
        def never(url):
            raise AssertionError("create_app must not contact ComfyUI")

        with mock.patch.object(server, "load_settings") as load:
            app = server.create_app(client_factory=never)
        load.assert_not_called()
        self.assertTrue(app.title)

    def test_default_client_factory_builds_a_real_client(self):
        client = server.default_client_factory("http://127.0.0.1:9188")
        try:
            self.assertEqual(client.base_url, "http://127.0.0.1:9188")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
