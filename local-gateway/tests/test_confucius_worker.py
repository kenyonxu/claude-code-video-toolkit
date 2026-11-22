"""confucius_worker: health states, multipart /api/tts, temp cleanup, lock,
runtime config.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_worker.py' -v

No torch anywhere: the engine is a fake and module import must never pull
ConfuciusTTS (that import lives inside ConfuciusEngine only). No real
checkout either: the runtime-config tests build a fake one in a temp dir.
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import confucius_worker as cw  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


def wav_bytes(seconds: float = 0.1) -> bytes:
    pcm = b"\x00\x01" * int(22050 * seconds)
    return cw.pcm16_wav_bytes(pcm, 22050)


class FakeEngine:
    def __init__(self, result: bytes | None = None, exc: Exception | None = None):
        self.result = result if result is not None else wav_bytes()
        self.exc = exc
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def generate(self, text: str, lang: str, prompt_path: str) -> bytes:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((text, lang, Path(prompt_path)))
            if self.exc is not None:
                raise self.exc
            return self.result
        finally:
            with self._guard:
                self.active -= 1


def ok_state(engine=None):
    return cw.WorkerState(status="ok", sample_rate=22050,
                          engine=engine if engine is not None else FakeEngine())


# The relevant lines of upstream config/inference_config.yaml. It locates the
# tokenizer and the w2v-bert statistics relative to the working directory.
FAKE_UPSTREAM_CONFIG = """\
# ===================== Path Configuration =====================
log_dir:

seed:

paths:
  tokenizer_path: ./checkpoints
  w2v_bert_path: facebook/w2v-bert-2.0
  w2v_stat: ./checkpoints/wav2vec2bert_stats.pt

  style_encoder:
    target: external.campplus.CAMPPlus
    init_args:
      feat_dim: 80
    checkpoint: campplus_cn_common.bin

  vocoder_path: nvidia/bigvgan_v2_22khz_80band_256x
  t2s_checkpoint: t2s_model.safetensors
  s2a_checkpoint: s2a_model.pt

audio:
  target_sample_rate: 22050
