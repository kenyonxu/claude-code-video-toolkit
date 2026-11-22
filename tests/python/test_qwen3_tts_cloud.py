"""qwen3_tts cloud path (modal/runpod): clone upload, download cascade, timeout.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_cloud.py -v

Every cloud call is faked. Nothing here contacts any host: upload/download/R2
config/cleanup are patched in the qwen3_tts namespace and the provider call
is a stub, so the ref upload, r2_key → url → base64 download cascade and
batch timeout scaling of generate_audio are tested with no R2, no gateway
and no HTTP. There is no local route in this tool anymore (see
test_qwen3_tts_no_local.py for that guard).
"""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402

MP3 = base64.b64encode(b"MP3DATA").decode("ascii")

UPLOAD_URL = "https://r2.example/ref.mp3"
UPLOAD_KEY = "qwen3-tts/input/ref.mp3"
RESULT_KEY = "qwen3-tts/out.mp3"
RESULT_URL = "https://files.example/out.mp3"


def run_generate(text, output, ref_path=None, cloud="modal", cloud_output=None,
                 upload_result=(UPLOAD_URL, UPLOAD_KEY), r2_config=None,
                 timeout=300, **kwargs):
    """Run qwen3_tts.generate_audio with every cloud dependency faked.

    Returns a namespace with:
      result       — generate_audio's return dict
      captured     — provider/payload/timeout seen by the stubbed provider call
      upload       — upload_to_storage mock (None call allowed)
      dl_r2        — download_from_r2 mock
      dl_url       — download_from_url mock
      r2_config    — get_r2_payload_config mock
      cleanup_keys — keys registered with the (faked) r2_cleanup list

    The faked download_from_r2/download_from_url (when a mock return_value is
    not overridden by the caller via dl_r2_returns/dl_url_returns kwargs)
    write recognizable bytes to the destination so the file-write behaviour
    of each cascade branch is observable.
    """
    if cloud_output is None:
        cloud_output = {"success": True, "outputs": [
            {"audio_base64": MP3, "duration_seconds": 1.25}]}
    dl_r2_returns = kwargs.pop("dl_r2_returns", True)
    dl_url_returns = kwargs.pop("dl_url_returns", True)
    captured = {}
    cleanup_keys = []

    def fake_call(provider, payload, tool_name, timeout, **rest):
        captured["provider"] = provider
        captured["payload"] = payload
        captured["timeout"] = timeout
        return cloud_output, 0.5

    def fake_dl_r2(object_key, output_path):
        Path(output_path).write_bytes(b"R2BYTES")
        return dl_r2_returns

    def fake_dl_url(url, output_path, verbose=True):
        Path(output_path).write_bytes(b"URLBYTES")
        return dl_url_returns

    @contextmanager
    def fake_cleanup():
        yield cleanup_keys

    call_kwargs = dict(text=text, output_path=output, verbose=False,
                       cloud=cloud, timeout=timeout)
    call_kwargs.update(kwargs)
    if ref_path is not None:
        call_kwargs.setdefault("ref_audio", str(ref_path))
        call_kwargs.setdefault("ref_text", "transcript")

    with mock.patch("cloud_gpu.call_cloud_endpoint", side_effect=fake_call), \
            mock.patch.object(qwen3_tts, "upload_to_storage",
                              return_value=upload_result) as up, \
            mock.patch.object(qwen3_tts, "download_from_r2",
                              side_effect=fake_dl_r2) as dl_r2, \
            mock.patch.object(qwen3_tts, "download_from_url",
                              side_effect=fake_dl_url) as dl_url, \
            mock.patch.object(qwen3_tts, "get_r2_payload_config",
                              return_value=r2_config) as r2_config_mock, \
            mock.patch.object(qwen3_tts, "r2_cleanup", fake_cleanup):
        result = qwen3_tts.generate_audio(**call_kwargs)
    return SimpleNamespace(result=result, captured=captured, upload=up,
                           dl_r2=dl_r2, dl_url=dl_url, r2_config=r2_config_mock,
                           cleanup_keys=cleanup_keys)


class FakeCloudBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"


class ClonePayloadTests(FakeCloudBase):
    def test_clone_uploads_reference_and_sends_ref_audio_url(self):
        run = run_generate("你好", str(self.out), ref_path=self.ref)
        self.assertTrue(run.result["success"], run.result)
        run.upload.assert_called_once_with(str(self.ref), "qwen3-tts/input")
        payload = run.captured["payload"]["input"]
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["ref_audio_url"], UPLOAD_URL)
        self.assertEqual(payload["ref_text"], "transcript")
        self.assertNotIn("ref_audio_base64", payload)
        self.assertIn(UPLOAD_KEY, run.cleanup_keys)

    def test_clone_upload_failure_fails_before_provider_call(self):
        run = run_generate("你好", str(self.out), ref_path=self.ref,
                           upload_result=(None, None))
        self.assertFalse(run.result["success"])
        self.assertIn("Failed to upload reference audio", run.result["error"])
        self.assertNotIn("provider", run.captured)
        self.assertEqual(run.cleanup_keys, [])

    def test_clone_without_model_omits_model(self):
        run = run_generate("你好", str(self.out), ref_path=self.ref)
        self.assertTrue(run.result["success"])
        self.assertNotIn("model", run.captured["payload"]["input"])

    def test_sampling_params_forwarded(self):
        run = run_generate("你好", str(self.out), ref_path=self.ref,
                           temperature=0.6, top_p=0.9)
        payload = run.captured["payload"]["input"]
        self.assertEqual(payload["temperature"], 0.6)
        self.assertEqual(payload["top_p"], 0.9)

    def test_r2_payload_config_is_included_for_cloud(self):
        config = {"bucket": "b", "endpoint": "https://r2.example"}
        run = run_generate("你好", str(self.out), ref_path=self.ref,
                           r2_config=config)
        self.assertTrue(run.result["success"])
        run.r2_config.assert_called_once()
        self.assertEqual(run.captured["payload"]["input"]["r2"], config)

    def test_custom_voice_sends_speaker_and_not_ref_fields(self):
        run = run_generate("hello", str(self.out), speaker="Ryan")
        self.assertTrue(run.result["success"])
        payload = run.captured["payload"]["input"]
        self.assertEqual(payload["mode"], "custom_voice")
        self.assertEqual(payload["speaker"], "Ryan")
        self.assertNotIn("ref_audio_url", payload)
        self.assertNotIn("ref_text", payload)
        run.upload.assert_not_called()

    def test_runpod_provider_is_passed_through_with_same_payload_shape(self):
        run = run_generate("你好", str(self.out), ref_path=self.ref,
                           cloud="runpod")
        self.assertTrue(run.result["success"])
        self.assertEqual(run.captured["provider"], "runpod")
        self.assertEqual(run.captured["payload"]["input"]["ref_audio_url"],
                         UPLOAD_URL)


