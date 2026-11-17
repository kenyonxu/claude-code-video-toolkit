"""local_gateway.comfyui: HTTP methods of the ComfyUI client.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v

Fakes only: no request leaves this process.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway.comfyui import ComfyUIClient, ComfyUIError  # noqa: E402

BASE = "http://127.0.0.1:9188"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text="",
                 json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text
        self._json_error = json_error

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
        s.get_response = FakeResponse(content=b"PNGDATA")
        data = make_client(s).view("img_00001_.png", "history", "temp")
        self.assertEqual(data, b"PNGDATA")
        url, kwargs = s.get_calls[0]
        self.assertEqual(url, BASE + "/view")
        self.assertEqual(kwargs["params"], {
            "filename": "img_00001_.png", "subfolder": "history", "type": "temp",
        })

    def test_view_defaults_subfolder_empty_and_type_output(self):
        s = FakeSession()
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


if __name__ == "__main__":
    unittest.main()
