"""local_gateway.config: read, normalize and validate gateway settings.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_config.py' -v

No test touches the developer's real .env (explicit env mappings or a temp
dotenv path only), no real models directory (temp dirs only), no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import config  # noqa: E402


class BaseUrlTests(unittest.TestCase):
    def test_defaults_when_env_is_empty(self):
        s = config.load_settings(env={})
        self.assertEqual(s.base_url, "http://127.0.0.1:9188")
        self.assertTrue(s.base_url_valid)
        self.assertIsNone(s.models_root)
        self.assertFalse(s.models_root_valid)
        self.assertFalse(s.config_valid)
        self.assertEqual([e.code for e in s.errors], ["config_missing"])

    def test_trailing_slash_is_stripped(self):
        s = config.load_settings(env={"COMFYUI_BASE_URL": "http://127.0.0.1:9188/"})
        self.assertEqual(s.base_url, "http://127.0.0.1:9188")

    def test_loopback_hosts_are_accepted(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                s = config.load_settings(env={"COMFYUI_BASE_URL": f"http://{host}:9188"})
                self.assertTrue(s.base_url_valid)

    def test_rejected_base_urls(self):
        bad = [
            "https://127.0.0.1:9188",          # not http
            "http://10.1.2.3:9188",            # not loopback
            "http://example.com",              # not loopback
            "http://user:pw@127.0.0.1:9188",   # credentials
            "http://127.0.0.1:9188?x=1",       # query
            "http://127.0.0.1:9188#frag",      # fragment
            "http://127.0.0.1:9188/comfy",     # non-root path
            "127.0.0.1:9188",                  # no scheme
            "http://127.0.0.1:bad",            # invalid port
        ]
        for url in bad:
            with self.subTest(url=url):
                s = config.load_settings(env={"COMFYUI_BASE_URL": url})
                self.assertFalse(s.base_url_valid)
                self.assertIn("config_invalid", [e.code for e in s.errors])

    def test_process_env_wins_over_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("COMFYUI_BASE_URL=http://127.0.0.1:9199\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COMFYUI_BASE_URL": "http://127.0.0.1:9200"}):
                s = config.load_settings(dotenv_path=dotenv)
        self.assertEqual(s.base_url, "http://127.0.0.1:9200")

    def test_dotenv_is_used_when_process_env_has_no_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("COMFYUI_BASE_URL=http://127.0.0.1:9199/\n", encoding="utf-8")
            with mock.patch.dict(os.environ):
                os.environ.pop("COMFYUI_BASE_URL", None)
                s = config.load_settings(dotenv_path=dotenv)
        self.assertEqual(s.base_url, "http://127.0.0.1:9199")


class ModelsRootTests(unittest.TestCase):
    def test_missing_models_root_is_config_missing(self):
        s = config.load_settings(env={})
        self.assertIn("config_missing", [e.code for e in s.errors])
        self.assertIsNone(s.models_root)
        action = next(e for e in s.errors if e.code == "config_missing").action
        self.assertIn("local-gateway/.env", action)

    def test_relative_models_root_is_config_invalid(self):
        s = config.load_settings(env={"COMFYUI_MODELS_ROOT": "ComfyUI/models"})
        self.assertFalse(s.models_root_valid)
        self.assertIn("config_invalid", [e.code for e in s.errors])
        self.assertIsNone(s.models_root)

    def test_nonexistent_models_root_is_config_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-dir"
            s = config.load_settings(env={"COMFYUI_MODELS_ROOT": str(missing)})
        self.assertFalse(s.models_root_valid)
        self.assertIn("config_invalid", [e.code for e in s.errors])

    def test_existing_absolute_models_root_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = config.load_settings(env={"COMFYUI_MODELS_ROOT": tmp})
            self.assertTrue(s.models_root_valid)
            self.assertTrue(s.config_valid)
            self.assertEqual(s.models_root, tmp)
            self.assertEqual(s.errors, ())

    def test_tilde_models_root_is_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "models").mkdir()
            with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}):
                s = config.load_settings(env={"COMFYUI_MODELS_ROOT": "~/models"})
        self.assertTrue(s.models_root_valid)
        self.assertEqual(s.models_root, os.path.join(tmp, "models"))


if __name__ == "__main__":
    unittest.main()
