"""confucius_client: health, one-shot multipart generate, bounded reads.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_client.py' -v

Every socket is a fake Session; nothing leaves this process.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from local_gateway import confucius_client as cc  # noqa: E402

WAV = b"RIFF" + b"\x00" * 64


class FakeStreamResponse:
    def __init__(self, status_code=200, content=b"", json_data=None,
                 text="", json_error=False, stream_exc=None):
        self.status_code = status_code
        self._content = content
        self._json_data = json_data
        self.text = text
        self._json_error = json_error
        self._stream_exc = stream_exc
        self.closed = False

    def json(self):
        if self._json_error or self._json_data is None:
            raise ValueError("no json")
        return self._json_data

    def iter_content(self, chunk_size=1):
        view = memoryview(self._content)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start:start + chunk_size])
        if self._stream_exc is not None:
            raise self._stream_exc

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.trust_env = True
        self.closed = False
        self.get_calls = []
        self.post_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        self.closed = True

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


def health_with(session) -> dict:
    # The client opens a fresh Session per call (approved spec), so the
    # patch must span construction AND health(); calling health() outside
    # the patch would build a real Session and dial 127.0.0.1 for real.
    with mock.patch.object(cc.requests, "Session", return_value=session):
        return cc.ConfuciusClient("http://127.0.0.1:8765").health()


class HealthTests(unittest.TestCase):
    def test_ok_payload_is_returned(self):
        session = FakeSession(response=FakeStreamResponse(
            json_data={"status": "ok", "sample_rate": 22050}))
        payload = health_with(session)
        self.assertEqual(payload, {"status": "ok", "sample_rate": 22050})
        self.assertFalse(session.trust_env)
        self.assertTrue(session.closed)

    def test_starting_and_error_503_payloads_are_returned(self):
        for payload in ({"status": "starting", "sample_rate": None},
                        {"status": "error", "sample_rate": None,
                         "error": "RuntimeError: boom"}):
            with self.subTest(status=payload["status"]):
                session = FakeSession(response=FakeStreamResponse(
                    status_code=503, json_data=payload))
                self.assertEqual(health_with(session), payload)

    def test_http_503_without_json_is_unavailable(self):
        session = FakeSession(response=FakeStreamResponse(
            status_code=503, json_error=True))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            health_with(session)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_connection_error_is_unavailable(self):
        session = FakeSession(
            exc=requests.exceptions.ConnectionError("refused"))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            health_with(session)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_timeout_is_timeout(self):
        session = FakeSession(
            exc=requests.exceptions.ReadTimeout("slow"))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            health_with(session)
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")

    def test_non_object_json_is_protocol_error(self):
        session = FakeSession(response=FakeStreamResponse(json_data=[1]))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            health_with(session)
        self.assertEqual(ctx.exception.code, "tts_backend_error")

    def test_missing_status_string_is_protocol_error(self):
        session = FakeSession(response=FakeStreamResponse(json_data={}))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            health_with(session)
        self.assertEqual(ctx.exception.code, "tts_backend_error")


class GenerateTests(unittest.TestCase):
    def call(self, session, deadline=None, **client_kwargs):
        deadline = deadline if deadline is not None else time.monotonic() + 60.0
        with mock.patch.object(cc.requests, "Session", return_value=session):
            client = cc.ConfuciusClient("http://127.0.0.1:8765",
                                        **client_kwargs)
            return client.generate(
                text="你好", lang="zh", reference_bytes=b"REF",
                filename="ccvt_ref_ab12.mp3", content_type="audio/mpeg",
                deadline=deadline)

    def test_success_posts_multipart_once_and_returns_bytes(self):
        session = FakeSession(response=FakeStreamResponse(content=WAV))
        data = self.call(session)
        self.assertEqual(data, WAV)
        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "http://127.0.0.1:8765/api/tts")
        self.assertEqual(kwargs["data"], {"text": "你好", "lang": "zh"})
        filename, content, content_type = kwargs["files"]["reference"]
        self.assertEqual((filename, content, content_type),
                         ("ccvt_ref_ab12.mp3", b"REF", "audio/mpeg"))
        self.assertEqual(len(session.post_calls), 1)
        self.assertTrue(session.trust_env is False)
        self.assertTrue(session.closed)

    def test_read_timeout_uses_remaining_budget(self):
        session = FakeSession(response=FakeStreamResponse(content=WAV))
        deadline = time.monotonic() + 120.0
        with mock.patch.object(cc.requests, "Session", return_value=session):
            cc.ConfuciusClient("http://127.0.0.1:8765").generate(
                text="t", lang="zh", reference_bytes=b"R", filename="r.mp3",
                content_type="audio/mpeg", deadline=deadline)
        timeout = session.post_calls[0][1]["timeout"]
        self.assertEqual(timeout[0], cc.CONNECT_TIMEOUT)
        self.assertGreater(timeout[1], 100.0)
        self.assertLessEqual(timeout[1], 120.0)

    def test_expired_deadline_raises_timeout_before_post(self):
        session = FakeSession(response=FakeStreamResponse(content=WAV))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            self.call(session, deadline=time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")
        self.assertEqual(session.post_calls, [])

    def test_deadline_is_enforced_during_streaming(self):
        session = FakeSession(response=FakeStreamResponse(content=WAV))
        with mock.patch.object(cc.time, "monotonic", side_effect=[0.0, 101.0]):
            with self.assertRaises(cc.ConfuciusError) as ctx:
                self.call(session, deadline=100.0)
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")

    def test_stream_request_exception_is_structured_and_response_closes(self):
        response = FakeStreamResponse(
            content=WAV,
            stream_exc=requests.exceptions.ReadTimeout("stream stalled"))
        session = FakeSession(response=response)
        with self.assertRaises(cc.ConfuciusError) as ctx:
            self.call(session)
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")
        self.assertTrue(response.closed)

    def test_non_200_maps_by_status(self):
        cases = [(503, "tts_backend_unavailable"),
                 (504, "tts_backend_timeout"),
                 (422, "tts_backend_error"),
                 (500, "tts_backend_error")]
        for status, code in cases:
            with self.subTest(status=status):
                session = FakeSession(response=FakeStreamResponse(
                    status_code=status,
                    json_data={"detail": {"error": "bad", "code": "x"}},
                    text='{"detail": {"error": "bad"}}'))
                with self.assertRaises(cc.ConfuciusError) as ctx:
                    self.call(session)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(len(session.post_calls), 1)  # no retry

    def test_connection_error_is_unavailable_with_single_post(self):
        session = FakeSession(
            exc=requests.exceptions.ConnectionError("refused"))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            self.call(session)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")
        self.assertEqual(len(session.post_calls), 1)

    def test_non_wav_body_is_protocol_error(self):
        session = FakeSession(response=FakeStreamResponse(content=b"NOPE"))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            self.call(session)
        self.assertEqual(ctx.exception.code, "tts_backend_error")

    def test_oversized_body_aborts_bounded_read(self):
        big = b"RIFF" + b"\x00" * 4096
        session = FakeSession(response=FakeStreamResponse(content=big))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            self.call(session, max_response_bytes=1024)
        self.assertEqual(ctx.exception.code, "tts_backend_error")
        self.assertIn("1024", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
