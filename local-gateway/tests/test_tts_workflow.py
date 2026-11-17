"""tts_workflow: template shape, patching rules and history audio location.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts_workflow.py' -v

Pure data tests: no network, no filesystem writes.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import tts_workflow  # noqa: E402


class TemplateShapeTests(unittest.TestCase):
    def test_template_has_three_nodes_with_stable_ids(self):
        template = tts_workflow.load_template()
        self.assertEqual(set(template), {"1", "2", "3"})
        self.assertEqual(template["1"]["class_type"], "LoadAudio")
        self.assertEqual(template["2"]["class_type"],
                         "AILab_Qwen3TTSVoiceClone_Advanced")
        self.assertEqual(template["3"]["class_type"], "SaveAudioAdvanced")
        for node in template.values():
            self.assertEqual(set(node), {"class_type", "inputs"})

    def test_clone_node_carries_the_fixed_inputs(self):
        inputs = tts_workflow.load_template()["2"]["inputs"]
        expected = dict(tts_workflow.FIXED_TTS_INPUTS)
        expected["do_sample"] = False
        for key, value in expected.items():
            self.assertEqual(inputs[key], value, key)
        # Cloud sampling defaults are never pre-injected.
        self.assertNotIn("temperature", inputs)
        self.assertNotIn("top_p", inputs)

    def test_nodes_are_wired_load_to_clone_to_save(self):
        template = tts_workflow.load_template()
        self.assertEqual(template["2"]["inputs"]["reference_audio"], ["1", 0])
        self.assertEqual(template["3"]["inputs"]["audio"], ["2", 0])

    def test_load_template_returns_a_fresh_copy(self):
        first = tts_workflow.load_template()
        first["2"]["inputs"]["target_text"] = "mutated"
        second = tts_workflow.load_template()
        self.assertEqual(second["2"]["inputs"]["target_text"], "")


class BuildPromptTests(unittest.TestCase):
    def base(self, **overrides):
        kwargs = dict(text="你好", ref_text="ref transcript",
                      reference_audio="sub/ref_001.mp3", language="Chinese",
                      output_format="mp3", output_prefix="ccvt/test/item_0")
        kwargs.update(overrides)
        return tts_workflow.build_clone_prompt(**kwargs)

    def test_mp3_patches_flat_format_and_v0_quality(self):
        prompt = self.base()
        save = prompt["3"]["inputs"]
        self.assertEqual(save["format"], "mp3")
        self.assertEqual(save["format.quality"], "V0")
        self.assertEqual(save["filename_prefix"], "ccvt/test/item_0")

    def test_wav_uses_flac_without_quality_key(self):
        prompt = self.base(output_format="wav")
        save = prompt["3"]["inputs"]
        self.assertEqual(save["format"], "flac")
        self.assertNotIn("format.quality", save)

    def test_request_mapping_reaches_the_right_inputs(self):
        prompt = self.base()
        self.assertEqual(prompt["1"]["inputs"]["audio"], "sub/ref_001.mp3")
        self.assertEqual(prompt["2"]["inputs"]["target_text"], "你好")
        self.assertEqual(prompt["2"]["inputs"]["reference_text"], "ref transcript")
        self.assertEqual(prompt["2"]["inputs"]["language"], "Chinese")

    def test_no_sampling_params_keeps_do_sample_false(self):
        prompt = self.base()
        inputs = prompt["2"]["inputs"]
        self.assertFalse(inputs["do_sample"])
        self.assertNotIn("temperature", inputs)
        self.assertNotIn("top_p", inputs)

    def test_temperature_only_sets_do_sample_and_temperature(self):
        prompt = self.base(temperature=0.5)
        inputs = prompt["2"]["inputs"]
        self.assertTrue(inputs["do_sample"])
        self.assertEqual(inputs["temperature"], 0.5)
        self.assertNotIn("top_p", inputs)

    def test_top_p_only_sets_do_sample_and_top_p(self):
        prompt = self.base(top_p=0.9)
        inputs = prompt["2"]["inputs"]
        self.assertTrue(inputs["do_sample"])
        self.assertEqual(inputs["top_p"], 0.9)
        self.assertNotIn("temperature", inputs)

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(ValueError):
            self.base(output_format="opus")

    def test_patching_never_leaks_into_the_template_file(self):
        self.base(text="mutating call")
        fresh = tts_workflow.load_template()
        self.assertEqual(fresh["2"]["inputs"]["target_text"], "")


class LocateHistoryAudioTests(unittest.TestCase):
    def entry(self, audio):
        return {"status": {"completed": True, "status_str": "success"},
                "outputs": {tts_workflow.SAVE_NODE: {"audio": audio}}}

    def test_returns_the_single_metadata_dict(self):
        meta = {"filename": "a.mp3", "subfolder": "ccvt", "type": "output"}
        self.assertEqual(
            tts_workflow.locate_history_audio(self.entry([meta])), meta)

    def test_accepts_empty_subfolder_string(self):
        meta = {"filename": "a.mp3", "subfolder": "", "type": "output"}
        self.assertEqual(
            tts_workflow.locate_history_audio(self.entry([meta])), meta)

    def test_missing_outputs_raises(self):
        with self.assertRaises(ValueError):
            tts_workflow.locate_history_audio({"outputs": {}})

    def test_empty_audio_list_raises(self):
        with self.assertRaises(ValueError):
            tts_workflow.locate_history_audio(self.entry([]))

    def test_multiple_audio_entries_raise(self):
        meta = {"filename": "a.mp3", "subfolder": "", "type": "output"}
        with self.assertRaises(ValueError):
            tts_workflow.locate_history_audio(self.entry([meta, meta]))

    def test_missing_string_key_raises(self):
        with self.assertRaises(ValueError):
            tts_workflow.locate_history_audio(
                self.entry([{"filename": "a.mp3", "type": "output"}]))


if __name__ == "__main__":
    unittest.main()
