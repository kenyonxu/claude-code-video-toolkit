"""local provider in tools/cloud_gpu.py: config lookup and transport.

    uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v

Every network interaction is faked. Nothing here contacts 127.0.0.1 or any
other host, and no assertion depends on this machine's .env: the four
LOCAL_* variables are isolated per test.
"""
from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import cloud_gpu as cg  # noqa: E402

LOCAL_VARS = (
    "LOCAL_TTS_ENDPOINT_URL",
    "LOCAL_IMAGE_ENDPOINT_URL",
    "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
    "LOCAL_VIDEO_ENDPOINT_URL",
)


@contextmanager
def local_env(**values):
    """Snapshot, clear and optionally set the four LOCAL_* vars."""
    saved = {k: os.environ.get(k) for k in LOCAL_VARS}
    for k in LOCAL_VARS:
        os.environ.pop(k, None)
    for k, v in values.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ProviderConfigTests(unittest.TestCase):
    def test_local_env_var_mapping_is_the_four_capability_urls(self):
        self.assertEqual(cg._LOCAL_ENV_VARS, {
            "qwen3_tts": "LOCAL_TTS_ENDPOINT_URL",
            "qwen_image": "LOCAL_IMAGE_ENDPOINT_URL",
            "image_edit": "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
            "h3_video": "LOCAL_VIDEO_ENDPOINT_URL",
        })

    def test_local_config_returns_endpoint_url_for_each_tool(self):
        for tool, var in cg._LOCAL_ENV_VARS.items():
            with local_env(**{var: "http://127.0.0.1:8123/x"}):
                self.assertEqual(cg.get_provider_config("local", tool),
                                 {"endpoint_url": "http://127.0.0.1:8123/x"})

    def test_local_config_is_none_when_env_missing(self):
        with local_env():
            self.assertEqual(cg.get_provider_config("local", "qwen3_tts"),
                             {"endpoint_url": None})

    def test_local_config_is_none_for_unregistered_tool(self):
        with local_env(**dict.fromkeys(LOCAL_VARS, "http://127.0.0.1:8123/x")):
            self.assertEqual(cg.get_provider_config("local", "flux2"),
                             {"endpoint_url": None})

    def test_unknown_provider_error_lists_local(self):
        with self.assertRaises(ValueError) as ctx:
            cg.get_provider_config("gcp", "qwen3_tts")
        self.assertIn("local", str(ctx.exception))

    def test_call_cloud_endpoint_unknown_provider_error_lists_local(self):
        with self.assertRaises(ValueError) as ctx:
            cg.call_cloud_endpoint("gcp", {}, "qwen3_tts", verbose=False)
        self.assertIn("local", str(ctx.exception))

    def test_runpod_and_modal_config_unchanged(self):
        env = {"RUNPOD_API_KEY": "rk", "RUNPOD_QWEN3_TTS_ENDPOINT_ID": "rid",
               "MODAL_QWEN3_TTS_ENDPOINT_URL": "http://m.example", "MODAL_TOKEN_ID": "tid",
               "MODAL_TOKEN_SECRET": "tsec"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cg.get_provider_config("runpod", "qwen3_tts"),
                             {"api_key": "rk", "endpoint_id": "rid"})
            self.assertEqual(cg.get_provider_config("modal", "qwen3_tts"),
                             {"endpoint_url": "http://m.example",
                              "token_id": "tid", "token_secret": "tsec"})


import requests  # noqa: E402  (exceptions for the fake failures)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


