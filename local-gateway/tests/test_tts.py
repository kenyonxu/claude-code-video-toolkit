"""tts: request validation, preflight, orchestration and the FastAPI route.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v

ComfyUI clients, PATH tools and model trees are faked/temp. ffmpeg/ffprobe
run for real only inside audio.finalize_output on tiny generated clips.
"""
from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import audio, config, registry, tts, tts_workflow  # noqa: E402
from local_gateway.comfyui import ComfyUIError, ComfyUITimeout  # noqa: E402
from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

LANGUAGES = ["Auto", "English", "Chinese"]
REF_BYTES = b"ID3fake-audio-bytes"


def clone_object_info_fixture(**overrides):
    base = {
        "AILab_Qwen3TTSVoiceClone_Advanced": {"input": {"required": {
            "model_size": [["0.6B", "1.7B"], {}],
            "language": [list(LANGUAGES), {}],
            "target_text": ["STRING", {}],
            "reference_text": ["STRING", {}],
            "reference_audio": ["AUDIO", {}],
        }}},
        "LoadAudio": {"input": {"required": {"audio": ["AUDIO", {}]}}},
        "SaveAudioAdvanced": {"input": {"required": {
            "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                {"key": "flac", "inputs": {"required": {}}},
                {"key": "mp3", "inputs": {"required": {}}},
                {"key": "opus", "inputs": {"required": {}}},
            ]}]}}},
    }
    base.update(overrides)
    return base


def success_entry(filename="clone_00001_.mp3"):
    return {"status": {"completed": True, "status_str": "success"},
            "outputs": {tts_workflow.SAVE_NODE: {"audio": [
                {"filename": filename, "subfolder": "ccvt",
                 "type": "output"}]}}}


class FakeTTSClient:
    def __init__(self, *, object_info=None, upload_response=None, entries=None,
                 view_bytes=b"", wait_exc=None):
        self.object_info_data = (object_info if object_info is not None
                                 else clone_object_info_fixture())
        self.upload_response = (upload_response if upload_response is not None
                                else {"name": "ccvt_clone_ref_ab12.mp3",
                                      "subfolder": "", "type": "input"})
        self.entries = entries if entries is not None else {}
        self.view_bytes = view_bytes
        self.wait_exc = wait_exc
        self.upload_calls = []
        self.submit_calls = []
        self.view_calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        self.closed = True

    def object_info(self, node_name=None):
        if node_name not in self.object_info_data:
            raise ComfyUIError(
                f"Invalid node class {node_name!r} requested",
                code="node_missing")
        return self.object_info_data[node_name]

    def upload(self, filename, content, content_type="application/octet-stream"):
        self.upload_calls.append((filename, content, content_type))
        return self.upload_response

    def submit(self, prompt):
        self.submit_calls.append(prompt)
        return f"prompt-{len(self.submit_calls)}"

    def wait(self, prompt_id, deadline, poll_interval=1.0):
        if self.wait_exc is not None:
            raise self.wait_exc
        return self.entries[prompt_id]

    def view(self, filename, subfolder="", file_type="output",
             max_bytes=audio.VIEW_MAX_BYTES):
        self.view_calls.append((filename, subfolder, file_type, max_bytes))
        return self.view_bytes


class Mp3Mixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "clip.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.3", "-c:a", "libmp3lame",
             str(out)],
            check=True, capture_output=True, shell=False)
        cls.mp3_bytes = out.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class GatewayFixture(Mp3Mixin):
    def make_settings(self, tmp):
        root = Path(tmp)
        for rel in registry.model_by_key(
                "qwen3-tts-1.7B").required_nonempty_directories:
            d = root / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / "weights.safetensors").write_bytes(b"x")
        return config.load_settings(env={"COMFYUI_MODELS_ROOT": str(root)})


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def valid_body(**overrides):
    body = {"text": "你好，世界", "ref_audio_base64": b64(REF_BYTES),
            "ref_text": "参考音频逐字稿"}
    body.update(overrides)
    return body


