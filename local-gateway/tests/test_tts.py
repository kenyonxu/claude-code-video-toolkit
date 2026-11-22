"""tts: Confucius request validation, preflight, orchestration, route.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v

Worker clients and PATH tools are faked/temp. ffmpeg/ffprobe run for real
only inside audio.finalize_output on tiny generated clips.
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

from local_gateway import audio, config, registry, tts  # noqa: E402
from local_gateway.confucius_client import ConfuciusError  # noqa: E402
from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

REF_BYTES = b"ID3fake-audio-bytes"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def valid_body(**overrides):
    body = {"text": "你好，世界", "ref_audio_base64": b64(REF_BYTES)}
    body.update(overrides)
    return body


class WavMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "clip.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.3", "-ar", "22050", "-ac", "1",
             "-c:a", "pcm_s16le", str(out)],
            check=True, capture_output=True, shell=False)
        cls.wav_bytes = out.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class FakeConfuciusClient:
    def __init__(self, *, health_payload=None, wav_bytes=None,
                 generate_exc=None):
        self.health_payload = (health_payload if health_payload is not None
                               else {"status": "ok", "sample_rate": 22050})
        self.wav_bytes = wav_bytes
        self.generate_exc = generate_exc
        self.generate_calls = []

    def health(self):
        if isinstance(self.health_payload, Exception):
            raise self.health_payload
        return self.health_payload

    def generate(self, *, text, lang, reference_bytes, filename,
                 content_type, deadline):
        self.generate_calls.append({
            "text": text, "lang": lang,
            "reference_bytes": reference_bytes,
            "filename": filename, "content_type": content_type,
            "deadline": deadline})
        if self.generate_exc is not None:
            raise self.generate_exc
        return self.wav_bytes


class DeadlineFormulaTests(unittest.TestCase):
    def test_matches_the_spec_formula(self):
        for count, expected in ((1, 270), (3, 540), (4, 540), (8, 810)):
            self.assertEqual(tts.deadline_seconds(count), expected, count)


class ErrorCodeMappingTests(unittest.TestCase):
    def test_spec_section_7_statuses(self):
        expected = {
            "mode_unsupported": 422, "validation_error": 422,
            "unknown_model": 422, "external_url_unsupported": 422,
            "dependency_missing": 503, "tts_backend_unavailable": 503,
            "tts_backend_timeout": 504, "tts_backend_error": 502,
            "generation_failed": 500,
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
        self.assertEqual(request.languages, ("zh",))
        self.assertEqual(request.output_format, "mp3")
        self.assertEqual(request.model, "confucius4-tts")
        self.assertEqual(request.ref_audio_bytes, REF_BYTES)
        self.assertEqual(request.ref_suffix, ".mp3")

    def test_mode_fields_are_mode_unsupported(self):
        for field, value in (
                ("ref_text", "逐字稿"), ("speaker", "Ryan"),
                ("instruct", "warm"), ("temperature", 0.7), ("top_p", 0.9),
                ("custom_voice", {"speaker": "Ryan"}),
                ("voice_design", {"instruct": "warm"})):
            with self.subTest(field=field):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(**{field: value}))
                self.assertEqual(ctx.exception.code, "mode_unsupported")
                self.assertIn(field, ctx.exception.message)

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

    def test_unknown_field_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(speed=1.5))
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertIn("speed", ctx.exception.message)

    def test_unknown_model_is_unknown_model(self):
        for model in ("qwen3-tts-1.7B", "confucius4-tts-xl"):
            with self.subTest(model=model):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(model=model))
                self.assertEqual(ctx.exception.code, "unknown_model")

    def test_non_tts_model_is_rejected(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(model="qwen-image-2.1"))
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_text_batch_and_char_limits(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=[f"t{i}" for i in range(9)]))
        self.assertEqual(ctx.exception.code, "validation_error")
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text="好" * 1025))
        self.assertEqual(ctx.exception.code, "validation_error")
        request = self.parse(valid_body(text=["好" * 1024, "ok"]))
        self.assertEqual(len(request.texts), 2)

    def test_empty_text_items_rejected(self):
        for bad in ("", "   ", ["ok", ""]):
            with self.subTest(bad=bad):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(text=bad))
                self.assertEqual(ctx.exception.code, "validation_error")

    def test_language_matrix(self):
        self.assertEqual(self.parse(valid_body()).languages, ("zh",))
        request = self.parse(valid_body(text=["a", "b"], language="en"))
        self.assertEqual(request.languages, ("en", "en"))
        request = self.parse(valid_body(text=["a", "b"], language=["ja", "ko"]))
        self.assertEqual(request.languages, ("ja", "ko"))
        for bad in ("Auto", "ZH", "klingon"):
            with self.subTest(bad=bad):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(language=bad))
                self.assertEqual(ctx.exception.code, "validation_error")
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=["a", "b"], language=["zh"]))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_output_format(self):
        request = self.parse(valid_body(output_format="wav"))
        self.assertEqual(request.output_format, "wav")
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(output_format="flac"))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_ref_audio_base64_rules(self):
        request = self.parse(valid_body(
            ref_audio_base64="data:audio/wav;base64," + b64(b"RIFF")))
        self.assertEqual(request.ref_audio_bytes, b"RIFF")
        self.assertEqual(request.ref_suffix, ".wav")
        for bad, why in (
                ("data:audio/mpeg,AAAA", "no base64 param"),
                ("data:;base64,AAAA", "no mime"),
                ("not valid base64!", "invalid base64"),
                (b64(b""), "empty")):
            with self.subTest(why=why):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(ref_audio_base64=bad))
                self.assertEqual(ctx.exception.code, "validation_error")
        with mock.patch.object(tts, "MAX_REF_AUDIO_BYTES", 8):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.parse(valid_body(ref_audio_base64=b64(b"0123456789")))
            self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(tts.MAX_REF_AUDIO_BYTES, 16 * 1024 * 1024)

    def test_missing_ref_audio_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse({"text": "hi"})
        self.assertEqual(ctx.exception.code, "validation_error")


class PreflightTests(WavMixin, unittest.TestCase):
    def settings(self):
        return config.load_settings(env={"COMFYUI_MODELS_ROOT": "C:/x"})

    def test_missing_ffmpeg_is_dependency_missing_before_any_client(self):
        def never(url):
            raise AssertionError("preflight must fail before any client")

        with mock.patch("shutil.which", side_effect=lambda name: None):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), never)
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_worker_not_ok_is_unavailable(self):
        client = FakeConfuciusClient(
            health_payload={"status": "starting", "sample_rate": None})
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), lambda url: client)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")
        self.assertEqual(ctx.exception.http_status, 503)

    def test_worker_wrong_sample_rate_is_unavailable(self):
        client = FakeConfuciusClient(
            health_payload={"status": "ok", "sample_rate": 24000})
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), lambda url: client)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_worker_connection_error_maps_to_its_code(self):
        client = FakeConfuciusClient(health_payload=ConfuciusError(
            "Cannot reach the Confucius worker: refused",
            code="tts_backend_unavailable"))
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), lambda url: client)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_invalid_worker_url_is_unavailable_before_any_client(self):
        def never(url):
            raise AssertionError(
                "preflight must not dial an invalid worker URL")

        settings = config.load_settings(env={
            "COMFYUI_MODELS_ROOT": "C:/x",
            "CONFUCIUS4_TTS_BASE_URL": "http://10.0.0.5:8765"})
        self.assertFalse(settings.tts_worker_url_valid)
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(settings, never)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")
        self.assertEqual(ctx.exception.http_status, 503)
        self.assertIn("CONFUCIUS4_TTS_BASE_URL", ctx.exception.message)

    def test_ok_worker_passes(self):
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            tts.preflight(self.settings(),
                          lambda url: FakeConfuciusClient())


class GenerateCloneTests(WavMixin, unittest.TestCase):
    def run_clone(self, client, body, lock=None):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": "C:/x"})
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            return tts.generate_clone(settings, lambda url: client, body,
                                      lock or threading.Lock())

    def client(self, **kwargs):
        kwargs.setdefault("wav_bytes", self.wav_bytes)
        return FakeConfuciusClient(**kwargs)

    def test_single_text_success_shape(self):
        client = self.client()
        payload = self.run_clone(client, valid_body(output_format="wav"))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["model"], "confucius4-tts")
        self.assertGreaterEqual(payload["processing_time_seconds"], 0)
        self.assertEqual(len(payload["outputs"]), 1)
        item = payload["outputs"][0]
        self.assertEqual(item["audio_base64"], b64(self.wav_bytes))
        self.assertGreater(item["duration_seconds"], 0)
        self.assertEqual(payload["audio_base64"], item["audio_base64"])
        self.assertEqual(payload["duration_seconds"],
                         item["duration_seconds"])
        self.assertNotIn("audio_url", payload)
        self.assertNotIn("r2_key", payload)
        self.assertNotIn("ref_audio_url", payload)

    def test_batch_sends_per_item_multipart_with_same_reference(self):
        client = self.client()
        body = valid_body(text=["第一句", "第二句", "第三句"],
                          language=["zh", "en", "ja"])
        payload = self.run_clone(client, body)
        self.assertEqual(
            [(c["text"], c["lang"]) for c in client.generate_calls],
            [("第一句", "zh"), ("第二句", "en"), ("第三句", "ja")])
        references = {c["reference_bytes"] for c in client.generate_calls}
        self.assertEqual(references, {REF_BYTES})  # decoded once, sent per item
        self.assertTrue(all(c["filename"].startswith("ccvt_ref_")
                            for c in client.generate_calls))
        self.assertEqual(len(payload["outputs"]), 3)
        self.assertNotIn("audio_base64", payload)

    def test_item_failure_fails_whole_request_without_partials(self):
        client = self.client(generate_exc=ConfuciusError(
            "worker blew up", code="tts_backend_error"))
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.run_clone(client, valid_body(text=["a", "b"]))
        self.assertEqual(ctx.exception.code, "tts_backend_error")
        self.assertEqual(ctx.exception.http_status, 502)

    def test_deadline_exhaustion_is_timeout_without_generate(self):
        # monotonic answers: route entry, lock-stage check, then beyond
        # the deadline (started + 270s), so _require_remaining raises.
        client = self.client()
        real = time.monotonic()
        values = iter([real, real + 10 ** 6, real + 10 ** 6])
        with mock.patch.object(tts.time, "monotonic",
                               side_effect=lambda: next(values)):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body())
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")
        self.assertEqual(client.generate_calls, [])

    def test_lock_wait_timeout_is_timeout_without_generate(self):
        lock = threading.Lock()
        lock.acquire()
        client = self.client()
        try:
            with mock.patch.object(tts, "deadline_seconds", return_value=0):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.run_clone(client, valid_body(), lock=lock)
            self.assertEqual(ctx.exception.code, "tts_backend_timeout")
            self.assertEqual(client.generate_calls, [])
        finally:
            lock.release()

    def test_lock_is_released_after_success_and_failure(self):
        lock = threading.Lock()
        self.run_clone(self.client(), valid_body(), lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        with self.assertRaises(tts.TTSRouteError):
            self.run_clone(self.client(generate_exc=ConfuciusError(
                "x", code="tts_backend_error")), valid_body(), lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_validation_error_raises_before_the_lock(self):
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
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.run_clone(self.client(), valid_body(mode="x"), lock=lock)
        self.assertEqual(ctx.exception.code, "mode_unsupported")
        self.assertEqual(lock.acquire_calls, [])


class RouteTests(WavMixin, unittest.TestCase):
    def make_client_app(self, client):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": "C:/x"})
        app = server.create_app(settings=settings,
                                tts_client_factory=lambda url: client)
        return TestClient(app)

    def client(self, **kwargs):
        kwargs.setdefault("wav_bytes", self.wav_bytes)
        return FakeConfuciusClient(**kwargs)

    def test_success_returns_200(self):
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(self.client()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertGreater(body["outputs"][0]["duration_seconds"], 0)

    def test_error_codes_map_to_spec_statuses(self):
        cases = [
            (valid_body(mode="custom_voice"), FakeConfuciusClient(), 422,
             "mode_unsupported"),
            (valid_body(ref_text="x"), FakeConfuciusClient(), 422,
             "mode_unsupported"),
            (valid_body(unknown=1), FakeConfuciusClient(), 422,
             "validation_error"),
            (valid_body(model="nope"), FakeConfuciusClient(), 422,
             "unknown_model"),
            (valid_body(ref_audio_url="https://x"), FakeConfuciusClient(),
             422, "external_url_unsupported"),
            (valid_body(), FakeConfuciusClient(
                health_payload={"status": "starting"}), 503,
             "tts_backend_unavailable"),
            (valid_body(), FakeConfuciusClient(
                health_payload=ConfuciusError(
                    "slow", code="tts_backend_timeout")), 504,
             "tts_backend_timeout"),
            (valid_body(), FakeConfuciusClient(
                generate_exc=ConfuciusError(
                    "x", code="tts_backend_error")), 502,
             "tts_backend_error"),
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

    def test_ffmpeg_missing_is_dependency_missing(self):
        with mock.patch("shutil.which", side_effect=lambda name: None):
            with self.make_client_app(self.client()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "dependency_missing")

    def test_unexpected_exception_is_generic_generation_failed(self):
        with mock.patch.object(tts, "generate_clone",
                               side_effect=RuntimeError("secret internals")):
            with self.make_client_app(FakeConfuciusClient()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "generation_failed")
        self.assertNotIn("secret internals", body["error"])


if __name__ == "__main__":
    unittest.main()
