"""local_gateway.comfyui: HTTP methods of the ComfyUI client.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v

Fakes only: no request leaves this process.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway.comfyui import (  # noqa: E402
    ComfyUIClient, ComfyUIError, ComfyUITimeout, input_names, input_values,
)

BASE = "http://127.0.0.1:9188"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text="",
                 json_error=False, chunks=None):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text
        self._json_error = json_error
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=None):
        if self._chunks is not None:
            yield from self._chunks

    def close(self):
        self.closed = True

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


class FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.get_response = FakeResponse(json_data={})
        self.post_response = FakeResponse(json_data={})
        self.get_responses = []  # when set, popped per GET (for wait tests)
        self.trust_env = True
        self.closed = False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.get_responses:
            return self.get_responses.pop(0)
        return self.get_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response

    def close(self):
        self.closed = True


def make_client(session):
    return ComfyUIClient(BASE, session=session)


class HttpClientTests(unittest.TestCase):
    def test_system_stats_path_and_timeout(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={"system": {"comfyui_version": "0.45.0"}})
        stats = make_client(s).system_stats()
        self.assertEqual(stats, {"system": {"comfyui_version": "0.45.0"}})
        url, kwargs = s.get_calls[0]
        self.assertEqual(url, BASE + "/system_stats")
        self.assertEqual(kwargs["timeout"], (2, 10))

    def test_object_info_all_nodes_and_single_node(self):
        s = FakeSession()
        schema = {"input": {"required": {"unet_name": [["model.safetensors"], {}]}}}
        s.get_response = FakeResponse(json_data={"UNETLoader": schema})
        client = make_client(s)
        all_nodes = client.object_info()
        single = client.object_info("UNETLoader")
        self.assertEqual(all_nodes, {"UNETLoader": schema})
        self.assertEqual(single, schema)
        self.assertEqual(s.get_calls[0][0], BASE + "/object_info")
        self.assertEqual(s.get_calls[1][0], BASE + "/object_info/UNETLoader")

    def test_submit_posts_prompt_wrapper_and_returns_prompt_id(self):
        s = FakeSession()
        s.post_response = FakeResponse(json_data={"prompt_id": "abc-1"})
        prompt_id = make_client(s).submit({"3": {"class_type": "KSampler"}})
        self.assertEqual(prompt_id, "abc-1")
        url, kwargs = s.post_calls[0]
        self.assertEqual(url, BASE + "/prompt")
        self.assertEqual(kwargs["json"], {"prompt": {"3": {"class_type": "KSampler"}}})
        self.assertEqual(kwargs["timeout"], (2, 10))
        self.assertEqual(len(s.post_calls), 1)  # single POST, no retry

    def test_submit_without_prompt_id_raises_structured_error(self):
        s = FakeSession()
        s.post_response = FakeResponse(json_data={"number": 1})
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).submit({})
        self.assertIn("prompt_id", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "comfyui_error")

    def test_history_path_contains_prompt_id(self):
        s = FakeSession()
        make_client(s).history("abc-1")
        self.assertEqual(s.get_calls[0][0], BASE + "/history/abc-1")

    def test_view_sends_three_query_params_and_returns_bytes(self):
        s = FakeSession()
        s.get_response = FakeResponse(chunks=[b"PNGDATA"])
        data = make_client(s).view("img_00001_.png", "history", "temp")
        self.assertEqual(data, b"PNGDATA")
        url, kwargs = s.get_calls[0]
        self.assertEqual(url, BASE + "/view")
        self.assertEqual(kwargs["params"], {
            "filename": "img_00001_.png", "subfolder": "history", "type": "temp",
        })

    def test_view_defaults_subfolder_empty_and_type_output(self):
        s = FakeSession()
        s.get_response = FakeResponse(chunks=[b"P"])
        make_client(s).view("x.png")
        self.assertEqual(s.get_calls[0][1]["params"], {
            "filename": "x.png", "subfolder": "", "type": "output",
        })

    def test_upload_uses_image_field_and_string_false_overwrite(self):
        s = FakeSession()
        s.post_response = FakeResponse(json_data={"name": "ref.wav", "type": "input"})
        result = make_client(s).upload("ref.wav", b"RIFF", "audio/wav")
        self.assertEqual(result, {"name": "ref.wav", "type": "input"})
        url, kwargs = s.post_calls[0]
        self.assertEqual(url, BASE + "/upload/image")
        self.assertEqual(kwargs["files"], {"image": ("ref.wav", b"RIFF", "audio/wav")})
        self.assertEqual(kwargs["data"], {"type": "input", "overwrite": "false"})

    def test_session_trust_env_false_and_context_manager_closes(self):
        s = FakeSession()
        with make_client(s) as client:
            client.system_stats()
        self.assertFalse(s.trust_env)
        self.assertTrue(s.closed)

    def test_http_error_raises_comfyui_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(status_code=500, text="boom")
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_error")

    def test_json_error_raises_comfyui_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(text="<html>", json_error=True)
        with self.assertRaises(ComfyUIError):
            make_client(s).system_stats()

    def test_non_dict_json_raises_comfyui_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data=[1, 2], text="[1, 2]")
        with self.assertRaises(ComfyUIError):
            make_client(s).system_stats()

    def test_connection_failure_is_comfyui_unreachable(self):
        class Broken(FakeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.ConnectionError("connection refused")

        with self.assertRaises(ComfyUIError) as ctx:
            make_client(Broken()).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_unreachable")


class RequestErrorClassificationTests(unittest.TestCase):
    def test_read_timeout_maps_to_comfyui_request_timeout(self):
        class Slow(FakeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.ReadTimeout("read timed out")

        with self.assertRaises(ComfyUIError) as ctx:
            make_client(Slow()).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_request_timeout")

    def test_connect_timeout_is_a_request_timeout_not_unreachable(self):
        class Slow(FakeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.ConnectTimeout("connect timed out")

        with self.assertRaises(ComfyUIError) as ctx:
            make_client(Slow()).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_request_timeout")

    def test_invalid_url_maps_to_comfyui_error(self):
        class Bad(FakeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.InvalidURL("bad url")

        with self.assertRaises(ComfyUIError) as ctx:
            make_client(Bad()).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_error")

    def test_generic_request_exception_maps_to_comfyui_error(self):
        class Odd(FakeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.ChunkedEncodingError("broken chunk")

        with self.assertRaises(ComfyUIError) as ctx:
            make_client(Odd()).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_error")


class BoundedViewTests(unittest.TestCase):
    def test_view_streams_chunks_and_returns_joined_bytes(self):
        s = FakeSession()
        s.get_response = FakeResponse(chunks=[b"AAA", b"BBB"])
        data = make_client(s).view("out.mp3", "sub", "output")
        self.assertEqual(data, b"AAABBB")
        url, kwargs = s.get_calls[0]
        self.assertEqual(url, BASE + "/view")
        self.assertTrue(kwargs["stream"])
        self.assertTrue(s.get_response.closed)

    def test_view_over_limit_aborts_with_generation_failed(self):
        s = FakeSession()
        s.get_response = FakeResponse(chunks=[b"A" * 10, b"B" * 10])
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).view("big.bin", max_bytes=15)
        self.assertEqual(ctx.exception.code, "generation_failed")
        self.assertIn("15", ctx.exception.message)

    def test_view_exactly_at_limit_succeeds(self):
        s = FakeSession()
        s.get_response = FakeResponse(chunks=[b"A" * 10, b"B" * 5])
        data = make_client(s).view("ok.bin", max_bytes=15)
        self.assertEqual(data, b"A" * 10 + b"B" * 5)

    def test_view_http_error_is_comfyui_error_and_not_streamed(self):
        s = FakeSession()
        s.get_response = FakeResponse(status_code=500, text="boom",
                                      chunks=[b"never"])
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).view("x.png")
        self.assertEqual(ctx.exception.code, "comfyui_error")


class PollSleepTests(unittest.TestCase):
    def test_wait_truncates_sleep_to_remaining_deadline(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={})  # never finishes
        sleeps = []
        with mock.patch.object(time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(ComfyUITimeout):
                make_client(s).wait("p1", deadline=time.monotonic() + 0.05,
                                    poll_interval=5.0)
        self.assertTrue(sleeps)
        for slept in sleeps:
            self.assertLessEqual(slept, 0.05)


class SchemaHelperTests(unittest.TestCase):
    def test_input_values_current_dict_shape(self):
        meta = {"input": {"required": {"model_size": [["0.6B", "1.7B"], {}]}}}
        self.assertEqual(input_values(meta, "model_size"), ["0.6B", "1.7B"])

    def test_input_values_legacy_list_shape(self):
        meta = {"input": {"required": [["model_size", ["0.6B", "1.7B"]]]}}
        self.assertEqual(input_values(meta, "model_size"), ["0.6B", "1.7B"])

    def test_input_values_dynamic_combo_v3_reads_option_keys(self):
        meta = {"input": {"required": {"format": [
            "COMFY_DYNAMICCOMBO_V3",
            {"options": [{"key": "flac"}, {"key": "mp3"}]},
        ]}}}
        self.assertEqual(input_values(meta, "format"), ["flac", "mp3"])

    def test_input_values_dynamic_combo_without_options_is_none(self):
        meta = {"input": {"required": {"format": ["COMBO", "COMBO"]}}}
        self.assertIsNone(input_values(meta, "format"))

    def test_input_values_non_combo_type_is_none(self):
        meta = {"input": {"required": {"target_text": ["STRING", {"multiline": True}]}}}
        self.assertIsNone(input_values(meta, "target_text"))

    def test_input_names_covers_both_shapes_and_optional(self):
        meta = {"input": {
            "required": [["seed", "INT"]],
            "optional": {"language": [["Auto", "Chinese"], {}]},
        }}
        self.assertEqual(input_names(meta), {"seed", "language"})


def success_history(prompt_id):
    return {prompt_id: {"status": {"completed": True, "status_str": "success"},
                        "outputs": {}}}


class WaitTests(unittest.TestCase):
    def test_wait_returns_the_successful_history_entry(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data=success_history("p1"))
        entry = make_client(s).wait("p1", deadline=time.monotonic() + 5,
                                    poll_interval=0.01)
        self.assertEqual(entry["status"]["status_str"], "success")

    def test_wait_polls_until_the_prompt_appears(self):
        s = FakeSession()
        s.get_responses = [
            FakeResponse(json_data={}),
            FakeResponse(json_data={}),
            FakeResponse(json_data=success_history("p2")),
        ]
        entry = make_client(s).wait("p2", deadline=time.monotonic() + 5,
                                    poll_interval=0.01)
        self.assertEqual(entry["status"]["status_str"], "success")
        self.assertEqual(len(s.get_calls), 3)

    def test_wait_execution_error_keeps_prompt_id_and_status(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={
            "p3": {"status": {"completed": True, "status_str": "error",
                              "messages": ["Execution interrupted"]}},
        })
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).wait("p3", deadline=time.monotonic() + 5,
                                poll_interval=0.01)
        self.assertIn("p3", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "execution_error")
        self.assertEqual(ctx.exception.details["status_str"], "error")

    def test_wait_cancelled_entry_is_an_execution_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={
            "p4": {"status": {"completed": False, "status_str": "cancelled"}},
        })
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).wait("p4", deadline=time.monotonic() + 5,
                                poll_interval=0.01)
        self.assertEqual(ctx.exception.code, "execution_error")

    def test_wait_rejects_nonpositive_poll_interval(self):
        with self.assertRaises(ValueError):
            make_client(FakeSession()).wait("p1", deadline=time.monotonic() + 5,
                                            poll_interval=0)

    def test_wait_rejects_deadline_in_the_past(self):
        with self.assertRaises(ValueError):
            make_client(FakeSession()).wait("p1", deadline=time.monotonic() - 1,
                                            poll_interval=0.01)

    def test_wait_timeout_raises_comfyui_timeout_with_prompt_id(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={})  # prompt never appears
        with self.assertRaises(ComfyUITimeout) as ctx:
            make_client(s).wait("p9", deadline=time.monotonic() + 0.05,
                                poll_interval=0.01)
        self.assertEqual(ctx.exception.prompt_id, "p9")
        self.assertIn("p9", ctx.exception.message)
        self.assertTrue(issubclass(ComfyUITimeout, ComfyUIError))


if __name__ == "__main__":
    unittest.main()