class DeadlineFormulaTests(unittest.TestCase):
    def test_matches_the_spec_formula(self):
        for count, expected in ((1, 270), (3, 540), (4, 540), (8, 810)):
            self.assertEqual(tts.deadline_seconds(count), expected, count)


class ErrorCodeMappingTests(unittest.TestCase):
    def test_spec_section_9_statuses(self):
        expected = {
            "mode_unsupported": 422, "validation_error": 422,
            "unknown_model": 422, "external_url_unsupported": 422,
            "dependency_missing": 503, "comfyui_unreachable": 503,
            "comfyui_request_timeout": 504, "comfyui_timeout": 504,
            "comfyui_error": 502, "generation_failed": 500,
        }
        self.assertEqual(tts.ERROR_STATUS, expected)
        for code, status in expected.items():
            self.assertEqual(
                tts.TTSRouteError("m", code).http_status, status, code)


class ParseRequestTests(unittest.TestCase):
    def parse(self, body):
        return tts.parse_request(body)

    def test_minimal_request_gets_defaults(self):
        request = self.parse(valid_body())
        self.assertEqual(request.texts, ("你好，世界",))
        self.assertEqual(request.languages, ("Auto",))
        self.assertEqual(request.output_format, "mp3")
        self.assertEqual(request.model, "qwen3-tts-1.7B")
        self.assertEqual(request.ref_audio_bytes, REF_BYTES)
        self.assertIsNone(request.temperature)
        self.assertIsNone(request.top_p)

    def test_explicit_null_sampling_is_treated_as_absent(self):
        request = self.parse(valid_body(temperature=None, top_p=None))
        self.assertIsNone(request.temperature)
        self.assertIsNone(request.top_p)

    def test_unknown_field_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(speaker="Ryan"))
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertIn("speaker", ctx.exception.message)

    def test_custom_voice_key_is_mode_unsupported(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(custom_voice={"speaker": "Ryan"}))
        self.assertEqual(ctx.exception.code, "mode_unsupported")

    def test_voice_design_key_is_mode_unsupported(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(voice_design={"instruct": "warm"}))
        self.assertEqual(ctx.exception.code, "mode_unsupported")

    def test_non_clone_mode_is_mode_unsupported(self):
        for mode in ("custom_voice", "voice_design", "Clone", ""):
            with self.subTest(mode=mode):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(mode=mode))
                self.assertEqual(ctx.exception.code, "mode_unsupported")

    def test_external_fields_are_rejected(self):
        for field, value in (("ref_audio_url", "https://r2/x.mp3"),
                             ("r2", {"bucket": "b"})):
            with self.subTest(field=field):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(**{field: value}))
                self.assertEqual(ctx.exception.code,
                                 "external_url_unsupported")

    def test_unknown_model_is_unknown_model(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(model="qwen3-tts-0.6B"))
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_non_tts_model_is_rejected_not_treated_as_clone_model(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(model="qwen-image-2.1"))
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_text_batch_limits(self):
        nine = [f"t{i}" for i in range(9)]
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=nine))
        self.assertEqual(ctx.exception.code, "validation_error")
        eight = [f"t{i}" for i in range(8)]
        request = self.parse(valid_body(text=eight))
        self.assertEqual(len(request.texts), 8)

    def test_empty_text_items_rejected(self):
        for bad in ("", "   ", ["ok", ""]):
            with self.subTest(bad=bad):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(text=bad))
                self.assertEqual(ctx.exception.code, "validation_error")

    def test_language_array_must_match_text_length(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=["a", "b"], language=["Auto"]))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_language_scalar_broadcasts_over_batch(self):
        request = self.parse(valid_body(text=["a", "b"], language="Chinese"))
        self.assertEqual(request.languages, ("Chinese", "Chinese"))

    def test_bad_output_format_rejected(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(output_format="opus"))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_sampling_ranges_and_types(self):
        cases = [
            {"temperature": 0.05}, {"temperature": 2.5},
            {"temperature": True}, {"temperature": float("nan")},
            {"top_p": -0.1}, {"top_p": 1.5}, {"top_p": "0.5"},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(**case))
                self.assertEqual(ctx.exception.code, "validation_error")

    def test_data_url_prefix_decodes_and_sets_suffix(self):
        body = valid_body(ref_audio_base64="data:audio/wav;base64," + b64(b"RIFF"))
        request = self.parse(body)
        self.assertEqual(request.ref_audio_bytes, b"RIFF")
        self.assertEqual(request.ref_suffix, ".wav")

    def test_invalid_base64_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(ref_audio_base64="not valid base64!"))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_ref_audio_over_limit_is_validation_error(self):
        with mock.patch.object(tts, "MAX_REF_AUDIO_BYTES", 8):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.parse(valid_body(ref_audio_base64=b64(b"0123456789")))
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(tts.MAX_REF_AUDIO_BYTES, 16 * 1024 * 1024)

    def test_missing_ref_text_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(ref_text=""))
        self.assertEqual(ctx.exception.code, "validation_error")