class FakeSession:
    """Stands in for the requests.Session created inside _call_local."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.post_calls = []
        self.trust_env = True
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        self.closed = True

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeProgress:
    def __init__(self):
        self.events = []

    def event(self, stage, msg, pct=None, level="dim"):
        self.events.append(stage)

    @contextmanager
    def heartbeat(self, stage="waiting", msg_template=""):
        self.events.append("heartbeat")
        yield


def run_local(session, endpoint_url, tool_name="qwen3_tts", payload=None,
              timeout=600, progress=None):
    with mock.patch.object(cg.requests, "Session", return_value=session):
        return cg._call_local(
            payload=payload if payload is not None else {"input": {"text": "hi"}},
            endpoint_url=endpoint_url,
            tool_name=tool_name,
            timeout=timeout,
            progress_label="Testing",
            progress=progress,
        )


class LocalTransportTests(unittest.TestCase):
    url = "http://127.0.0.1:8123/tts/generate"

    def test_success_returns_tuple_and_complete_event(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True, "audio_base64": "AA"}))
        progress = FakeProgress()
        result, elapsed = run_local(session, self.url, payload={"input": {"text": "hi"}},
                                    progress=progress)
        self.assertEqual(result, {"success": True, "audio_base64": "AA"})
        self.assertGreaterEqual(elapsed, 0)
        self.assertIn("complete", progress.events)

    def test_payload_input_key_is_unwrapped_into_the_json_body(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url, payload={"input": {"text": "hi"}})
        sent_url, kwargs = session.post_calls[0]
        self.assertEqual(sent_url, self.url)
        self.assertEqual(kwargs["json"], {"text": "hi"})

    def test_payload_without_input_is_sent_as_is(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url, payload={"text": "hi"})
        self.assertEqual(session.post_calls[0][1]["json"], {"text": "hi"})

    def test_headers_content_type_only_trust_env_false_session_closed(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url)
        kwargs = session.post_calls[0][1]
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/json"})
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(session.trust_env)
        self.assertTrue(session.closed)

    def test_timeout_is_5_and_timeout_plus_30_with_a_single_post(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url, timeout=600)
        kwargs = session.post_calls[0][1]
        self.assertEqual(kwargs["timeout"], (5, 630))
        self.assertEqual(len(session.post_calls), 1)

    def test_missing_url_error_names_tool_and_env_var_before_any_request(self):
        session = FakeSession(response=FakeResponse(json_data={}))
        result, elapsed = run_local(session, None, tool_name="qwen3_tts")
        self.assertIn("qwen3_tts", result["error"])
        self.assertIn("LOCAL_TTS_ENDPOINT_URL", result["error"])
        self.assertEqual(elapsed, 0)
        self.assertEqual(session.post_calls, [])

    def test_unregistered_tool_error_lists_all_local_env_vars(self):
        result, _ = run_local(FakeSession(), None, tool_name="flux2")
        self.assertIn("flux2", result["error"])
        for var in LOCAL_VARS:
            self.assertIn(var, result["error"])

    def test_non_loopback_endpoint_rejected_before_request(self):
        session = FakeSession(response=FakeResponse(json_data={}))
        result, elapsed = run_local(session, "http://192.168.1.9:8123/tts/generate")
        self.assertIn("loopback", result["error"])
        self.assertEqual(elapsed, 0)
        self.assertEqual(session.post_calls, [])

    def test_endpoint_validation_rejects_each_violation(self):
        cases = [
            "https://127.0.0.1:8123/tts/generate",   # not http
            "http://user:pass@127.0.0.1:8123/tts/generate",  # credentials
            "http://127.0.0.1:8123/tts/generate?x=1",  # query
            "http://127.0.0.1:8123/tts/generate#frag",  # fragment
            "http://127.0.0.1:8123/",                # root path
            "ftp://127.0.0.1:8123/tts/generate",     # not http
            "http://127.0.0.1:bad/tts/generate",     # invalid port
        ]
        for url in cases:
            with self.subTest(url=url):
                session = FakeSession(response=FakeResponse(json_data={}))
                result, elapsed = run_local(session, url)
                self.assertIn("error", result)
                self.assertEqual(elapsed, 0)
                self.assertEqual(session.post_calls, [])

    def test_connection_failure_mentions_comfyui_and_start_script(self):
        session = FakeSession(exc=requests.exceptions.ConnectionError("connection refused"))
        result, _ = run_local(session, self.url)
        self.assertIn("start_gateway.bat", result["error"])
        self.assertIn("ComfyUI", result["error"])

    def test_timeout_returns_error_dict_not_exception(self):
        session = FakeSession(exc=requests.exceptions.ReadTimeout("read timed out"))
        result, _ = run_local(session, self.url)
        self.assertIn("timed out", result["error"])

    def test_non_200_prefers_json_error_field(self):
        body = FakeResponse(status_code=422, json_data={"error": "bad input"},
                            text='{"error": "bad input"}')
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("422", result["error"])
        self.assertIn("bad input", result["error"])

    def test_non_200_prefers_json_detail_field(self):
        body = FakeResponse(status_code=422, json_data={"detail": "field required"},
                            text='{"detail": "field required"}')
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("field required", result["error"])

    def test_non_200_plain_text_is_truncated_to_500_chars(self):
        body = FakeResponse(status_code=500, text="x" * 2000)
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("x" * 500, result["error"])
        self.assertNotIn("x" * 501, result["error"])

    def test_non_json_response_returns_error(self):
        body = FakeResponse(status_code=200, text="<html>oops</html>", json_error=True)
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("error", result)

    def test_non_dict_json_returns_error(self):
        body = FakeResponse(status_code=200, json_data=[1, 2, 3], text="[1, 2, 3]")
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("error", result)

    def test_application_error_emits_error_event_and_no_complete(self):
        body = FakeResponse(status_code=200, json_data={"error": "comfyui node failed"})
        progress = FakeProgress()
        result, _ = run_local(FakeSession(response=body), self.url, progress=progress)
        self.assertEqual(result, {"error": "comfyui node failed"})
        self.assertIn("error", progress.events)
        self.assertNotIn("complete", progress.events)


class LocalDispatchTests(unittest.TestCase):
    tts_url = "http://127.0.0.1:8123/tts/generate"

    def test_call_cloud_endpoint_dispatches_local_end_to_end(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        progress = FakeProgress()
        with local_env(LOCAL_TTS_ENDPOINT_URL=self.tts_url):
            with mock.patch.object(cg.requests, "Session", return_value=session):
                result, _ = cg.call_cloud_endpoint(
                    "local", {"input": {"text": "hi"}}, "qwen3_tts",
                    timeout=100, progress=progress, verbose=False)
        self.assertEqual(result, {"success": True})
        sent_url, kwargs = session.post_calls[0]
        self.assertEqual(sent_url, self.tts_url)
        self.assertEqual(kwargs["json"], {"text": "hi"})
        self.assertEqual(kwargs["timeout"], (5, 130))
        self.assertIn("complete", progress.events)

    def test_image_generate_and_edit_select_their_own_env_vars(self):
        cases = [
            ("qwen_image", "LOCAL_IMAGE_ENDPOINT_URL", "/image/generate"),
            ("image_edit", "LOCAL_IMAGE_EDIT_ENDPOINT_URL", "/image/edit"),
        ]
        for tool, var, path in cases:
            with self.subTest(tool=tool):
                url = f"http://127.0.0.1:8123{path}"
                session = FakeSession(response=FakeResponse(json_data={"success": True}))
                with local_env(**{var: url}):
                    with mock.patch.object(cg.requests, "Session", return_value=session):
                        cg.call_cloud_endpoint("local", {"input": {}}, tool, verbose=False)
                self.assertEqual(session.post_calls[0][0], url)

    def test_missing_url_through_dispatch_returns_actionable_error(self):
        with local_env():
            result, elapsed = cg.call_cloud_endpoint(
                "local", {"input": {}}, "h3_video", verbose=False)
        self.assertIn("LOCAL_VIDEO_ENDPOINT_URL", result["error"])
        self.assertEqual(elapsed, 0)


if __name__ == "__main__":
    unittest.main()