class TimeoutScalingTests(FakeCloudBase):
    def test_single_call_uses_base_timeout(self):
        run = run_generate("hello", str(self.out), timeout=300)
        self.assertEqual(run.captured["timeout"], 300)

    def test_timeout_scales_with_batch(self):
        outputs = [{"audio_base64": MP3, "duration_seconds": 1.0}] * 5
        run = run_generate(
            ["一", "二", "三", "四", "五"], [str(self.out)] * 5,
            ref_path=self.ref, timeout=300,
            cloud_output={"success": True, "outputs": outputs})
        self.assertEqual(run.captured["timeout"], 300 * max(1, 5 // 3 + 1))


class DownloadCascadeTests(FakeCloudBase):
    def test_r2_key_result_downloads_from_r2_and_registers_cleanup(self):
        run = run_generate("hello", str(self.out), cloud_output={
            "success": True,
            "outputs": [{"r2_key": RESULT_KEY, "duration_seconds": 1.0}]})
        self.assertTrue(run.result["success"], run.result)
        run.dl_r2.assert_called_once_with(RESULT_KEY, str(self.out))
        run.dl_url.assert_not_called()
        self.assertEqual(self.out.read_bytes(), b"R2BYTES")
        self.assertEqual(run.cleanup_keys, [RESULT_KEY])

    def test_url_fallback_when_r2_download_fails(self):
        run = run_generate("hello", str(self.out), dl_r2_returns=False,
                           cloud_output={
                               "success": True,
                               "outputs": [{"r2_key": RESULT_KEY,
                                            "audio_url": RESULT_URL,
                                            "duration_seconds": 1.0}]})
        self.assertTrue(run.result["success"], run.result)
        run.dl_url.assert_called_once()
        self.assertEqual(run.dl_url.call_args.args, (RESULT_URL, str(self.out)))
        self.assertEqual(run.dl_url.call_args.kwargs, {"verbose": False})
        self.assertEqual(self.out.read_bytes(), b"URLBYTES")
        # Same object the r2_key names — registered however we fetched it.
        self.assertEqual(run.cleanup_keys, [RESULT_KEY])

    def test_url_used_directly_when_no_r2_key(self):
        run = run_generate("hello", str(self.out), cloud_output={
            "success": True,
            "outputs": [{"audio_url": RESULT_URL, "duration_seconds": 1.0}]})
        self.assertTrue(run.result["success"], run.result)
        run.dl_r2.assert_not_called()
        self.assertEqual(self.out.read_bytes(), b"URLBYTES")
        self.assertEqual(run.cleanup_keys, [])

    def test_base64_fallback_writes_decoded_file(self):
        run = run_generate("hello", str(self.out), cloud_output={
            "success": True,
            "outputs": [{"audio_base64": MP3, "duration_seconds": 1.0}]})
        self.assertTrue(run.result["success"], run.result)
        run.dl_r2.assert_not_called()
        run.dl_url.assert_not_called()
        self.assertEqual(self.out.read_bytes(), b"MP3DATA")
        self.assertEqual(run.cleanup_keys, [])

    def test_result_without_any_audio_fails_explicitly(self):
        run = run_generate("hello", str(self.out), cloud_output={
            "success": True, "outputs": [{"duration_seconds": 1.0}]})
        self.assertFalse(run.result["success"])
        self.assertIn("No audio in output[0]", run.result["error"])

    def test_mixed_batch_uses_each_outputs_own_source(self):
        outputs = [
            {"r2_key": RESULT_KEY, "duration_seconds": 1.0},
            {"audio_base64": MP3, "duration_seconds": 1.0},
        ]
        outs = [Path(self.tmp.name) / f"out{i}.mp3" for i in range(2)]
        run = run_generate(["a", "b"], [str(p) for p in outs],
                           cloud_output={"success": True, "outputs": outputs})
        self.assertTrue(run.result["success"], run.result)
        self.assertEqual(outs[0].read_bytes(), b"R2BYTES")
        self.assertEqual(outs[1].read_bytes(), b"MP3DATA")
        self.assertEqual(run.cleanup_keys, [RESULT_KEY])


if __name__ == "__main__":
    unittest.main()