class PreflightTests(GatewayFixture, unittest.TestCase):
    def test_missing_ffmpeg_is_dependency_missing_before_any_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)

            def never(url):
                raise AssertionError("preflight must fail before any client")

            with mock.patch("shutil.which",
                            side_effect=lambda name: None):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, never)
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_empty_weights_directory_is_dependency_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            for rel in registry.model_by_key(
                    "qwen3-tts-1.7B").required_nonempty_directories:
                for child in (Path(tmp) / rel).iterdir():
                    child.unlink()
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: FakeTTSClient())
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_missing_clone_node_is_dependency_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: FakeTTSClient(
                        object_info={}))
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_object_info_http_error_maps_to_its_comfyui_code(self):
        class Broken(FakeTTSClient):
            def object_info(self, node_name=None):
                raise ComfyUIError("HTTP 500", code="comfyui_error")

        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: Broken())
        self.assertEqual(ctx.exception.code, "comfyui_error")
        self.assertEqual(ctx.exception.http_status, 502)

    def test_save_format_options_are_checked(self):
        meta = clone_object_info_fixture(
            SaveAudioAdvanced={"input": {"required": {
                "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                    {"key": "flac", "inputs": {"required": {}}},
                    {"key": "opus", "inputs": {"required": {}}},
                ]}]}}})
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: FakeTTSClient(
                        object_info=meta))
        self.assertEqual(ctx.exception.code, "dependency_missing")
        self.assertIn("mp3", ctx.exception.message)

    def test_language_choices_come_from_the_runtime_enum(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                result = tts.preflight(settings, lambda url: FakeTTSClient())
        self.assertEqual(result.language_choices, tuple(LANGUAGES))


class GenerateCloneTests(GatewayFixture, unittest.TestCase):
    def client_for(self, **kwargs):
        kwargs.setdefault("view_bytes", self.mp3_bytes)
        entries = kwargs.pop("entries", None)
        if entries is None:
            entries = {f"prompt-{i+1}": success_entry(f"clone_{i+1:05d}_.mp3")
                       for i in range(8)}
        return FakeTTSClient(entries=entries, **kwargs)

    def run_clone(self, client, body, settings_tmp, lock=None):
        settings = self.make_settings(settings_tmp)
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            return tts.generate_clone(settings, lambda url: client, body,
                                      lock or threading.Lock())

    def test_single_text_success_shape(self):
        client = self.client_for()
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.run_clone(client, valid_body(), tmp)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["model"], "qwen3-tts-1.7B")
        self.assertGreaterEqual(payload["processing_time_seconds"], 0)
        self.assertEqual(len(payload["outputs"]), 1)
        item = payload["outputs"][0]
        self.assertEqual(item["audio_base64"], b64(self.mp3_bytes))
        self.assertGreater(item["duration_seconds"], 0)
        # single-text mirror for existing clients
        self.assertEqual(payload["audio_base64"], item["audio_base64"])
        self.assertEqual(payload["duration_seconds"],
                         item["duration_seconds"])
        self.assertNotIn("audio_url", payload)
        self.assertNotIn("r2_key", payload)

    def test_batch_uploads_once_and_keeps_order(self):
        client = self.client_for()
        body = valid_body(text=["第一句", "第二句", "第三句"],
                          language=["Chinese", "Auto", "English"])
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.run_clone(client, body, tmp)
        self.assertEqual(len(client.upload_calls), 1)
        self.assertEqual(len(client.submit_calls), 3)
        self.assertEqual(len(client.view_calls), 3)
        self.assertEqual(
            [prompt[tts_workflow.TTS_NODE]["inputs"]["target_text"]
             for prompt in client.submit_calls],
            ["第一句", "第二句", "第三句"])
        self.assertEqual(
            [prompt[tts_workflow.TTS_NODE]["inputs"]["language"]
             for prompt in client.submit_calls],
            ["Chinese", "Auto", "English"])
        prefixes = [prompt[tts_workflow.SAVE_NODE]["inputs"]["filename_prefix"]
                    for prompt in client.submit_calls]
        self.assertEqual(len(set(prefixes)), 3)
        self.assertTrue(all(p.startswith("ccvt/tts/") for p in prefixes))
        self.assertEqual(len(payload["outputs"]), 3)
        self.assertNotIn("audio_base64", payload)

    def test_upload_join_uses_subfolder_when_present(self):
        client = self.client_for(
            upload_response={"name": "ref_9.mp3", "subfolder": "ccvt",
                             "type": "input"})
        with tempfile.TemporaryDirectory() as tmp:
            self.run_clone(client, valid_body(), tmp)
        prompt = client.submit_calls[0]
        self.assertEqual(prompt["1"]["inputs"]["audio"], "ccvt/ref_9.mp3")

    def test_upload_without_name_is_generation_failed(self):
        client = self.client_for(upload_response={"type": "input"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(), tmp)
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_language_outside_runtime_enum_is_validation_error(self):
        client = self.client_for()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(language="Klingon"), tmp)
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(client.submit_calls, [])

    def test_item_failure_fails_the_whole_request_without_partials(self):
        client = self.client_for(wait_exc=ComfyUIError(
            "execution blew up", code="execution_error",
            details={"status_str": "error"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(text=["a", "b"]), tmp)
        self.assertEqual(ctx.exception.code, "generation_failed")
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertEqual(client.view_calls, [])

    def test_wait_timeout_maps_to_504(self):
        client = self.client_for(
            wait_exc=ComfyUITimeout("prompt-1", time.monotonic() + 1))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(), tmp)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")
        self.assertEqual(ctx.exception.http_status, 504)

    def test_lock_is_released_after_success_and_failure(self):
        lock = threading.Lock()
        client = self.client_for()
        with tempfile.TemporaryDirectory() as tmp:
            self.run_clone(client, valid_body(), tmp, lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

        lock = threading.Lock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError):
                self.run_clone(self.client_for(wait_exc=ComfyUIError(
                    "boom", code="execution_error")), valid_body(), tmp,
                    lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_deadline_and_processing_time_start_before_the_lock(self):
        lock = threading.Lock()
        lock.acquire()
        done = threading.Event()
        result = {}

        def worker():
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    result["payload"] = self.run_clone(
                        self.client_for(), valid_body(), tmp, lock=lock)
                finally:
                    done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.5)
        lock.release()
        self.assertTrue(done.wait(30))
        thread.join(30)
        self.assertGreaterEqual(result["payload"]["processing_time_seconds"],
                                0.5)

    def test_lock_wait_timeout_is_comfyui_timeout_without_submit(self):
        lock = threading.Lock()
        lock.acquire()
        client = self.client_for()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(tts, "deadline_seconds", return_value=0):
                    with self.assertRaises(tts.TTSRouteError) as ctx:
                        self.run_clone(client, valid_body(), tmp, lock=lock)
            self.assertEqual(ctx.exception.code, "comfyui_timeout")
            self.assertEqual(client.submit_calls, [])
        finally:
            lock.release()

    def test_validation_error_raises_before_the_lock(self):
        # _thread.lock attributes are read-only, so the builtin lock cannot
        # be patched; a duck-typed lock records acquire calls instead.
        class TrackingLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.acquire_calls = []

            def acquire(self, blocking=True, timeout=-1):
                self.acquire_calls.append((blocking, timeout))
                return self._lock.acquire(blocking, timeout)

            def release(self):
                self._lock.release()

        lock = TrackingLock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(self.client_for(), valid_body(mode="x"), tmp,
                               lock=lock)
        self.assertEqual(ctx.exception.code, "mode_unsupported")
        self.assertEqual(lock.acquire_calls, [])


class RouteTests(GatewayFixture, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The settings must outlive the requests: preflight walks this tree.
        self._settings = self.make_settings(self._tmp.name)

    def make_client_app(self, client):
        app = server.create_app(settings=self._settings,
                                client_factory=lambda url: client)
        return TestClient(app)

    def test_success_returns_200(self):
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(
                    FakeTTSClient(view_bytes=self.mp3_bytes,
                                  entries={"prompt-1": success_entry()})) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["outputs"][0]["duration_seconds"] > 0, True)

    def test_error_codes_map_to_spec_statuses(self):
        cases = [
            (valid_body(mode="custom_voice"), FakeTTSClient(), 422,
             "mode_unsupported"),
            (valid_body(unknown=1), FakeTTSClient(), 422, "validation_error"),
            (valid_body(model="nope"), FakeTTSClient(), 422, "unknown_model"),
            (valid_body(ref_audio_url="https://x"), FakeTTSClient(), 422,
             "external_url_unsupported"),
            (valid_body(), FakeTTSClient(object_info={}), 503,
             "dependency_missing"),
            (valid_body(), FakeTTSClient(
                wait_exc=ComfyUITimeout("p", time.monotonic() + 1)), 504,
             "comfyui_timeout"),
            (valid_body(), FakeTTSClient(
                wait_exc=ComfyUIError("exec", code="execution_error")), 500,
             "generation_failed"),
        ]
        for body, client, status, code in cases:
            with self.subTest(code=code):
                with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                    with self.make_client_app(client) as tc:
                        response = tc.post("/tts/generate", json=body)
                self.assertEqual(response.status_code, status)
                error = response.json()
                self.assertEqual(error["code"], code)
                self.assertFalse(error["success"])
                self.assertTrue(error["error"])

    def test_comfyui_unreachable_via_object_info_is_503(self):
        class Broken(FakeTTSClient):
            def object_info(self, node_name=None):
                raise ComfyUIError("refused", code="comfyui_unreachable")

        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(Broken()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "comfyui_unreachable")

    def test_comfyui_request_timeout_is_504(self):
        class Slow(FakeTTSClient):
            def object_info(self, node_name=None):
                raise ComfyUIError("read timed out",
                                   code="comfyui_request_timeout")

        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(Slow()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["code"], "comfyui_request_timeout")

    def test_unexpected_exception_is_generic_generation_failed(self):
        with mock.patch.object(tts, "generate_clone",
                               side_effect=RuntimeError("secret internals")):
            with self.make_client_app(FakeTTSClient()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "generation_failed")
        self.assertNotIn("secret internals", body["error"])


if __name__ == "__main__":
    unittest.main()
