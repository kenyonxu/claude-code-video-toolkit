"""local_gateway registry, health aggregation and FastAPI /health tests.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v

Fakes only: temp model trees, scripted client metadata, no network, no real
.env, no real ComfyUI, no real worker.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import config, health  # noqa: E402
from local_gateway.comfyui import ComfyUIError  # noqa: E402
from local_gateway import registry  # noqa: E402
from local_gateway.registry import RequiredOption  # noqa: E402
from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_three_first_release_models_with_capabilities(self):
        self.assertEqual({e.key: e.capability for e in registry.MODEL_ENTRIES}, {
            "confucius4-tts": "tts",
            "qwen-image-2.1": "image",
            "minimax-h3-fl2va": "video",
        })

    def test_tts_entry_is_confucius4_clone_only(self):
        entry = registry.model_by_key("confucius4-tts")
        self.assertEqual(entry.required_directories, ())
        self.assertEqual(entry.required_nonempty_directories, ())
        self.assertEqual(entry.required_nodes, ())
        self.assertEqual(entry.required_options, ())
        self.assertEqual(entry.modes, ("clone",))
        self.assertEqual(entry.output_formats, ("mp3", "wav"))
        self.assertEqual(entry.languages, ("zh", "en", "ja", "ko", "de", "fr",
                                           "es", "id", "it", "ms", "pt", "ru",
                                           "th", "vi"))
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
        self.assertIn(RequiredOption(
            "UNETLoader", "unet_name",
            ("qwen_image_2.1_turbo_int8_convrot.safetensors",)), entry.required_options)

    def test_video_entry_keeps_double_extension_and_optional_audio_vae(self):
        entry = registry.model_by_key("minimax-h3-fl2va")
        self.assertIn("vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
                      entry.required_files)
        self.assertEqual(entry.optional_files,
                         ("vae/minimax_h3_audio_vae_fp32.safetensors",))

    def test_route_targets_and_empty_implemented_set(self):
        self.assertEqual(registry.ROUTE_TARGETS, {
            "tts": "confucius4-tts",
            "image_generate": "qwen-image-2.1",
            "image_edit": "qwen-image-2.1",
            "video": "minimax-h3-fl2va",
        })
        # Task 8 (real acceptance) flips this to frozenset({"tts"}) and
        # updates this assertion in the same commit.
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset())

    def test_required_nodes_is_unique_and_has_no_tts_nodes(self):
        nodes = registry.required_nodes()
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertIn("UNETLoader", nodes)
        self.assertNotIn("AILab_Qwen3TTSVoiceClone_Advanced", nodes)

    def test_model_by_key_unknown_returns_none(self):
        self.assertIsNone(registry.model_by_key("no-such-model"))


FULL_STATS = {
    "system": {
        "os": "nt",
        "ram_total": 68611555328,
        "ram_free": 34000000000,
        "comfyui_version": "0.45.0",
        "python_version": "3.12",
        "argv": ["python", "main.py"],
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


def build_model_tree(root, *, include=("image", "video"),
                     optional_audio_vae=False):
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


class FakeTTSClient:
    """Scripted stand-in for ConfuciusClient."""

    def __init__(self, health_payload=None, exc=None):
        self.health_payload = (health_payload if health_payload is not None
                               else {"status": "ok", "sample_rate": 22050})
        self.exc = exc

    def health(self):
        if self.exc is not None:
            raise self.exc
        return self.health_payload


class HealthAggregationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings_for(self, root, base_url=None, worker_url=None):
        env = {"COMFYUI_MODELS_ROOT": str(root)}
        if base_url is not None:
            env["COMFYUI_BASE_URL"] = base_url
        if worker_url is not None:
            env["CONFUCIUS4_TTS_BASE_URL"] = worker_url
        return config.load_settings(env=env)

    def payload_for(self, root, client, tts=None, base_url=None):
        tts_client = tts if tts is not None else FakeTTSClient()
        return health.build_health_payload(
            self.settings_for(root, base_url=base_url), lambda url: client,
            lambda url: tts_client)

    def test_full_ready_with_comfyui_down_returns_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            payload, status = self.payload_for(
                root, FakeClient(exc=ComfyUIError("refused",
                                                  code="comfyui_unreachable")))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["models_ready"])
        self.assertFalse(payload["comfyui"]["reachable"])
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": True})
        self.assertEqual(payload["tts_backend"], {
            "base_url": "http://127.0.0.1:8765",
            "status": "ok",
            "sample_rate": 22050,
        })
        self.assertEqual(payload["routes"], {
            "tts": {"implemented": False, "available": False},
            "image_generate": {"implemented": False, "available": False},
            "image_edit": {"implemented": False, "available": False},
            "video": {"implemented": False, "available": False},
        })

    def test_with_comfyui_up_stats_are_reported_and_models_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(status, 200)
        self.assertTrue(payload["comfyui"]["reachable"])
        self.assertEqual(payload["comfyui"]["version"], "0.45.0")
        self.assertEqual(payload["comfyui"]["gpu"]["name"],
                         "NVIDIA GeForce RTX 4090")
        self.assertEqual(set(payload["models"]),
                         {"confucius4-tts", "qwen-image-2.1",
                          "minimax-h3-fl2va"})
        self.assertTrue(all(m["ready"] for m in payload["models"].values()))
        self.assertEqual(payload["errors"], [])

    def test_worker_starting_blocks_ready_with_unavailable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()),
                tts=FakeTTSClient({"status": "starting", "sample_rate": None}))
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["tts_backend"]["status"], "starting")
        self.assertFalse(payload["models"]["confucius4-tts"]["ready"])
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("tts_backend_unavailable", codes)

    def test_worker_wrong_sample_rate_blocks_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()),
                tts=FakeTTSClient({"status": "ok", "sample_rate": 24000}))
        self.assertEqual(status, 503)
        self.assertFalse(payload["models"]["confucius4-tts"]["ready"])
        self.assertIn("tts_backend_unavailable",
                      [e["code"] for e in payload["errors"]])

    def test_worker_unreachable_maps_to_unavailable(self):
        from local_gateway.confucius_client import ConfuciusError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()),
                tts=FakeTTSClient(exc=ConfuciusError(
                    "Cannot reach the Confucius worker: refused",
                    code="tts_backend_unavailable")))
        self.assertEqual(status, 503)
        self.assertEqual(payload["tts_backend"]["status"], "unreachable")
        self.assertFalse(payload["models_ready"])

    def test_worker_base_url_comes_from_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            env = {"COMFYUI_MODELS_ROOT": str(root),
                   "CONFUCIUS4_TTS_BASE_URL": "http://127.0.0.1:9999"}
            settings = config.load_settings(env=env)
            payload, _ = health.build_health_payload(
                settings, lambda url: FakeClient(
                    object_info=object_info_fixture()),
                lambda url: FakeTTSClient())
        self.assertEqual(payload["tts_backend"]["base_url"],
                         "http://127.0.0.1:9999")

    def test_invalid_worker_url_is_never_dialed(self):
        def never(url):
            raise AssertionError(
                "an invalid CONFUCIUS4_TTS_BASE_URL must not be dialed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            payload, status = health.build_health_payload(
                self.settings_for(root, worker_url="http://10.0.0.5:8765"),
                lambda url: FakeClient(object_info=object_info_fixture()),
                never)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["models_ready"])
        self.assertFalse(payload["models"]["confucius4-tts"]["ready"])
        self.assertEqual(payload["tts_backend"], {
            "base_url": "http://10.0.0.5:8765",
            "status": "unreachable",
            "sample_rate": None,
        })
        self.assertIn("config_invalid",
                      [e["code"] for e in payload["errors"]])

    def test_missing_video_files_report_relative_paths_and_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("image",))
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(status, 503)
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["files_ready"])
        self.assertEqual(video["missing_files"], [
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ])

    def test_image_video_node_gaps_do_not_block_ready(self):
        # Missing image/video nodes are handled by their own route preflight
        # (P3/P4); health only reports them.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture()
            meta.pop("VHS_VideoCombine")
            payload, status = self.payload_for(
                root, FakeClient(object_info=meta))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["nodes_ready"])
        self.assertEqual(video["missing_nodes"], ["VHS_VideoCombine"])
        self.assertIn("node_missing", [e["code"] for e in payload["errors"]])

    def test_missing_ffprobe_blocks_ready(self):
        def fake_which(name):
            return "/fake/bin/ffmpeg" if name == "ffmpeg" else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            with mock.patch("shutil.which", side_effect=fake_which):
                payload, status = self.payload_for(
                    root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": False})
        self.assertTrue(all(not r["available"] for r in payload["routes"].values()))

    def test_missing_models_root_config_blocks_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            settings = config.load_settings(env={})
            payload, status = health.build_health_payload(
                settings, lambda url: FakeClient(object_info=object_info_fixture()),
                lambda url: FakeTTSClient())
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        # Legacy COMFYUI_MODELS_ROOT config still gates the aggregate P0-P4
        # health contract, but it must never make the Confucius model unready.
        self.assertTrue(payload["models"]["confucius4-tts"]["ready"])
        self.assertIn("config_missing",
                      [e["code"] for e in payload["errors"]])

    def test_unexpected_exception_is_internal_error_without_traceback(self):
        def boom(url):
            raise ZeroDivisionError("division by zero")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            # Direct call (not payload_for): payload_for would wrap boom as
            # `lambda url: boom`, so the `with client_factory(...) as client`
            # line would fail on the function object with a TypeError before
            # boom is ever invoked. Passed as the factory itself, boom(url)
            # raises ZeroDivisionError inside _build.
            payload, status = health.build_health_payload(
                self.settings_for(root), boom,
                lambda url: FakeTTSClient())
        self.assertEqual(status, 503)
        error = payload["errors"][0]
        self.assertEqual(error["code"], "internal_error")
        self.assertIn("ZeroDivisionError", error["message"])
        self.assertNotIn("Traceback", str(payload))
        self.assertIn("tts_backend", payload)

    def test_object_info_is_fetched_once_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(object_info=object_info_fixture())
            self.payload_for(root, client)
        self.assertEqual(len(client.object_info_calls),
                         len(set(client.object_info_calls)))
        self.assertEqual(set(client.object_info_calls),
                         set(registry.required_nodes()))


class ServerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_app(self, root, client, tts=None):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": str(root)})
        return server.create_app(
            settings=settings, client_factory=lambda url: client,
            tts_client_factory=lambda url: (tts if tts is not None
                                            else FakeTTSClient()))

    def test_health_200_when_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            with TestClient(self.make_app(root, FakeClient(
                    object_info=object_info_fixture()))) as tc:
                response = tc.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ready"], True)
        self.assertEqual(body["tts_backend"]["status"], "ok")
        self.assertEqual(set(body["routes"]),
                         {"tts", "image_generate", "image_edit", "video"})

    def test_creating_the_app_makes_no_network_request_and_does_not_read_real_env(self):
        def never(url):
            raise AssertionError("create_app must not contact any backend")

        with mock.patch.object(server, "load_settings") as load:
            app = server.create_app(client_factory=never,
                                    tts_client_factory=never)
        load.assert_not_called()
        self.assertTrue(app.title)

    def test_default_client_factories_build_real_clients(self):
        comfy = server.default_client_factory("http://127.0.0.1:9188")
        try:
            self.assertEqual(comfy.base_url, "http://127.0.0.1:9188")
        finally:
            comfy.close()
        tts_client = server.default_tts_client_factory(
            "http://127.0.0.1:8765")
        self.assertEqual(tts_client.base_url, "http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
