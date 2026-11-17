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


if __name__ == "__main__":
    unittest.main()