"""
TOKENIZER_LINE = 6
STATS_LINE = 8
assert FAKE_UPSTREAM_CONFIG.split("\n")[TOKENIZER_LINE].startswith(
    "  tokenizer_path:"), "fixture line numbers changed"
assert FAKE_UPSTREAM_CONFIG.split("\n")[STATS_LINE].startswith(
    "  w2v_stat:"), "fixture line numbers changed"


def make_fake_checkout(parent: Path, config: str = FAKE_UPSTREAM_CONFIG) -> Path:
    """A stand-in for the Confucius4-TTS checkout: only its config exists."""
    root = parent / "confucius"
    (root / "checkpoints").mkdir(parents=True)
    config_path = root.joinpath(*cw.UPSTREAM_CONFIG)
    config_path.parent.mkdir()
    config_path.write_bytes(config.encode("utf-8"))
    return root


class Pcm16WavTests(unittest.TestCase):
    def test_pcm16_wav_bytes_is_mono_22050_pcm16(self):
        data = cw.pcm16_wav_bytes(b"\x00\x00" * 2205, 22050)
        with wave.open(io.BytesIO(data), "rb") as reader:
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), 22050)
            self.assertEqual(reader.getcomptype(), "NONE")


class HealthTests(unittest.TestCase):
    def test_starting_is_503_with_status(self):
        with TestClient(cw.create_worker_app(cw.WorkerState())) as tc:
            response = tc.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "starting", "sample_rate": None})

    def test_ok_is_200_with_sample_rate(self):
        with TestClient(cw.create_worker_app(ok_state())) as tc:
            response = tc.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "sample_rate": 22050})

    def test_error_is_503_without_traceback(self):
        state = cw.WorkerState(status="error", sample_rate=None,
                               error="RuntimeError: boom")
        with TestClient(cw.create_worker_app(state)) as tc:
            response = tc.get("/health")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["error"], "RuntimeError: boom")
        self.assertNotIn("Traceback", str(body))


class ApiTtsTests(unittest.TestCase):
    def post(self, tc, text="你好", lang="zh", name="ref.mp3",
             content=b"REFERENCE", content_type="audio/mpeg"):
        return tc.post("/api/tts", data={"text": text, "lang": lang},
                       files={"reference": (name, content, content_type)})

    def test_ok_returns_pcm16_mono_22050_wav_and_deletes_temp(self):
        engine = FakeEngine()
        with mock.patch.object(cw.tempfile, "NamedTemporaryFile",
                               wraps=cw.tempfile.NamedTemporaryFile) as ntf, \
                TestClient(cw.create_worker_app(ok_state(engine))) as tc:
            response = self.post(tc)
        self.assertEqual(response.status_code, 200)
        prompt_path = engine.calls[0][2]
        self.assertFalse(prompt_path.exists())  # deleted after the request
        self.assertEqual(response.headers["content-type"], "audio/wav")
        with wave.open(io.BytesIO(response.content), "rb") as reader:
            self.assertEqual(reader.getframerate(), 22050)
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getsampwidth(), 2)
        text, lang, prompt_path = engine.calls[0]
        self.assertEqual((text, lang), ("你好", "zh"))
        self.assertTrue(prompt_path.name.startswith("ccvt_ref_"))
        self.assertTrue(str(prompt_path).endswith(".mp3"))
        ntf.assert_called_once()

    def test_validation_rejects_bad_text_lang_and_empty_reference(self):
        cases = [
            {"text": "", "lang": "zh", "content": b"x"},
            {"text": "好", "lang": "ZH", "content": b"x"},
            {"text": "好", "lang": "klingon", "content": b"x"},
            {"text": "好" * 1025, "lang": "zh", "content": b"x"},
            {"text": "好", "lang": "zh", "content": b""},
        ]
        engine = FakeEngine()
        with TestClient(cw.create_worker_app(ok_state(engine))) as tc:
            for case in cases:
                with self.subTest(case=case["text"][:10] + case["lang"]):
                    response = self.post(tc, **case)
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(engine.calls, [])

    def test_oversized_reference_is_422(self):
        engine = FakeEngine()
        with mock.patch.object(cw, "MAX_REFERENCE_BYTES", 8), \
                TestClient(cw.create_worker_app(ok_state(engine))) as tc:
            response = self.post(tc, content=b"0123456789")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(engine.calls, [])

    def test_not_ready_is_503_without_engine_call(self):
        engine = FakeEngine()
        state = cw.WorkerState(status="starting", sample_rate=None, engine=None)
        with TestClient(cw.create_worker_app(state)) as tc:
            response = self.post(tc)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(engine.calls, [])

    def test_generation_exception_is_sanitized_500(self):
        engine = FakeEngine(exc=RuntimeError("secret /abs/path traceback"))
        with TestClient(cw.create_worker_app(ok_state(engine))) as tc:
            response = self.post(tc)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"],
                         {"error": "generation failed on the worker",
                          "code": "generation_failed"})

    def test_generation_is_serialized_under_the_worker_lock(self):
        engine = FakeEngine()
        state = ok_state(engine)
        with TestClient(cw.create_worker_app(state)) as tc:
            threads = [threading.Thread(
                target=lambda: self.post(tc, text=f"t{i}")) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(engine.calls), 3)
        self.assertEqual(engine.max_active, 1)


class StartupHelperTests(unittest.TestCase):
    def test_apply_offline_env_sets_both_variables(self):
        with mock.patch.dict(cw.os.environ, {}, clear=False):
            cw.os.environ.pop("HF_HUB_OFFLINE", None)
            cw.os.environ.pop("TRANSFORMERS_OFFLINE", None)
            cw.apply_offline_env()
            self.assertEqual(cw.os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(cw.os.environ["TRANSFORMERS_OFFLINE"], "1")

    def test_apply_offline_env_pins_repository_model_cache(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(cw.os.environ, {}, clear=True):
            root = Path(tmp)
            cw.apply_offline_env(root)
            self.assertEqual(cw.os.environ["HF_HUB_CACHE"],
                             str(root / "models" / "hf"))
            self.assertEqual(cw.os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(cw.os.environ["TRANSFORMERS_OFFLINE"], "1")

    def test_main_requires_an_explicit_root(self):
        with mock.patch.dict(cw.os.environ, {}, clear=True), \
                mock.patch.object(cw, "write_runtime_config",
                                  side_effect=AssertionError("no default root")), \
                mock.patch.object(cw.sys, "stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cw.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_languages_are_the_fourteen_lower_case_codes(self):
        self.assertEqual(cw.LANGUAGES, ("zh", "en", "ja", "ko", "de", "fr",
                                        "es", "id", "it", "ms", "pt", "ru",
                                        "th", "vi"))

    def test_runtime_config_rewrites_exactly_the_two_checkpoint_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_checkout(Path(tmp))
            source = root.joinpath(*cw.UPSTREAM_CONFIG)
            before, before_mtime = source.read_bytes(), source.stat().st_mtime_ns
            target_dir = Path(tmp) / "runtime"
            target_dir.mkdir()

            generated = cw.write_runtime_config(root, target_dir)

            self.assertEqual(generated, target_dir / "inference_config.yaml")
            old = FAKE_UPSTREAM_CONFIG.split("\n")
            new = generated.read_bytes().decode("utf-8").split("\n")
            self.assertEqual(len(new), len(old))
            changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
            self.assertEqual(changed, [TOKENIZER_LINE, STATS_LINE])
            absolute = root.resolve().as_posix()
            self.assertEqual(new[TOKENIZER_LINE],
                             f'  tokenizer_path: "{absolute}/checkpoints"')
            self.assertEqual(new[STATS_LINE], f'  w2v_stat: "'
                             f'{absolute}/checkpoints/wav2vec2bert_stats.pt"')
            # The upstream config and its directory are exactly as they were.
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(source.stat().st_mtime_ns, before_mtime)
            self.assertEqual([p.name for p in source.parent.iterdir()],
                             ["inference_config.yaml"])

    def test_runtime_config_keeps_the_source_line_endings(self):
        crlf = FAKE_UPSTREAM_CONFIG.replace("\n", "\r\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_checkout(Path(tmp), crlf)
            target_dir = Path(tmp) / "runtime"
            target_dir.mkdir()

            generated = cw.write_runtime_config(root, target_dir)

            lines = generated.read_bytes().split(b"\n")
            self.assertEqual(lines[-1], b"")
            self.assertTrue(all(line.endswith(b"\r") for line in lines[:-1]))
            self.assertTrue(lines[TOKENIZER_LINE].startswith(
                b'  tokenizer_path: "'))
            self.assertTrue(lines[STATS_LINE].startswith(b'  w2v_stat: "'))

    def test_runtime_config_paths_are_absolute_and_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_fake_checkout(Path(tmp))
            unnormalized = Path(tmp) / "confucius" / ".." / "confucius"
            self.assertIn("..", unnormalized.parts)
            target_dir = Path(tmp) / "runtime"
            target_dir.mkdir()

            generated = cw.write_runtime_config(unnormalized, target_dir)

            text = generated.read_text(encoding="utf-8")
            expected = (Path(tmp) / "confucius").resolve().as_posix()
            self.assertIn(f'  tokenizer_path: "{expected}/checkpoints"', text)
            self.assertNotIn("..", text)

    def test_runtime_config_fails_closed_when_the_layout_changed(self):
        broken = {
            "missing stats": FAKE_UPSTREAM_CONFIG.replace(
                "  w2v_stat: ./checkpoints/wav2vec2bert_stats.pt\n", ""),
            "duplicate tokenizer": FAKE_UPSTREAM_CONFIG.replace(
                "  tokenizer_path: ./checkpoints\n",
                "  tokenizer_path: ./checkpoints\n  tokenizer_path: ./other\n"),
            "tokenizer not ./ relative": FAKE_UPSTREAM_CONFIG.replace(
                "tokenizer_path: ./checkpoints", "tokenizer_path: checkpoints"),
            "stats not ./ relative": FAKE_UPSTREAM_CONFIG.replace(
                "w2v_stat: ./checkpoints/wav2vec2bert_stats.pt",
                "w2v_stat: checkpoints/wav2vec2bert_stats.pt"),
        }
        for label, config in broken.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = make_fake_checkout(Path(tmp), config)
                target_dir = Path(tmp) / "runtime"
                target_dir.mkdir()

                with self.assertRaises(cw.ConfigError):
                    cw.write_runtime_config(root, target_dir)

                self.assertEqual(list(target_dir.iterdir()), [])  # not written

    def test_main_keeps_the_temp_config_until_uvicorn_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_checkout(Path(tmp))
            seen = {}

            def fake_run(app, host, port):
                seen["bind"] = (host, port)
                seen["config"] = loader.call_args.args[1]
                seen["exists_while_serving"] = seen["config"].exists()
                seen["text"] = seen["config"].read_text(encoding="utf-8")
                seen["hf_cache"] = cw.os.environ.get("HF_HUB_CACHE")

            self.addCleanup(setattr, sys, "path", list(sys.path))
            with mock.patch.dict(cw.os.environ, {"TEMP": tmp, "TMP": tmp},
                                 clear=True), \
                    mock.patch.object(cw, "start_loader") as loader, \
                    mock.patch("uvicorn.run", side_effect=fake_run):
                cw.main(["--root", str(root), "--host", "127.0.0.1",
                         "--port", "8765"])

            self.assertEqual(seen["bind"], ("127.0.0.1", 8765))
            self.assertEqual(loader.call_count, 1)
            state, config_path, device = loader.call_args.args
            self.assertEqual((state.status, device), ("starting", "cuda"))
            self.assertTrue(seen["exists_while_serving"])
            self.assertIn(f'  tokenizer_path: "'
                          f'{root.resolve().as_posix()}/checkpoints"',
                          seen["text"])
            self.assertEqual(seen["hf_cache"], str(root / "models" / "hf"))
            # uvicorn has exited: the temporary config and its directory are gone.
            self.assertFalse(config_path.exists())
            self.assertFalse(config_path.parent.exists())
            self.assertEqual([p.name for p in Path(tmp).iterdir()],
                             ["confucius"])

    def test_main_exits_2_when_the_upstream_config_is_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "confucius"
            root.mkdir()  # no config/inference_config.yaml at all
            err = io.StringIO()
            self.addCleanup(setattr, sys, "path", list(sys.path))
            with mock.patch.dict(cw.os.environ, {"TEMP": tmp, "TMP": tmp},
                                 clear=True), \
                    mock.patch.object(cw, "start_loader") as loader, \
                    mock.patch.object(cw.sys, "stderr", new=err):
                with self.assertRaises(SystemExit) as ctx:
                    cw.main(["--root", str(root)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("Error:", err.getvalue())
            loader.assert_not_called()
            self.assertEqual([p.name for p in Path(tmp).iterdir()],
                             ["confucius"])


if __name__ == "__main__":
    unittest.main()
