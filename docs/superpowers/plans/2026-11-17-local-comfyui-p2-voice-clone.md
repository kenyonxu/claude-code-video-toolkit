# Local ComfyUI Provider P2 (Voice Clone) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first real generation chain of the local provider: `POST /tts/generate` on the loopback gateway translates a clone-only request into an original `AILab_Qwen3TTSVoiceClone_Advanced` ComfyUI workflow (upload, submit, poll, bounded view, MP3 passthrough / FLAC-to-WAV transcode), and `qwen3_tts.py` / `voiceover.py` reach it end-to-end with model and timeout passthrough, proven by three real clone generations on the acceptance machine.

**Architecture:** The gateway grows four focused modules: `tts_workflow.py` (pure prompt template + patching + history audio location), `audio.py` (bounded download budget, ffprobe validation, WAV transcode), `tts.py` (request validation, clone-only preflight, deadline, per-process lock, ordered batch orchestration) and a `POST /tts/generate` route in `server.py` that only assembles dependencies and JSON responses. `comfyui.py` is hardened first (timeout classification, bounded `view`, poll-sleep truncation, shared object_info schema helpers). The CLIs add `local` as a third provider value inside the existing `call_cloud_endpoint` dispatch: local clone sends `ref_audio_base64`, never touches R2/0x0.st/litterbox, and accepts base64-only results. CustomVoice and VoiceDesign are *not* implemented anywhere.

**Tech Stack:** Python >=3.10, uv (plain `unittest`, never pytest), FastAPI + TestClient (httpx, dev group) for the gateway, `requests` for both transports, FFmpeg/ffprobe subprocess (`shell=False`) for audio, Git Bash on Windows. Two test domains: root `tests/python` via `uv run --extra youtube`, gateway `local-gateway/tests` via `uv run --directory local-gateway`.

**Spec:** `docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md` (clone-only revision, confirmed 2026-11-17; authoritative for every value below). Companion: `docs/local-comfyui-provider-roadmap.md`, `docs/local-comfyui-provider-p0-p1-design.md`, plan `docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md`. Where wording could differ, the spec wins.

## Global Constraints

Copied verbatim from the spec; every task implicitly includes all of these.

- **Clone-only:** "P2 只做 Voice Clone"。Only `AILab_Qwen3TTSVoiceClone_Advanced` and `workflows/tts_voice_clone.json` are implemented. "CustomVoice 和 VoiceDesign 本期不做：不实现、不写入 health 能力、不作为验收项"。Non-clone requests return HTTP 422 `mode_unsupported`; `custom_voice`/`voice_design` fields also return `mode_unsupported`; `ref_audio_url` and `r2` return 422 `external_url_unsupported`。Preflight checks "非空目录 `Qwen3-TTS-12Hz-1.7B-Base` 与 `Qwen3-TTS-Tokenizer-12Hz`，每个目录至少有一个目录项" and nothing else from the TTS tree.
- **Deadline:** `deadline_seconds = 270 * max(1, item_count // 3 + 1)`，computed "从进入路由、等待 lock 开始"，covering "上传、submit、poll、view、ffprobe 和 FFmpeg"。Client side: `timeout=300` with `timeout * max(1, len(texts)//3 + 1)`。Poll sleep is truncated to the remaining deadline; FFmpeg subprocess timeout uses the remaining time; "不自动 `/interrupt`，不重试 `/prompt`"。
- **Bounded view:** "单文件最大 64 MiB，单请求所有输出最大 128 MiB。超限返回 `generation_failed`"。
- **Request contract:** top-level fields only `model` (default `qwen3-tts-1.7B`， unknown → `unknown_model`), `text` (single or "最多 8 项非空字符串数组"), `mode` (default `clone`), `language` (default `Auto`, scalar broadcast, array must match text length, values in the runtime enum), `output_format` (`mp3` default or `wav`), `ref_audio_base64` (required, data-URL prefix allowed, "解码后最大 16 MiB"), `ref_text` (required non-empty), `temperature` (finite, "范围 0.1-2.0"), `top_p` (finite, "范围 0.0-1.0"). Unknown fields → `validation_error`; "显式 null 的 temperature/top_p 视为未提供"。
- **Sampling:** "未提供采样参数时 `do_sample=false`。temperature 或 top_p 任一显式提供时 `do_sample=true`，并只补丁提供的值，不补写云端默认数值"。
- **Save format:** "`SaveAudioAdvanced.format` 是扁平字符串。MP3 使用 `format="mp3"` 和独立的 `format.quality="V0"`；FLAC 使用 `format="flac"`，不写 `format.quality`"。WAV = "将 FLAC 字节写入请求临时目录，用 FFmpeg 参数数组和 `shell=False` 转为 `pcm_s16le` WAV"。Final bytes must pass ffprobe with "duration_seconds > 0"。base64 carries no data-URL prefix.
- **History:** audio metadata comes only from `entry["outputs"][save_node_id]["audio"][0]` with `filename/subfolder/type`; "不得自行拼 ComfyUI 输出路径"。
- **Upload:** reference uploaded "每次请求只上传一次"; `LoadAudio.audio` joins the upload response's `name` with a non-empty `subfolder` ("不使用调用方原文件名")； upload without a usable name → `generation_failed`。
- **Batch:** "每个文本独立 submit/poll/view，保持输入顺序，并复用同一次上传"； "任一 item 失败时整个请求失败，不返回已生成 item 的部分 base64，临时文件按请求清理"。One `threading.Lock` in app state serializes clone per gateway process; "`/health` 不获取此锁".
- **Error table:** `mode_unsupported` 422, `validation_error` 422, `unknown_model` 422, `external_url_unsupported` 422, `dependency_missing` 503, `comfyui_unreachable` 503, `comfyui_request_timeout` 504, `comfyui_timeout` 504, `comfyui_error` 502, `generation_failed` 500. "requests Timeout 映射为 `comfyui_request_timeout`；连接失败才是 `comfyui_unreachable`，InvalidURL/MissingSchema 和其他请求错误归为 `comfyui_error`"。Error messages must not leak "r2 凭据、完整模型根路径或 traceback".
- **CLI:** `--cloud` gains `local` (default stays `modal` in both tools); qwen3_tts gains free-string `--model` (`generate_audio(..., model=None)`, only non-None enters the payload); local requires `--ref-audio` + `--ref-text` before any HTTP; `--design-instruct`/builtin-speaker calls rejected under local; local never calls `upload_to_storage()` and never sends `ref_audio_url`/r2; results accept `audio_base64` only (audio_url/r2_key → explicit failure); `--setup --cloud local` exits 2 with a local hint. voiceover gains `--qwen-model` (all local clone paths) and `--timeout` (default 300); "brand VoiceDesign 或其他非 clone 路径在 local 下明确拒绝"; "ElevenLabs 的 `--model` 保持不变"; "Modal/RunPod 不变"。
- **Health/registry:** tts entry gets `modes=(clone,)`, `output_formats=(mp3, wav)`, `model_size=1.7B`, fixed Advanced params and batch/deadline limits; health adds `dependencies: { ffmpeg: boolean, ffprobe: boolean }` with `dependency_missing` errors and "全局 `ready` 同时要求两个工具可用"; "异常 fallback 不再使用浅拷贝共享列表，改用 skeleton 工厂"; "object_info 每节点每次 health 请求只拉取一次" with a call-count assertion; "`IMPLEMENTED_ROUTES` 只能在真实验收后加 `tts`"，other three routes stay false; "CustomVoice/VoiceDesign 不出现在注册表 modes 或 health 能力中".
- **Tests:** "所有网络、模型目录和 ComfyUI 都使用 fake"（only spec §14 real-machine acceptance is exempt）。All uv-run test commands are prefixed `TEMP='E:\GitHub' TMP='E:\GitHub'`。No pytest anywhere. ffmpeg/ffprobe subprocess use in unit tests is allowed (local process, not network).
- **Real acceptance material (only these):** audio `C:/Users/kenyo/Downloads/Music/voice_02.mp3` (12.616875 s) and `REF_TEXT='为什么不用suno及类似的音乐生成平台? 因为贵，对游戏需要的音乐生成不友好，而且suno这种直接生成成曲的基本无法对成品做调整'`. Three generations only: qwen3_tts clone MP3, qwen3_tts clone WAV, voiceover clone MP3. Never reference `custom.mp3`, `designed.wav` or any `lugh-ref.wav`.
- **Out of scope:** no image/video routes, no 0.6B, no `x_vector_only`/`max_new_tokens`/`top_k`/`repetition_penalty`/`attention`/`device`/`precision` public params, no changes to `video_job.py`, `verify_setup.py`, root `.env.example`, toolkit registry, or Modal/RunPod behavior. Deferred cleanups stay deferred ("`input=None` 的云端边界、quote 的 `/`、BAT LF、CI 矩阵化、报告文字、P5 成本/registry、依赖升级和广泛重构").
- **Process:** current branch is `feat/local-comfyui-provider-p2` (already checked out; no fork/branch steps). Commit prefixes `DOCS:` / `FEAT:` / `FIX:`. Acceptance artifacts go under `scratch/local-comfyui/p2/` (gitignored). "不得把只有 fake 测试通过、未跑三项真实 clone、未跑最终 health 或 BAT 启动失败的状态记为 P2 完成。"

## File Structure

| File | Responsibility |
|---|---|
| `local-gateway/local_gateway/comfyui.py` (modify) | Timeout→code classification, streaming bounded `view(max_bytes=)`, poll-sleep truncation, pure object_info helpers `input_values` / `input_names` |
| `local-gateway/local_gateway/health.py` (modify) | Use shared `input_values`; `comfyui_request_timeout` error action; non-empty directory checks; `dependencies` block + ready gating; skeleton-list factory |
| `local-gateway/local_gateway/registry.py` (modify) | Clone-only tts entry: `AILab_Qwen3TTSVoiceClone_Advanced`, non-empty Base/Tokenizer dirs, `modes`/`output_formats`/`max_batch_items`/`deadline_chunk_seconds` |
| `local-gateway/local_gateway/workflows/tts_voice_clone.json` (create) | Original API prompt template: `LoadAudio` → `AILab_Qwen3TTSVoiceClone_Advanced` → `SaveAudioAdvanced`, fixed values per spec §6.1 |
| `local-gateway/local_gateway/tts_workflow.py` (create) | Template load/deep-copy, `build_clone_prompt` whitelisted patching, `locate_history_audio`; pure, no network/fs writes |
| `local-gateway/local_gateway/audio.py` (create) | `ViewBudget`, bounded history download, ffprobe duration, FLAC→WAV `pcm_s16le` transcode, `finalize_output` |
| `local-gateway/local_gateway/tts.py` (create) | `parse_request`, `preflight`, `deadline_seconds`, `TTSRouteError` + code→status table, `generate_clone` (lock, upload-once, ordered items, no partial results) |
| `local-gateway/local_gateway/server.py` (modify) | Register `POST /tts/generate`, app-state `tts_lock`, JSON error/success responses only |
| `local-gateway/tests/test_comfyui.py` (modify) | New client tests: timeout classes, bounded view, sleep truncation, schema helpers |
| `local-gateway/tests/test_health.py` (modify) | Registry shape for clone entry, non-empty dirs, dependencies, fallback factory, object_info call-count |
| `local-gateway/tests/test_tts_workflow.py` (create) | Template shape, patching rules, history audio location |
| `local-gateway/tests/test_audio.py` (create) | Budget, bounded download, real mp3/flac pipelines, timeouts, error truncation |
| `local-gateway/tests/test_tts.py` (create) | Validation matrix, preflight, orchestration, deadline, lock, route HTTP mapping |
| `tools/qwen3_tts.py` (modify) | `--model`, `--cloud local`, clone-only local guards, base64 ref/result path, setup guard, doc defaults |
| `tools/voiceover.py` (modify) | `--cloud local`, `--qwen-model`, `--timeout` (300), local clone guards incl. brand design, passthrough on all qwen3 paths |
| `tests/python/test_qwen3_tts_local.py` (create) | Root-domain qwen3_tts local tests: guards, payload, base64-only results, modal unchanged, setup exit 2 |
| `tests/python/test_voiceover_local.py` (create) | Root-domain voiceover local tests: guard helper, model/timeout passthrough, argparse |
| `tests/python/test_cloud_gpu_local.py` (modify) | Message-level assertions for non-JSON / non-dict transport errors (spec §13.3) |
| `docs/local-comfyui-provider-p0-p1-design.md` (modify) | §4.3 adds `git status --short --branch --untracked-files=all`; §11 BAT `"%~dp0"` → `"%~dp0."` |
| `docs/local-comfyui-provider-roadmap.md` (modify) | P2 section rewritten clone-only; P2 checkboxes ticked only in Task 9 |
| `local-gateway/README.md` (modify) | TTS route usage, modes=(clone,), ffmpeg/ffprobe requirement, `LOCAL_TTS_ENDPOINT_URL` |

CI workflows need no change: `python-tests.yml` already covers `local-gateway/**` and `tests/python/**`, and `lint-python.yml` already lints `local-gateway/`.

## Execution conventions

- Repository root: `E:\GitHub\claude-code-video-toolkit`. Shell: Git Bash. Every Python/test invocation goes through uv.
- Root single-file test: `TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py -v`
- Gateway single-file test: `TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v`
- Full regression (Task 9): the four commands from spec §14.3, verbatim.
- Tasks run in order on `feat/local-comfyui-provider-p2`. Never use pytest. Gateway tests never read the real `local-gateway/.env`; root tests never read the real `.env`.
- When a step says "replace the block starting `<snippet>`", anchor on the snippet text, not line numbers (earlier tasks shift lines).

---

### Task 1: Non-blocking cleanup (spec §13 items 1, 2, 3, 7) and the qwen3_tts local setup guard

**Files:**
- Modify: `tests/python/test_cloud_gpu_local.py` (two test methods)
- Modify: `tools/qwen3_tts.py` (docstring defaults, `--cloud` choices, `--setup` local guard)
- Create: `tests/python/test_qwen3_tts_local.py`
- Modify: `docs/local-comfyui-provider-p0-p1-design.md` (§4.3 command list, §11 BAT snippet)

**Interfaces:**
- Consumes: existing `qwen3_tts.main()` / `parse_args()`; existing `_call_local` error strings (`"Local gateway returned a non-JSON body: ..."`, `"Local gateway returned JSON that is not an object: ..."`).
- Produces: `parse_args()` accepts `--cloud local`; `main()` exits 2 on `--setup --cloud local` before touching RunPod/Modal setup; `tests/python/test_qwen3_tts_local.py` exists with a module header that later tasks append test classes to.

- [ ] **Step 1: Write the failing setup-guard test and the transport message-level assertions**

Create `tests/python/test_qwen3_tts_local.py` with exactly:

```python
"""qwen3_tts local provider branch: guards, payload and result handling.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py -v

Every cloud call is faked. Nothing here contacts 127.0.0.1 or any other host.
"""
from __future__ import annotations

import base64
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402


class LocalSetupGuardTests(unittest.TestCase):
    def test_setup_cloud_local_exits_2_with_hint_and_no_cloud_setup(self):
        argv = ["qwen3_tts.py", "--setup", "--cloud", "local"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(
                    qwen3_tts, "setup_runpod",
                    side_effect=AssertionError("setup_runpod must not run")) as runpod, \
                mock.patch.object(
                    qwen3_tts, "setup_modal",
                    side_effect=AssertionError("setup_modal must not run")), \
                mock.patch("cloud_gpu.ProgressReporter"), \
                mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                qwen3_tts.main()
        self.assertEqual(ctx.exception.code, 2)
        runpod.assert_not_called()
        text = err.getvalue()
        self.assertIn("start_gateway.bat", text)
        self.assertIn("LOCAL_TTS_ENDPOINT_URL", text)
        self.assertIn("127.0.0.1:8123", text)


if __name__ == "__main__":
    unittest.main()
```

Then in `tests/python/test_cloud_gpu_local.py`, replace these two methods:

```python
    def test_non_json_response_returns_error(self):
        body = FakeResponse(status_code=200, text="<html>oops</html>", json_error=True)
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("error", result)

    def test_non_dict_json_returns_error(self):
        body = FakeResponse(status_code=200, json_data=[1, 2, 3], text="[1, 2, 3]")
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("error", result)
```

with message-level assertions (spec §13.3):

```python
    def test_non_json_response_error_mentions_body_kind_and_content(self):
        body = FakeResponse(status_code=200, text="<html>oops</html>", json_error=True)
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("non-JSON body", result["error"])
        self.assertIn("<html>oops</html>", result["error"])

    def test_non_dict_json_error_mentions_body_kind_and_content(self):
        body = FakeResponse(status_code=200, json_data=[1, 2, 3], text="[1, 2, 3]")
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("not an object", result["error"])
        self.assertIn("[1, 2, 3]", result["error"])
```

- [ ] **Step 2: Run the tests to verify the new guard test fails**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py -v
```

Expected: FAIL: the test fails on the stderr assertions: without the guard, `--cloud local` is an invalid argparse choice (argparse also exits 2, but its message says "invalid choice" and never mentions `LOCAL_TTS_ENDPOINT_URL` or `start_gateway.bat`). Also run:

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v
```

Expected: PASS (this edit only strengthens already-passing assertions; test-only hardening has no red phase; the strengthened messages already exist in `_call_local`).

- [ ] **Step 3: Implement the minimal changes in `tools/qwen3_tts.py`**

Three edits:

(a) Module docstring, fix the provider default claims. Replace:

```python
Cloud providers: RunPod (default), Modal.
```

with:

```python
Cloud providers: Modal (default), RunPod, or the local gateway (--cloud local; voice clone only).
```

Replace the usage line:

```python
    # Built-in speaker (RunPod, default)
    uv run tools/qwen3_tts.py --text "Hello world" --speaker Ryan --output hello.mp3
```

with:

```python
    # Built-in speaker (Modal, default)
    uv run tools/qwen3_tts.py --text "Hello world" --speaker Ryan --output hello.mp3
```

and in the argparse epilog replace:

```python
  # Built-in speaker (RunPod, default)
  uv run tools/qwen3_tts.py --text "Hello world" --speaker Ryan --output hello.mp3
```

with:

```python
  # Built-in speaker (Modal, default)
  uv run tools/qwen3_tts.py --text "Hello world" --speaker Ryan --output hello.mp3

  # Local gateway voice clone (requires local ComfyUI + gateway)
  uv run tools/qwen3_tts.py --cloud local --text "Hello" --ref-audio ref.mp3 --ref-text "transcript" --output cloned.mp3
```

(b) `--cloud` argument. Replace:

```python
    parser.add_argument(
        "--cloud",
        type=str,
        default="modal",
        choices=["runpod", "modal"],
        help="Cloud GPU provider (default: modal)",
    )
```

with:

```python
    parser.add_argument(
        "--cloud",
        type=str,
        default="modal",
        choices=["runpod", "modal", "local"],
        help="Cloud GPU provider (default: modal; local = loopback gateway, voice clone only)",
    )
```

(c) `--setup` guard. In `main()`, replace:

```python
    # Handle --setup
    if args.setup:
        if args.cloud == "modal":
```

with:

```python
    # Handle --setup
    if args.setup:
        if args.cloud == "local":
            print(
                "Setup: --cloud local needs no endpoint setup.\n"
                "  1. Start ComfyUI on 127.0.0.1:9188\n"
                "  2. Copy local-gateway/.env.example to local-gateway/.env and set COMFYUI_MODELS_ROOT\n"
                "  3. Run local-gateway/start_gateway.bat (binds 127.0.0.1:8123)\n"
                "  4. Set LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate in .env",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.cloud == "modal":
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py tests/python/test_cloud_gpu_local.py -v
```

Expected: PASS, all tests.

- [ ] **Step 5: Apply the two P0/P1 document cleanups (spec §13.1, §13.2)**

In `docs/local-comfyui-provider-p0-p1-design.md` §4.3, replace the acceptance block:

```bash
set -euo pipefail
test "$(git remote get-url origin)" = "https://github.com/kenyonxu/claude-code-video-toolkit.git"
test "$(git remote get-url upstream)" = "https://github.com/digitalsamba/claude-code-video-toolkit.git"
test "$(git branch --show-current)" = "feat/local-comfyui-provider"
uv run tools/copy_lint.py \
  docs/local-comfyui-provider-plan.md \
  docs/local-comfyui-provider-roadmap.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md
```

with (adds the status check; keeps exactly the four documents):

```bash
set -euo pipefail
test "$(git remote get-url origin)" = "https://github.com/kenyonxu/claude-code-video-toolkit.git"
test "$(git remote get-url upstream)" = "https://github.com/digitalsamba/claude-code-video-toolkit.git"
test "$(git branch --show-current)" = "feat/local-comfyui-provider"
uv run tools/copy_lint.py \
  docs/local-comfyui-provider-plan.md \
  docs/local-comfyui-provider-roadmap.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md
git status --short --branch --untracked-files=all
```

and one line after the block (keeping the existing sentence structure, no new em dashes):

```text
`git status` 输出必须只显示 `## feat/local-comfyui-provider...` 与已提交文件，`--untracked-files=all` 确认没有漏提交的验收产物。
```

Also in `docs/local-comfyui-provider-p0-p1-design.md` §4.1, replace the preflight status command `git status --short --branch` with `git status --short --branch --untracked-files=all`, so the preflight exposes every intended document rather than a collapsed directory entry.

In the same file §11, replace the BAT snippet:

```bat
uv run --directory "%~dp0" uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123
```

with the verified form used by the actual script:

```bat
uv run --directory "%~dp0." uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123
```

Verify lint:

```bash
uv run tools/copy_lint.py docs/local-comfyui-provider-p0-p1-design.md
```

Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add tools/qwen3_tts.py tests/python/test_qwen3_tts_local.py tests/python/test_cloud_gpu_local.py
git commit -m "FIX: guard qwen3_tts local setup and assert transport error messages"
git add docs/local-comfyui-provider-p0-p1-design.md docs/superpowers/plans/2026-11-17-local-comfyui-p2-voice-clone.md
git commit -m "DOCS: align P0 cleanup and add P2 voice clone implementation plan"
```

---

### Task 2: ComfyUI client hardening: timeout classification, bounded view, poll-sleep truncation, schema helpers

**Files:**
- Modify: `local-gateway/local_gateway/comfyui.py`
- Modify: `local-gateway/local_gateway/health.py` (import the shared helper; add `comfyui_request_timeout` action)
- Test: `local-gateway/tests/test_comfyui.py`, `local-gateway/tests/test_health.py`

**Interfaces:**
- Consumes: existing `ComfyUIClient._request` / `view` / `wait`; existing health `_comfyui_error`.
- Produces (used by Tasks 3, 4, 5):
  - `comfyui.input_values(meta: dict, input_name: str) -> list | None` (module-level, pure)
  - `comfyui.input_names(meta: dict) -> set[str]` (module-level, pure)
  - `ComfyUIClient.view(filename: str, subfolder: str = "", file_type: str = "output", max_bytes: int = DEFAULT_VIEW_MAX_BYTES) -> bytes`
  - `comfyui.DEFAULT_VIEW_MAX_BYTES = 64 * 1024 * 1024`, `comfyui.VIEW_CHUNK_BYTES = 256 * 1024`
  - `ComfyUIError.code` values now also include `"comfyui_request_timeout"` and `"generation_failed"` (from view overflow)
  - `wait()` never sleeps past its deadline.

- [ ] **Step 1: Write the failing tests (test_comfyui.py)**

Append to `local-gateway/tests/test_comfyui.py` (inside the file, after `HttpClientTests`; extend `FakeResponse` first). Extend `FakeResponse.__init__` signature by adding two optional parameters and two methods. Replace:

```python
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text="",
                 json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text
        self._json_error = json_error
```

with:

```python
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
```

Then append the new test classes:

```python
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
```

Update the import block at the top of the file to:

```python
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
```

Two existing tests feed `view()` and must switch from `content=` to `chunks=` now that view streams. In `test_view_sends_three_query_params_and_returns_bytes` replace `s.get_response = FakeResponse(content=b"PNGDATA")` with `s.get_response = FakeResponse(chunks=[b"PNGDATA"])`, and in `test_view_defaults_subfolder_empty_and_type_output` replace the single `make_client(s).view("x.png")` line with:

```python
        s.get_response = FakeResponse(chunks=[b"P"])
        make_client(s).view("x.png")
```

Everything else in those two tests stays unchanged.

Append to `local-gateway/tests/test_health.py`, inside `HealthAggregationTests`:

```python
    def test_request_timeout_error_action_does_not_say_start_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(
                exc=ComfyUIError("read timed out", code="comfyui_request_timeout"))
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("comfyui_request_timeout", codes)
        error = next(e for e in payload["errors"]
                     if e["code"] == "comfyui_request_timeout")
        self.assertNotIn("Start ComfyUI", error["action"])
        self.assertIn("timeout", error["message"].lower())
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: FAIL: `ImportError: cannot import name 'input_values'`; the classification/view/sleep tests fail with wrong codes / `TypeError: view() got an unexpected keyword argument 'max_bytes'` / sleeps of 5.0 recorded.

- [ ] **Step 3: Implement the comfyui.py changes**

In `local-gateway/local_gateway/comfyui.py`:

(a) Module constants, after `DEFAULT_TIMEOUT = (2, 10)`:

```python
VIEW_CHUNK_BYTES = 256 * 1024
DEFAULT_VIEW_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB per /view response
```

(b) Replace `_request` entirely:

```python
    def _request(self, method: str, path: str, **kwargs):
        url = self.base_url + path
        try:
            if method == "GET":
                return self._session.get(url, timeout=self._timeout, **kwargs)
            return self._session.post(url, timeout=self._timeout, **kwargs)
        except requests.exceptions.Timeout as e:
            raise ComfyUIError(
                f"ComfyUI HTTP request timed out ({method} {path}): {e}",
                code="comfyui_request_timeout",
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise ComfyUIError(
                f"Cannot reach ComfyUI at {self.base_url}: {e}",
                code="comfyui_unreachable",
            ) from e
        except (requests.exceptions.InvalidURL,
                requests.exceptions.MissingSchema) as e:
            raise ComfyUIError(
                f"Invalid ComfyUI URL for {method} {path}: {e}",
                code="comfyui_error",
            ) from e
        except requests.exceptions.RequestException as e:
            raise ComfyUIError(
                f"ComfyUI request failed for {method} {path}: {e}",
                code="comfyui_error",
            ) from e
```

(c) Replace `view` entirely:

```python
    def view(self, filename: str, subfolder: str = "", file_type: str = "output",
             max_bytes: int = DEFAULT_VIEW_MAX_BYTES) -> bytes:
        """GET /view streamed in chunks and bounded by max_bytes.

        Reading stops (and the response is closed) as soon as the running
        total exceeds max_bytes, so an oversized or hostile output cannot
        make the gateway allocate unbounded memory. Overflow raises
        generation_failed per the P2 spec.
        """
        response = self._request(
            "GET", "/view", stream=True,
            params={"filename": filename, "subfolder": subfolder, "type": file_type},
        )
        if response.status_code != 200:
            response.close()
            raise ComfyUIError(
                f"ComfyUI returned HTTP {response.status_code} for GET /view:"
                f" {response.text[:200]}",
                code="comfyui_error",
            )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=VIEW_CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ComfyUIError(
                        f"ComfyUI /view output for {filename!r} exceeds the"
                        f" {max_bytes}-byte limit after {total} bytes.",
                        code="generation_failed",
                    )
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks)
```

(d) In `wait`, replace the last two lines of the loop:

```python
            if time.monotonic() >= deadline:
                raise ComfyUITimeout(prompt_id, deadline)
            time.sleep(poll_interval)
```

with:

```python
            if time.monotonic() >= deadline:
                raise ComfyUITimeout(prompt_id, deadline)
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
```

(e) Module-level helpers, appended after the `ComfyUITimeout` class and before `ComfyUIClient`:

```python
def input_values(meta: dict, input_name: str) -> list | None:
    """Combo choices for one node input, or None when it is not a combo.

    Understands the current dict-keyed object_info shape, the legacy list
    shape, and ComfyUI 0.45.0's COMFY_DYNAMICCOMBO_V3 shape, whose options
    are objects with a string `key` (flac/mp3/opus and their quality fields).
    """
    inputs = meta.get("input", {}) if isinstance(meta, dict) else {}
    for section in ("required", "optional"):
        fields = inputs.get(section)
        if isinstance(fields, dict):
            spec = fields.get(input_name)
            if (isinstance(spec, (list, tuple)) and spec
                    and isinstance(spec[0], list)):
                return spec[0]
            if (isinstance(spec, (list, tuple)) and len(spec) >= 2
                    and spec[0] == "COMFY_DYNAMICCOMBO_V3"
                    and isinstance(spec[1], dict)):
                options = spec[1].get("options")
                if isinstance(options, list):
                    return [option["key"] for option in options
                            if isinstance(option, dict)
                            and isinstance(option.get("key"), str)]
                return None
            if spec is not None:
                return None
        elif isinstance(fields, list):
            for spec in fields:
                if (isinstance(spec, (list, tuple)) and len(spec) >= 2
                        and spec[0] == input_name):
                    return spec[1] if isinstance(spec[1], list) else None
    return None


def input_names(meta: dict) -> set[str]:
    """Input names across required+optional, both object_info shapes."""
    names: set[str] = set()
    inputs = meta.get("input", {}) if isinstance(meta, dict) else {}
    for section in ("required", "optional"):
        fields = inputs.get(section)
        if isinstance(fields, dict):
            names.update(str(k) for k in fields)
        elif isinstance(fields, list):
            for entry in fields:
                if isinstance(entry, (list, tuple)) and entry:
                    names.add(str(entry[0]))
    return names
```

- [ ] **Step 4: Implement the health.py changes**

In `local-gateway/local_gateway/health.py`:

(a) Import the shared helper. Replace:

```python
from .comfyui import ComfyUIError, ComfyUIClient
```

with:

```python
from .comfyui import ComfyUIError, ComfyUIClient, input_values as _input_values
```

(b) Delete the local `_input_values` function (the whole block from `def _input_values(meta: dict, input_name: str) -> list | None:` through its `return None`); the imported alias keeps every call site working unchanged.

(c) `_comfyui_error` gains the timeout branch. Replace:

```python
def _comfyui_error(e: ComfyUIError) -> dict:
    if e.code == "comfyui_unreachable":
```

with:

```python
def _comfyui_error(e: ComfyUIError) -> dict:
    if e.code == "comfyui_request_timeout":
        return _err(
            "comfyui_request_timeout", "comfyui",
            f"ComfyUI was too slow to answer: {e.message}",
            "A single ComfyUI HTTP request exceeded the gateway's short"
            " timeout. Check ComfyUI load and its console log; no running"
            " ComfyUI job was cancelled.",
        )
    if e.code == "comfyui_unreachable":
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: PASS, all gateway tests (old and new).

- [ ] **Step 6: Commit**

```bash
git add local-gateway/local_gateway/comfyui.py local-gateway/local_gateway/health.py local-gateway/tests/test_comfyui.py local-gateway/tests/test_health.py
git commit -m "FEAT: classify client timeouts, bound view downloads, truncate poll sleep"
```

---

### Task 3: Clone-only registry entry, workflow template and tts_workflow module

**Files:**
- Modify: `local-gateway/local_gateway/registry.py`
- Create: `local-gateway/local_gateway/workflows/tts_voice_clone.json`
- Create: `local-gateway/local_gateway/tts_workflow.py`
- Modify: `local-gateway/local_gateway/health.py` (non-empty directory checks)
- Test: `local-gateway/tests/test_tts_workflow.py` (create), `local-gateway/tests/test_health.py` (modify)

**Interfaces:**
- Consumes: Task 2's `input_values` (via health). Existing `ModelEntry`, `RequiredOption`.
- Produces (used by Tasks 5, 8, 9):
  - `registry.ModelEntry` new fields: `required_nonempty_directories: tuple[str, ...] = ()`, `modes: tuple[str, ...] = ()`, `output_formats: tuple[str, ...] = ()`, `max_batch_items: int = 0`, `deadline_chunk_seconds: int = 0`
  - tts entry: `required_nodes=("AILab_Qwen3TTSVoiceClone_Advanced", "LoadAudio", "SaveAudioAdvanced")`, `required_nonempty_directories=("TTS/Qwen3-TTS-12Hz-1.7B-Base", "TTS/Qwen3-TTS-Tokenizer-12Hz")`, `required_options=(RequiredOption("AILab_Qwen3TTSVoiceClone_Advanced", "model_size", ("1.7B",)), RequiredOption("SaveAudioAdvanced", "format", ("mp3", "flac")))`, `modes=("clone",)`, `output_formats=("mp3", "wav")`, `max_batch_items=8`, `deadline_chunk_seconds=270`
  - `tts_workflow.LOAD_AUDIO_NODE = "1"`, `TTS_NODE = "2"`, `SAVE_NODE = "3"`, `OUTPUT_FORMATS = ("mp3", "wav")`, `FIXED_TTS_INPUTS: dict`
  - `tts_workflow.load_template() -> dict`
  - `tts_workflow.build_clone_prompt(*, text: str, ref_text: str, reference_audio: str, language: str, output_format: str, output_prefix: str, temperature: float | None = None, top_p: float | None = None) -> dict`
  - `tts_workflow.locate_history_audio(entry: dict, save_node_id: str = SAVE_NODE) -> dict` (raises `ValueError` on any malformed entry)
  - health reports a missing/empty non-empty-required directory through `missing_files` and `model_file_missing` exactly like a missing file.

- [ ] **Step 1: Write the failing tests**

Create `local-gateway/tests/test_tts_workflow.py`:

```python
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
```

Update `local-gateway/tests/test_health.py`: replace the tts registry test:

```python
    def test_tts_entry_directory_nodes_and_options(self):
        entry = registry.model_by_key("qwen3-tts-1.7B")
        self.assertEqual(entry.required_directories, ("TTS/Qwen3-TTS/",))
        self.assertEqual(entry.required_nodes, (
            "AILab_Qwen3TTSCustomVoice",
            "AILab_Qwen3TTSVoiceClone",
            "AILab_Qwen3TTSVoiceDesign",
            "LoadAudio",
            "SaveAudioAdvanced",
        ))
        for node in ("AILab_Qwen3TTSCustomVoice", "AILab_Qwen3TTSVoiceClone",
                     "AILab_Qwen3TTSVoiceDesign"):
            self.assertIn(RequiredOption(node, "model_size", ("1.7B",)),
                          entry.required_options)
        self.assertIn(RequiredOption("AILab_Qwen3TTSCustomVoice", "speaker", ("Ryan",)),
                      entry.required_options)
```

with:

```python
    def test_tts_entry_is_clone_only(self):
        entry = registry.model_by_key("qwen3-tts-1.7B")
        self.assertEqual(entry.required_directories, ())
        self.assertEqual(entry.required_nonempty_directories, (
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ))
        self.assertEqual(entry.required_nodes, (
            "AILab_Qwen3TTSVoiceClone_Advanced",
            "LoadAudio",
            "SaveAudioAdvanced",
        ))
        self.assertEqual(entry.required_options, (
            RequiredOption("AILab_Qwen3TTSVoiceClone_Advanced", "model_size",
                           ("1.7B",)),
            RequiredOption("SaveAudioAdvanced", "format", ("mp3", "flac")),
        ))
        self.assertEqual(entry.modes, ("clone",))
        self.assertEqual(entry.output_formats, ("mp3", "wav"))
        self.assertEqual(entry.max_batch_items, 8)
        self.assertEqual(entry.deadline_chunk_seconds, 270)
```

Update `build_model_tree` in the same file: replace:

```python
def build_model_tree(root, *, include=("tts", "image", "video"),
                     optional_audio_vae=False):
    if "tts" in include:
        (root / "TTS" / "Qwen3-TTS").mkdir(parents=True, exist_ok=True)
```

with:

```python
def build_model_tree(root, *, include=("tts", "image", "video"),
                     optional_audio_vae=False, empty_tts_dirs=False):
    if "tts" in include:
        for rel in ("TTS/Qwen3-TTS-12Hz-1.7B-Base",
                    "TTS/Qwen3-TTS-Tokenizer-12Hz"):
            d = root / rel
            d.mkdir(parents=True, exist_ok=True)
            if not empty_tts_dirs:
                (d / "weights.safetensors").write_bytes(b"x")
```

Replace the missing-directory test:

```python
    def test_missing_tts_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("image", "video"))
            client = FakeClient(object_info=object_info_fixture())
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["files_ready"])
        self.assertEqual(tts["missing_files"], ["TTS/Qwen3-TTS/"])
```

with:

```python
    def test_missing_tts_weight_directories_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("image", "video"))
            client = FakeClient(object_info=object_info_fixture())
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["files_ready"])
        self.assertEqual(tts["missing_files"], [
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ])

    def test_empty_tts_weight_directory_is_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, empty_tts_dirs=True)
            client = FakeClient(object_info=object_info_fixture())
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["files_ready"])
        self.assertEqual(tts["missing_files"], [
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ])
```

Update `object_info_fixture` in the same file: replace the three AILab entries:

```python
        "AILab_Qwen3TTSCustomVoice": {"input": {"required": [
            ["model_size", ["0.6B", "1.7B"]],
            ["speaker", ["Ryan", "Aiden"]],
        ]}},
        "AILab_Qwen3TTSVoiceClone": {"input": {"required": [
            ["model_size", ["0.6B", "1.7B"]],
        ]}},
        "AILab_Qwen3TTSVoiceDesign": {"input": {"required": [
            ["model_size", ["1.7B"]],
        ]}},
```

with the clone-only Advanced node:

```python
        "AILab_Qwen3TTSVoiceClone_Advanced": {"input": {"required": [
            ["model_size", ["0.6B", "1.7B"]],
            ["language", ["Auto", "English", "Chinese"]],
            ["target_text", "STRING"],
            ["reference_text", "STRING"],
            ["reference_audio", "AUDIO"],
        ]}},
```

and replace the enum test `test_tts_model_size_and_speaker_enums_are_checked` with:

```python
    def test_tts_model_size_enum_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture(
                AILab_Qwen3TTSVoiceClone_Advanced={"input": {"required": [
                    ["model_size", ["0.6B"]],
                    ["language", ["Auto", "English", "Chinese"]],
                    ["target_text", "STRING"],
                    ["reference_text", "STRING"],
                    ["reference_audio", "AUDIO"],
                ]}},
            )
            client = FakeClient(object_info=meta)
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["nodes_ready"])
        self.assertEqual(tts["missing_loader_values"], [
            "AILab_Qwen3TTSVoiceClone_Advanced.model_size=1.7B",
        ])
```

Also replace the `SaveAudioAdvanced` fixture entry with the actual ComfyUI 0.45.0 DynamicCombo shape (the fixture's runtime conversion will preserve it):

```python
        "SaveAudioAdvanced": {"input": {"required": [
            ["format", "COMFY_DYNAMICCOMBO_V3", {"options": [
                {"key": "flac", "inputs": {"required": {}}},
                {"key": "mp3", "inputs": {"required": {}}},
                {"key": "opus", "inputs": {"required": {}}},
            ]}],
        ]}},
```

The required `mp3` and `flac` option keys then pass through the same `input_values` helper used by the real route.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts_workflow.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: test_tts_workflow fails with `ModuleNotFoundError: No module named 'local_gateway.tts_workflow'`; test_health fails on the rewritten registry assertions (`AttributeError: 'ModelEntry' object has no attribute 'required_nonempty_directories'` and wrong nodes).

- [ ] **Step 3: Implement registry.py**

In `local-gateway/local_gateway/registry.py`, extend `ModelEntry`: replace:

```python
@dataclass(frozen=True)
class ModelEntry:
    key: str
    capability: str
    required_files: tuple[str, ...] = ()
    required_directories: tuple[str, ...] = ()
    optional_files: tuple[str, ...] = ()
    required_nodes: tuple[str, ...] = ()
    required_options: tuple[RequiredOption, ...] = ()
```

with:

```python
@dataclass(frozen=True)
class ModelEntry:
    key: str
    capability: str
    required_files: tuple[str, ...] = ()
    required_directories: tuple[str, ...] = ()
    # Directories that must exist AND contain at least one entry (P2 clone
    # weights: an empty dir makes the AILab node fall back to
    # snapshot_download, which the gateway must not trigger).
    required_nonempty_directories: tuple[str, ...] = ()
    optional_files: tuple[str, ...] = ()
    required_nodes: tuple[str, ...] = ()
    required_options: tuple[RequiredOption, ...] = ()
    # Capability metadata (P2): routes and preflight read these instead of
    # hardcoding. CustomVoice/VoiceDesign never appear in modes.
    modes: tuple[str, ...] = ()
    output_formats: tuple[str, ...] = ()
    max_batch_items: int = 0
    deadline_chunk_seconds: int = 0
```

Replace the whole first `ModelEntry(...)` (the `qwen3-tts-1.7B` entry) with:

```python
    ModelEntry(
        key="qwen3-tts-1.7B",
        capability="tts",
        required_directories=(),
        required_nonempty_directories=(
            "TTS/Qwen3-TTS-12Hz-1.7B-Base",
            "TTS/Qwen3-TTS-Tokenizer-12Hz",
        ),
        required_nodes=(
            "AILab_Qwen3TTSVoiceClone_Advanced",
            "LoadAudio",
            "SaveAudioAdvanced",
        ),
        required_options=(
            RequiredOption("AILab_Qwen3TTSVoiceClone_Advanced", "model_size",
                           ("1.7B",)),
            RequiredOption("SaveAudioAdvanced", "format", ("mp3", "flac")),
        ),
        modes=("clone",),
        output_formats=("mp3", "wav"),
        max_batch_items=8,
        deadline_chunk_seconds=270,
    ),
```

- [ ] **Step 4: Create the workflow template**

Create `local-gateway/local_gateway/workflows/tts_voice_clone.json` (original work; only `class_type` and `inputs`; no third-party example content):

```json
{
  "1": {
    "class_type": "LoadAudio",
    "inputs": {
      "audio": "ccvt_clone_ref.mp3"
    }
  },
  "2": {
    "class_type": "AILab_Qwen3TTSVoiceClone_Advanced",
    "inputs": {
      "reference_audio": ["1", 0],
      "reference_text": "",
      "target_text": "",
      "language": "Auto",
      "model_size": "1.7B",
      "max_new_tokens": 2048,
      "top_k": 50,
      "repetition_penalty": 1.0,
      "do_sample": false,
      "attention": "auto",
      "device": "auto",
      "precision": "bf16",
      "unload_models": true,
      "seed": -1,
      "x_vector_only": false
    }
  },
  "3": {
    "class_type": "SaveAudioAdvanced",
    "inputs": {
      "audio": ["2", 0],
      "filename_prefix": "ccvt/tts_clone",
      "format": "flac"
    }
  }
}
```

- [ ] **Step 5: Create tts_workflow.py**

Create `local-gateway/local_gateway/tts_workflow.py`:

```python
"""Original VoiceClone Advanced prompt template, patching and history lookup.

Pure data functions: no network, no filesystem writes. The template lives in
workflows/tts_voice_clone.json next to this file and is re-loaded (deep, via
json round-trip on every call) so callers can never mutate shared state.
Node ids are stable; build_clone_prompt only touches whitelisted inputs.
"""
from __future__ import annotations

import json
from pathlib import Path

LOAD_AUDIO_NODE = "1"
TTS_NODE = "2"
SAVE_NODE = "3"

TEMPLATE_PATH = Path(__file__).resolve().parent / "workflows" / "tts_voice_clone.json"
OUTPUT_FORMATS = ("mp3", "wav")

# Fixed VoiceClone Advanced values (spec section 6.1). Sampling params are
# deliberately absent: they are patched only when the caller provides them.
FIXED_TTS_INPUTS = {
    "model_size": "1.7B",
    "max_new_tokens": 2048,
    "top_k": 50,
    "repetition_penalty": 1.0,
    "attention": "auto",
    "device": "auto",
    "precision": "bf16",
    "unload_models": True,
    "seed": -1,
    "x_vector_only": False,
}


def load_template() -> dict:
    """Return a fresh copy of the API prompt template."""
    with TEMPLATE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def build_clone_prompt(*, text: str, ref_text: str, reference_audio: str,
                       language: str, output_format: str, output_prefix: str,
                       temperature: float | None = None,
                       top_p: float | None = None) -> dict:
    """Patch one clone prompt; only whitelisted inputs are modified.

    SaveAudioAdvanced.format stays a flat string: mp3 adds the separate
    format.quality="V0" input, flac (the WAV intermediate) never writes a
    quality key. do_sample flips to true only when a sampling param is
    explicitly provided, and only provided values are patched in.
    """
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"unsupported output_format: {output_format!r}")
    if not output_prefix.strip():
        raise ValueError("output_prefix must be a non-empty unique per-item prefix")
    prompt = load_template()
    prompt[LOAD_AUDIO_NODE]["inputs"]["audio"] = reference_audio
    tts_inputs = prompt[TTS_NODE]["inputs"]
    tts_inputs["target_text"] = text
    tts_inputs["reference_text"] = ref_text
    tts_inputs["language"] = language
    if temperature is not None or top_p is not None:
        tts_inputs["do_sample"] = True
        if temperature is not None:
            tts_inputs["temperature"] = temperature
        if top_p is not None:
            tts_inputs["top_p"] = top_p
    save_inputs = prompt[SAVE_NODE]["inputs"]
    save_inputs["filename_prefix"] = output_prefix
    if output_format == "mp3":
        save_inputs["format"] = "mp3"
        save_inputs["format.quality"] = "V0"
    else:
        save_inputs["format"] = "flac"
        save_inputs.pop("format.quality", None)
    return prompt


def locate_history_audio(entry: dict, save_node_id: str = SAVE_NODE) -> dict:
    """Return the single audio metadata dict from a finished history entry.

    Reads entry["outputs"][save_node_id]["audio"][0]. The dict must carry
    string filename/subfolder/type keys (subfolder may be empty); anything
    else (missing outputs, empty list, several entries, non-string keys)
    raises ValueError, which the route maps to generation_failed.
    """
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    node_out = outputs.get(save_node_id) if isinstance(outputs, dict) else None
    audio = node_out.get("audio") if isinstance(node_out, dict) else None
    if not isinstance(audio, list) or len(audio) != 1:
        raise ValueError(
            f"expected exactly one audio entry on output node {save_node_id!r},"
            f" got {audio!r}")
    meta = audio[0]
    if not isinstance(meta, dict):
        raise ValueError(f"audio metadata is not an object: {meta!r}")
    for key in ("filename", "subfolder", "type"):
        if not isinstance(meta.get(key), str):
            raise ValueError(f"audio metadata misses string {key!r}: {meta!r}")
    if not meta["filename"] or not meta["type"]:
        raise ValueError(f"audio metadata has empty filename/type: {meta!r}")
    return meta
```

- [ ] **Step 6: Implement the health non-empty directory check**

In `local-gateway/local_gateway/health.py`, replace:

```python
    for rel in entry.required_directories:
        if not (root / rel).is_dir():
            ok = False
            missing.append(rel)
```

with:

```python
    for rel in entry.required_directories:
        if not (root / rel).is_dir():
            ok = False
            missing.append(rel)
    for rel in entry.required_nonempty_directories:
        directory = root / rel
        try:
            nonempty = directory.is_dir() and any(directory.iterdir())
        except OSError:
            nonempty = False
        if not nonempty:
            ok = False
            missing.append(rel)
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: PASS, all gateway tests.

- [ ] **Step 8: Commit**

```bash
git add local-gateway/local_gateway/registry.py local-gateway/local_gateway/workflows/tts_voice_clone.json local-gateway/local_gateway/tts_workflow.py local-gateway/local_gateway/health.py local-gateway/tests/test_tts_workflow.py local-gateway/tests/test_health.py
git commit -m "FEAT: clone workflow template and clone-only registry entry"
```

---

### Task 4: audio pipeline: bounded download budget, ffprobe, FLAC-to-WAV transcode

**Files:**
- Create: `local-gateway/local_gateway/audio.py`
- Test: `local-gateway/tests/test_audio.py` (create)

**Interfaces:**
- Consumes: Task 2 `ComfyUIClient.view(..., max_bytes=...)`, `ComfyUIError` with codes `generation_failed`/`comfyui_timeout`; Task 3 `tts_workflow.locate_history_audio` / `SAVE_NODE`.
- Produces (used by Task 5):
  - `audio.VIEW_MAX_BYTES = 64 * 1024 * 1024`, `audio.REQUEST_BUDGET_BYTES = 128 * 1024 * 1024`
  - `audio.ViewBudget(limit: int = REQUEST_BUDGET_BYTES)` with `.remaining: int`, `.max_bytes_for_next() -> int`, `.spend(n: int) -> None`
  - `audio.download_history_audio(client, history_entry: dict, save_node_id: str, budget: ViewBudget, deadline: float) -> bytes`
  - `audio.ffprobe_duration(path: Path, deadline: float) -> float`
  - `audio.flac_to_wav(flac_path: Path, wav_path: Path, deadline: float) -> None`
  - `audio.finalize_output(client, history_entry: dict, output_format: str, workdir: Path, stem: str, budget: ViewBudget, deadline: float) -> tuple[bytes, float]`
  - All failures raise `ComfyUIError` with code `generation_failed` (processing) or `comfyui_timeout` (deadline).

- [ ] **Step 1: Write the failing tests**

Create `local-gateway/tests/test_audio.py`:

```python
"""audio: bounded downloads, ffprobe validation and FLAC-to-WAV transcode.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v

ffmpeg/ffprobe run as real local subprocesses on tiny generated clips (no
network). ComfyUI clients are fakes.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import audio, tts_workflow  # noqa: E402
from local_gateway.comfyui import ComfyUIError  # noqa: E402

FAR_DEADLINE = 30.0


def make_clip(path: Path, codec: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
         "-c:a", codec, str(path)],
        check=True, capture_output=True, shell=False)


class ClipMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.mp3_path = root / "clip.mp3"
        cls.flac_path = root / "clip.flac"
        make_clip(cls.mp3_path, "libmp3lame")
        make_clip(cls.flac_path, "flac")
        cls.mp3_bytes = cls.mp3_path.read_bytes()
        cls.flac_bytes = cls.flac_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def deadline(self):
        return time.monotonic() + FAR_DEADLINE


class FakeViewClient:
    def __init__(self, content: bytes):
        self.content = content
        self.view_calls = []

    def view(self, filename, subfolder="", file_type="output",
             max_bytes=audio.VIEW_MAX_BYTES):
        self.view_calls.append((filename, subfolder, file_type, max_bytes))
        return self.content


def history_entry():
    return {"outputs": {tts_workflow.SAVE_NODE: {"audio": [
        {"filename": "clone_00001_.mp3", "subfolder": "ccvt",
         "type": "output"}]}}}


class ViewBudgetTests(unittest.TestCase):
    def test_limits_are_the_spec_values(self):
        self.assertEqual(audio.VIEW_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(audio.REQUEST_BUDGET_BYTES, 128 * 1024 * 1024)

    def test_max_bytes_is_min_of_file_limit_and_remaining(self):
        budget = audio.ViewBudget(limit=100)
        self.assertEqual(budget.max_bytes_for_next(), 100)
        budget.spend(40)
        self.assertEqual(budget.max_bytes_for_next(), 60)
        budget.spend(60)
        self.assertEqual(budget.max_bytes_for_next(), 0)

    def test_download_passes_the_bounded_max_bytes_and_spends(self):
        budget = audio.ViewBudget(limit=1000)
        client = FakeViewClient(b"x" * 30)
        data = audio.download_history_audio(client, history_entry(),
                                            tts_workflow.SAVE_NODE, budget,
                                            self.deadline())
        self.assertEqual(data, b"x" * 30)
        filename, subfolder, file_type, max_bytes = client.view_calls[0]
        self.assertEqual(filename, "clone_00001_.mp3")
        self.assertEqual(subfolder, "ccvt")
        self.assertEqual(file_type, "output")
        self.assertEqual(max_bytes, 1000)
        self.assertEqual(budget.remaining, 970)

    def test_download_with_exhausted_budget_requests_zero_max_bytes(self):
        # The overflow error itself is raised by the real streaming client
        # (see BoundedViewTests in test_comfyui.py); here we assert the
        # budget drives the max_bytes argument down to zero.
        budget = audio.ViewBudget(limit=10)
        budget.spend(10)
        client = FakeViewClient(b"abc")
        audio.download_history_audio(client, history_entry(),
                                     tts_workflow.SAVE_NODE, budget,
                                     self.deadline())
        self.assertEqual(client.view_calls[0][3], 0)

    def test_download_after_deadline_is_comfyui_timeout(self):
        budget = audio.ViewBudget()
        client = FakeViewClient(b"abc")
        with self.assertRaises(ComfyUIError) as ctx:
            audio.download_history_audio(client, history_entry(),
                                         tts_workflow.SAVE_NODE, budget,
                                         time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")

    def test_malformed_history_raises_value_error(self):
        budget = audio.ViewBudget()
        client = FakeViewClient(b"abc")
        with self.assertRaises(ValueError):
            audio.download_history_audio(client, {"outputs": {}},
                                         tts_workflow.SAVE_NODE, budget,
                                         self.deadline())


class FfprobeTests(ClipMixin, unittest.TestCase):
    def test_real_mp3_reports_positive_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(self.mp3_bytes)
            duration = audio.ffprobe_duration(path, self.deadline())
        self.assertGreater(duration, 0)

    def test_garbage_bytes_are_generation_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(b"not audio at all")
            with self.assertRaises(ComfyUIError) as ctx:
                audio.ffprobe_duration(path, self.deadline())
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_expired_deadline_is_comfyui_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(self.mp3_bytes)
            with self.assertRaises(ComfyUIError) as ctx:
                audio.ffprobe_duration(path, time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")

    def test_subprocess_timeout_is_comfyui_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mp3"
            path.write_bytes(self.mp3_bytes)
            with mock.patch.object(
                    subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("ffprobe", 1)):
                with self.assertRaises(ComfyUIError) as ctx:
                    audio.ffprobe_duration(path, self.deadline())
        self.assertEqual(ctx.exception.code, "comfyui_timeout")


class FlacToWavTests(ClipMixin, unittest.TestCase):
    def test_real_flac_becomes_pcm_s16le_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            flac = Path(tmp) / "in.flac"
            wav = Path(tmp) / "out.wav"
            flac.write_bytes(self.flac_bytes)
            audio.flac_to_wav(flac, wav, self.deadline())
            self.assertTrue(wav.is_file())
            data = wav.read_bytes()
        self.assertEqual(data[:4], b"RIFF")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", "-"],
            input=data, capture_output=True, shell=False)
        self.assertEqual(result.stdout.decode().strip(), "pcm_s16le")

    def test_garbage_flac_is_generation_failed_with_short_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            flac = Path(tmp) / "in.flac"
            wav = Path(tmp) / "out.wav"
            flac.write_bytes(b"junk" * 100)
            with self.assertRaises(ComfyUIError) as ctx:
                audio.flac_to_wav(flac, wav, self.deadline())
        self.assertEqual(ctx.exception.code, "generation_failed")
        self.assertLessEqual(len(ctx.exception.message), 400)

    def test_expired_deadline_is_comfyui_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            flac = Path(tmp) / "in.flac"
            wav = Path(tmp) / "out.wav"
            flac.write_bytes(self.flac_bytes)
            with self.assertRaises(ComfyUIError) as ctx:
                audio.flac_to_wav(flac, wav, time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")


class FinalizeOutputTests(ClipMixin, unittest.TestCase):
    def test_mp3_passthrough_returns_view_bytes_and_duration(self):
        client = FakeViewClient(self.mp3_bytes)
        budget = audio.ViewBudget()
        with tempfile.TemporaryDirectory() as tmp:
            data, duration = audio.finalize_output(
                client, history_entry(), "mp3", Path(tmp), "item_0",
                budget, self.deadline())
        self.assertEqual(data, self.mp3_bytes)
        self.assertGreater(duration, 0)
        self.assertEqual(budget.remaining,
                         audio.REQUEST_BUDGET_BYTES - len(self.mp3_bytes))

    def test_wav_transcodes_flac_and_reports_duration(self):
        client = FakeViewClient(self.flac_bytes)
        with tempfile.TemporaryDirectory() as tmp:
            data, duration = audio.finalize_output(
                client, history_entry(), "wav", Path(tmp), "item_0",
                audio.ViewBudget(), self.deadline())
        self.assertEqual(data[:4], b"RIFF")
        self.assertGreater(duration, 0)

    def test_wav_leaves_no_flac_or_wav_behind_in_workdir(self):
        client = FakeViewClient(self.flac_bytes)
        with tempfile.TemporaryDirectory() as tmp:
            audio.finalize_output(client, history_entry(), "wav", Path(tmp),
                                  "item_0", audio.ViewBudget(),
                                  self.deadline())
            leftovers = list(Path(tmp).iterdir())
        # The workdir is the caller's request temp dir; files inside are
        # cleaned by the caller's TemporaryDirectory. Here we only assert
        # the helper used the requested stem.
        self.assertTrue(all(p.name.startswith("item_0") for p in leftovers))


if __name__ == "__main__":
    unittest.main()
```


- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'local_gateway.audio'`.

- [ ] **Step 3: Implement audio.py**

Create `local-gateway/local_gateway/audio.py`:

```python
"""TTS audio pipeline: bounded view download, ffprobe validation, WAV transcode.

No TTS business parameters here (tts.py owns those). This module turns one
finished ComfyUI history entry into verified final bytes plus a duration:
MP3 passes through untouched, WAV comes from a FLAC intermediate transcoded
with an FFmpeg argument array (shell=False). Every stage consumes the
request deadline; every failure raises ComfyUIError with the spec's code.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import tts_workflow
from .comfyui import ComfyUIError

VIEW_MAX_BYTES = 64 * 1024 * 1024          # per /view file
REQUEST_BUDGET_BYTES = 128 * 1024 * 1024   # all outputs of one request
_STDERR_SNIPPET = 200


class ViewBudget:
    """Tracks the bytes still allowed for /view downloads in one request."""

    def __init__(self, limit: int = REQUEST_BUDGET_BYTES):
        self.remaining = limit

    def max_bytes_for_next(self) -> int:
        return min(VIEW_MAX_BYTES, max(0, self.remaining))

    def spend(self, n: int) -> None:
        self.remaining -= n


def _remaining(deadline: float, stage: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ComfyUIError(
            f"clone deadline reached before {stage}", code="comfyui_timeout")
    return left


def download_history_audio(client, history_entry: dict, save_node_id: str,
                           budget: ViewBudget, deadline: float) -> bytes:
    """Locate the audio metadata in the history entry and view() it bounded."""
    meta = tts_workflow.locate_history_audio(history_entry, save_node_id)
    _remaining(deadline, "audio download")
    data = client.view(meta["filename"], meta["subfolder"], meta["type"],
                       max_bytes=budget.max_bytes_for_next())
    budget.spend(len(data))
    return data


def ffprobe_duration(path: Path, deadline: float) -> float:
    """Run ffprobe on a file; require returncode 0 and duration > 0."""
    timeout = _remaining(deadline, "ffprobe")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ComfyUIError(
            f"ffprobe timed out after {timeout:.0f}s",
            code="comfyui_timeout") from e
    except OSError as e:
        raise ComfyUIError(
            f"ffprobe failed to run: {e}", code="generation_failed") from e
    if result.returncode != 0:
        raise ComfyUIError(
            f"ffprobe rejected the generated audio:"
            f" {result.stderr.strip()[:_STDERR_SNIPPET]}",
            code="generation_failed")
    try:
        duration = float(result.stdout.strip())
    except ValueError as e:
        raise ComfyUIError(
            f"ffprobe returned no parsable duration:"
            f" {result.stdout.strip()[:_STDERR_SNIPPET]!r}",
            code="generation_failed") from e
    if not duration > 0:
        raise ComfyUIError(
            f"ffprobe reported duration {duration!r}",
            code="generation_failed")
    return duration


def flac_to_wav(flac_path: Path, wav_path: Path, deadline: float) -> None:
    """Transcode FLAC bytes to pcm_s16le WAV with an argument array."""
    timeout = _remaining(deadline, "FFmpeg WAV transcode")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(flac_path), "-c:a", "pcm_s16le",
             str(wav_path)],
            capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ComfyUIError(
            f"FFmpeg WAV transcode timed out after {timeout:.0f}s",
            code="comfyui_timeout") from e
    except OSError as e:
        raise ComfyUIError(
            f"FFmpeg failed to run: {e}", code="generation_failed") from e
    if result.returncode != 0 or not wav_path.is_file():
        raise ComfyUIError(
            f"FFmpeg WAV transcode failed:"
            f" {result.stderr.strip()[:_STDERR_SNIPPET]}",
            code="generation_failed")


def finalize_output(client, history_entry: dict, output_format: str,
                    workdir: Path, stem: str, budget: ViewBudget,
                    deadline: float) -> tuple[bytes, float]:
    """Download, finalize and verify one item's audio; returns (bytes, duration)."""
    raw = download_history_audio(client, history_entry,
                                 tts_workflow.SAVE_NODE, budget, deadline)
    if output_format == "mp3":
        final_path = workdir / f"{stem}.mp3"
        final_path.write_bytes(raw)
    else:
        flac_path = workdir / f"{stem}.flac"
        wav_path = workdir / f"{stem}.wav"
        flac_path.write_bytes(raw)
        flac_to_wav(flac_path, wav_path, deadline)
        final_path = wav_path
    duration = ffprobe_duration(final_path, deadline)
    return final_path.read_bytes(), duration
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add local-gateway/local_gateway/audio.py local-gateway/tests/test_audio.py
git commit -m "FEAT: gateway audio pipeline with bounded view, ffprobe and WAV transcode"
```

---

### Task 5: tts orchestration module and POST /tts/generate route

**Files:**
- Create: `local-gateway/local_gateway/tts.py`
- Modify: `local-gateway/local_gateway/server.py`
- Test: `local-gateway/tests/test_tts.py` (create)

**Interfaces:**
- Consumes: Task 2 `ComfyUIError` codes + `input_values`/`input_names`; Task 3 registry fields, `tts_workflow.build_clone_prompt`; Task 4 `audio.ViewBudget`/`finalize_output`; existing `GatewaySettings`, `ComfyUIClient` factory shape.
- Produces:
  - `tts.ERROR_STATUS: dict[str, int]` (the §9 table)
  - `tts.TTSRouteError(message: str, code: str, details: dict | None = None)` with `.message`, `.code`, `.details`, `.http_status`
  - `tts.deadline_seconds(item_count: int) -> int` (= `270 * max(1, item_count // 3 + 1)` via the registry chunk)
  - `tts.parse_request(body) -> TTSRequest` (frozen dataclass with `texts`, `languages`, `output_format`, `model`, `ref_audio_bytes`, `ref_suffix`, `ref_text`, `temperature`, `top_p`)
  - `tts.preflight(settings: GatewaySettings, client_factory) -> Preflight` (`Preflight.language_choices: tuple[str, ...]`)
  - `tts.generate_clone(settings: GatewaySettings, client_factory, body: dict, lock: threading.Lock) -> dict`: success payload `{"success": True, "mode": "clone", "model": <key>, "processing_time_seconds": <float>, "outputs": [{"audio_base64": str, "duration_seconds": float}, ...]}` plus top-level `audio_base64`/`duration_seconds` mirrors for a single text; raises `TTSRouteError` otherwise.
  - `server.create_app()` sets `app.state.tts_lock` and exposes `POST /tts/generate` returning `{"success": False, "error": str, "code": str[, "details": ...]}` with the mapped status.

- [ ] **Step 1: Write the failing tests**

Create `local-gateway/tests/test_tts.py`:

```python
"""tts: request validation, preflight, orchestration and the FastAPI route.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v

ComfyUI clients, PATH tools and model trees are faked/temp. ffmpeg/ffprobe
run for real only inside audio.finalize_output on tiny generated clips.
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

from local_gateway import audio, config, registry, tts, tts_workflow  # noqa: E402
from local_gateway.comfyui import ComfyUIError, ComfyUITimeout  # noqa: E402
from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

LANGUAGES = ["Auto", "English", "Chinese"]
REF_BYTES = b"ID3fake-audio-bytes"


def clone_object_info_fixture(**overrides):
    base = {
        "AILab_Qwen3TTSVoiceClone_Advanced": {"input": {"required": {
            "model_size": [["0.6B", "1.7B"], {}],
            "language": [list(LANGUAGES), {}],
            "target_text": ["STRING", {}],
            "reference_text": ["STRING", {}],
            "reference_audio": ["AUDIO", {}],
        }}},
        "LoadAudio": {"input": {"required": {"audio": ["AUDIO", {}]}}},
        "SaveAudioAdvanced": {"input": {"required": {
            "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                {"key": "flac", "inputs": {"required": {}}},
                {"key": "mp3", "inputs": {"required": {}}},
                {"key": "opus", "inputs": {"required": {}}},
            ]}]}}},
    }
    base.update(overrides)
    return base


def success_entry(filename="clone_00001_.mp3"):
    return {"status": {"completed": True, "status_str": "success"},
            "outputs": {tts_workflow.SAVE_NODE: {"audio": [
                {"filename": filename, "subfolder": "ccvt",
                 "type": "output"}]}}}


class FakeTTSClient:
    def __init__(self, *, object_info=None, upload_response=None, entries=None,
                 view_bytes=b"", wait_exc=None):
        self.object_info_data = (object_info if object_info is not None
                                 else clone_object_info_fixture())
        self.upload_response = (upload_response if upload_response is not None
                                else {"name": "ccvt_clone_ref_ab12.mp3",
                                      "subfolder": "", "type": "input"})
        self.entries = entries if entries is not None else {}
        self.view_bytes = view_bytes
        self.wait_exc = wait_exc
        self.upload_calls = []
        self.submit_calls = []
        self.view_calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        self.closed = True

    def object_info(self, node_name=None):
        if node_name not in self.object_info_data:
            raise ComfyUIError(
                f"Invalid node class {node_name!r} requested",
                code="node_missing")
        return self.object_info_data[node_name]

    def upload(self, filename, content, content_type="application/octet-stream"):
        self.upload_calls.append((filename, content, content_type))
        return self.upload_response

    def submit(self, prompt):
        self.submit_calls.append(prompt)
        return f"prompt-{len(self.submit_calls)}"

    def wait(self, prompt_id, deadline, poll_interval=1.0):
        if self.wait_exc is not None:
            raise self.wait_exc
        return self.entries[prompt_id]

    def view(self, filename, subfolder="", file_type="output",
             max_bytes=audio.VIEW_MAX_BYTES):
        self.view_calls.append((filename, subfolder, file_type, max_bytes))
        return self.view_bytes


class Mp3Mixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "clip.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.3", "-c:a", "libmp3lame",
             str(out)],
            check=True, capture_output=True, shell=False)
        cls.mp3_bytes = out.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class GatewayFixture(Mp3Mixin):
    def make_settings(self, tmp):
        root = Path(tmp)
        for rel in registry.model_by_key(
                "qwen3-tts-1.7B").required_nonempty_directories:
            d = root / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / "weights.safetensors").write_bytes(b"x")
        return config.load_settings(env={"COMFYUI_MODELS_ROOT": str(root)})


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def valid_body(**overrides):
    body = {"text": "你好，世界", "ref_audio_base64": b64(REF_BYTES),
            "ref_text": "参考音频逐字稿"}
    body.update(overrides)
    return body


class DeadlineFormulaTests(unittest.TestCase):
    def test_matches_the_spec_formula(self):
        for count, expected in ((1, 270), (3, 270), (4, 540), (8, 810)):
            self.assertEqual(tts.deadline_seconds(count), expected, count)


class ErrorCodeMappingTests(unittest.TestCase):
    def test_spec_section_9_statuses(self):
        expected = {
            "mode_unsupported": 422, "validation_error": 422,
            "unknown_model": 422, "external_url_unsupported": 422,
            "dependency_missing": 503, "comfyui_unreachable": 503,
            "comfyui_request_timeout": 504, "comfyui_timeout": 504,
            "comfyui_error": 502, "generation_failed": 500,
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
        self.assertEqual(request.languages, ("Auto",))
        self.assertEqual(request.output_format, "mp3")
        self.assertEqual(request.model, "qwen3-tts-1.7B")
        self.assertEqual(request.ref_audio_bytes, REF_BYTES)
        self.assertIsNone(request.temperature)
        self.assertIsNone(request.top_p)

    def test_explicit_null_sampling_is_treated_as_absent(self):
        request = self.parse(valid_body(temperature=None, top_p=None))
        self.assertIsNone(request.temperature)
        self.assertIsNone(request.top_p)

    def test_unknown_field_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(speaker="Ryan"))
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertIn("speaker", ctx.exception.message)

    def test_custom_voice_key_is_mode_unsupported(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(custom_voice={"speaker": "Ryan"}))
        self.assertEqual(ctx.exception.code, "mode_unsupported")

    def test_voice_design_key_is_mode_unsupported(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(voice_design={"instruct": "warm"}))
        self.assertEqual(ctx.exception.code, "mode_unsupported")

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

    def test_unknown_model_is_unknown_model(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(model="qwen3-tts-0.6B"))
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_non_tts_model_is_rejected_not_treated_as_clone_model(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(model="qwen-image-2.1"))
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_text_batch_limits(self):
        nine = [f"t{i}" for i in range(9)]
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=nine))
        self.assertEqual(ctx.exception.code, "validation_error")
        eight = [f"t{i}" for i in range(8)]
        request = self.parse(valid_body(text=eight))
        self.assertEqual(len(request.texts), 8)

    def test_empty_text_items_rejected(self):
        for bad in ("", "   ", ["ok", ""]):
            with self.subTest(bad=bad):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(text=bad))
                self.assertEqual(ctx.exception.code, "validation_error")

    def test_language_array_must_match_text_length(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=["a", "b"], language=["Auto"]))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_language_scalar_broadcasts_over_batch(self):
        request = self.parse(valid_body(text=["a", "b"], language="Chinese"))
        self.assertEqual(request.languages, ("Chinese", "Chinese"))

    def test_bad_output_format_rejected(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(output_format="opus"))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_sampling_ranges_and_types(self):
        cases = [
            {"temperature": 0.05}, {"temperature": 2.5},
            {"temperature": True}, {"temperature": float("nan")},
            {"top_p": -0.1}, {"top_p": 1.5}, {"top_p": "0.5"},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(**case))
                self.assertEqual(ctx.exception.code, "validation_error")

    def test_data_url_prefix_decodes_and_sets_suffix(self):
        body = valid_body(ref_audio_base64="data:audio/wav;base64," + b64(b"RIFF"))
        request = self.parse(body)
        self.assertEqual(request.ref_audio_bytes, b"RIFF")
        self.assertEqual(request.ref_suffix, ".wav")

    def test_invalid_base64_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(ref_audio_base64="not valid base64!"))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_ref_audio_over_limit_is_validation_error(self):
        with mock.patch.object(tts, "MAX_REF_AUDIO_BYTES", 8):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.parse(valid_body(ref_audio_base64=b64(b"0123456789")))
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(tts.MAX_REF_AUDIO_BYTES, 16 * 1024 * 1024)

    def test_missing_ref_text_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(ref_text=""))
        self.assertEqual(ctx.exception.code, "validation_error")


class PreflightTests(GatewayFixture, unittest.TestCase):
    def test_missing_ffmpeg_is_dependency_missing_before_any_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)

            def never(url):
                raise AssertionError("preflight must fail before any client")

            with mock.patch("shutil.which",
                            side_effect=lambda name: None):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, never)
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_empty_weights_directory_is_dependency_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            for rel in registry.model_by_key(
                    "qwen3-tts-1.7B").required_nonempty_directories:
                for child in (Path(tmp) / rel).iterdir():
                    child.unlink()
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: FakeTTSClient())
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_missing_clone_node_is_dependency_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: FakeTTSClient(
                        object_info={}))
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_object_info_http_error_maps_to_its_comfyui_code(self):
        class Broken(FakeTTSClient):
            def object_info(self, node_name=None):
                raise ComfyUIError("HTTP 500", code="comfyui_error")

        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: Broken())
        self.assertEqual(ctx.exception.code, "comfyui_error")
        self.assertEqual(ctx.exception.http_status, 502)

    def test_save_format_options_are_checked(self):
        meta = clone_object_info_fixture(
            SaveAudioAdvanced={"input": {"required": {
                "format": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                    {"key": "flac", "inputs": {"required": {}}},
                    {"key": "opus", "inputs": {"required": {}}},
                ]}]}}})
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    tts.preflight(settings, lambda url: FakeTTSClient(
                        object_info=meta))
        self.assertEqual(ctx.exception.code, "dependency_missing")
        self.assertIn("mp3", ctx.exception.message)

    def test_language_choices_come_from_the_runtime_enum(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
                result = tts.preflight(settings, lambda url: FakeTTSClient())
        self.assertEqual(result.language_choices, tuple(LANGUAGES))


class GenerateCloneTests(GatewayFixture, unittest.TestCase):
    def client_for(self, **kwargs):
        kwargs.setdefault("view_bytes", self.mp3_bytes)
        entries = kwargs.pop("entries", None)
        if entries is None:
            entries = {f"prompt-{i+1}": success_entry(f"clone_{i+1:05d}_.mp3")
                       for i in range(8)}
        return FakeTTSClient(entries=entries, **kwargs)

    def run_clone(self, client, body, settings_tmp, lock=None):
        settings = self.make_settings(settings_tmp)
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            return tts.generate_clone(settings, lambda url: client, body,
                                      lock or threading.Lock())

    def test_single_text_success_shape(self):
        client = self.client_for()
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.run_clone(client, valid_body(), tmp)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["model"], "qwen3-tts-1.7B")
        self.assertGreaterEqual(payload["processing_time_seconds"], 0)
        self.assertEqual(len(payload["outputs"]), 1)
        item = payload["outputs"][0]
        self.assertEqual(item["audio_base64"], b64(self.mp3_bytes))
        self.assertGreater(item["duration_seconds"], 0)
        # single-text mirror for existing clients
        self.assertEqual(payload["audio_base64"], item["audio_base64"])
        self.assertEqual(payload["duration_seconds"],
                         item["duration_seconds"])
        self.assertNotIn("audio_url", payload)
        self.assertNotIn("r2_key", payload)

    def test_batch_uploads_once_and_keeps_order(self):
        client = self.client_for()
        body = valid_body(text=["第一句", "第二句", "第三句"],
                          language=["Chinese", "Auto", "English"])
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.run_clone(client, body, tmp)
        self.assertEqual(len(client.upload_calls), 1)
        self.assertEqual(len(client.submit_calls), 3)
        self.assertEqual(len(client.view_calls), 3)
        self.assertEqual(
            [prompt[tts_workflow.TTS_NODE]["inputs"]["target_text"]
             for prompt in client.submit_calls],
            ["第一句", "第二句", "第三句"])
        self.assertEqual(
            [prompt[tts_workflow.TTS_NODE]["inputs"]["language"]
             for prompt in client.submit_calls],
            ["Chinese", "Auto", "English"])
        prefixes = [prompt[tts_workflow.SAVE_NODE]["inputs"]["filename_prefix"]
                    for prompt in client.submit_calls]
        self.assertEqual(len(set(prefixes)), 3)
        self.assertTrue(all(p.startswith("ccvt/tts/") for p in prefixes))
        self.assertEqual(len(payload["outputs"]), 3)
        self.assertNotIn("audio_base64", payload)

    def test_upload_join_uses_subfolder_when_present(self):
        client = self.client_for(
            upload_response={"name": "ref_9.mp3", "subfolder": "ccvt",
                             "type": "input"})
        with tempfile.TemporaryDirectory() as tmp:
            self.run_clone(client, valid_body(), tmp)
        prompt = client.submit_calls[0]
        self.assertEqual(prompt["1"]["inputs"]["audio"], "ccvt/ref_9.mp3")

    def test_upload_without_name_is_generation_failed(self):
        client = self.client_for(upload_response={"type": "input"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(), tmp)
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_language_outside_runtime_enum_is_validation_error(self):
        client = self.client_for()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(language="Klingon"), tmp)
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(client.submit_calls, [])

    def test_item_failure_fails_the_whole_request_without_partials(self):
        client = self.client_for(wait_exc=ComfyUIError(
            "execution blew up", code="execution_error",
            details={"status_str": "error"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(text=["a", "b"]), tmp)
        self.assertEqual(ctx.exception.code, "generation_failed")
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertEqual(client.view_calls, [])

    def test_wait_timeout_maps_to_504(self):
        client = self.client_for(
            wait_exc=ComfyUITimeout("prompt-1", time.monotonic() + 1))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body(), tmp)
        self.assertEqual(ctx.exception.code, "comfyui_timeout")
        self.assertEqual(ctx.exception.http_status, 504)

    def test_lock_is_released_after_success_and_failure(self):
        lock = threading.Lock()
        client = self.client_for()
        with tempfile.TemporaryDirectory() as tmp:
            self.run_clone(client, valid_body(), tmp, lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

        lock = threading.Lock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tts.TTSRouteError):
                self.run_clone(self.client_for(wait_exc=ComfyUIError(
                    "boom", code="execution_error")), valid_body(), tmp,
                    lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_deadline_and_processing_time_start_before_the_lock(self):
        lock = threading.Lock()
        lock.acquire()
        done = threading.Event()
        result = {}

        def worker():
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    result["payload"] = self.run_clone(
                        self.client_for(), valid_body(), tmp, lock=lock)
                finally:
                    done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.5)
        lock.release()
        self.assertTrue(done.wait(30))
        thread.join(30)
        self.assertGreaterEqual(result["payload"]["processing_time_seconds"],
                                0.5)

    def test_lock_wait_timeout_is_comfyui_timeout_without_submit(self):
        lock = threading.Lock()
        lock.acquire()
        client = self.client_for()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(tts, "deadline_seconds", return_value=0):
                    with self.assertRaises(tts.TTSRouteError) as ctx:
                        self.run_clone(client, valid_body(), tmp, lock=lock)
            self.assertEqual(ctx.exception.code, "comfyui_timeout")
            self.assertEqual(client.submit_calls, [])
        finally:
            lock.release()

    def test_validation_error_raises_before_the_lock(self):
        lock = threading.Lock()
        acquired = []
        real_acquire = lock.acquire

        def tracked_acquire(*args, **kwargs):
            acquired.append(True)
            return real_acquire(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(lock, "acquire", side_effect=tracked_acquire):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.run_clone(self.client_for(), valid_body(mode="x"), tmp,
                                   lock=lock)
        self.assertEqual(ctx.exception.code, "mode_unsupported")
        self.assertEqual(acquired, [])


class RouteTests(GatewayFixture, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The settings must outlive the requests: preflight walks this tree.
        self._settings = self.make_settings(self._tmp.name)

    def make_client_app(self, client):
        app = server.create_app(settings=self._settings,
                                client_factory=lambda url: client)
        return TestClient(app)

    def test_success_returns_200(self):
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(
                    FakeTTSClient(view_bytes=self.mp3_bytes)) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["outputs"][0]["duration_seconds"] > 0, True)

    def test_error_codes_map_to_spec_statuses(self):
        cases = [
            (valid_body(mode="custom_voice"), FakeTTSClient(), 422,
             "mode_unsupported"),
            (valid_body(unknown=1), FakeTTSClient(), 422, "validation_error"),
            (valid_body(model="nope"), FakeTTSClient(), 422, "unknown_model"),
            (valid_body(ref_audio_url="https://x"), FakeTTSClient(), 422,
             "external_url_unsupported"),
            (valid_body(), FakeTTSClient(object_info={}), 503,
             "dependency_missing"),
            (valid_body(), FakeTTSClient(
                wait_exc=ComfyUITimeout("p", time.monotonic() + 1)), 504,
             "comfyui_timeout"),
            (valid_body(), FakeTTSClient(
                wait_exc=ComfyUIError("exec", code="execution_error")), 500,
             "generation_failed"),
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

    def test_comfyui_unreachable_via_object_info_is_503(self):
        class Broken(FakeTTSClient):
            def object_info(self, node_name=None):
                raise ComfyUIError("refused", code="comfyui_unreachable")

        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(Broken()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "comfyui_unreachable")

    def test_comfyui_request_timeout_is_504(self):
        class Slow(FakeTTSClient):
            def object_info(self, node_name=None):
                raise ComfyUIError("read timed out",
                                   code="comfyui_request_timeout")

        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(Slow()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["code"], "comfyui_request_timeout")

    def test_unexpected_exception_is_generic_generation_failed(self):
        with mock.patch.object(tts, "generate_clone",
                               side_effect=RuntimeError("secret internals")):
            with self.make_client_app(FakeTTSClient()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "generation_failed")
        self.assertNotIn("secret internals", body["error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'local_gateway.tts'` (and `server` has no route yet for the RouteTests once tts exists; at this step everything fails on the import).

- [ ] **Step 3: Implement tts.py**

Create `local-gateway/local_gateway/tts.py`:

```python
"""POST /tts/generate orchestration: validation, preflight, deadline, lock.

Clone-only. This module never builds a FastAPI response (server.py does);
it returns a success payload dict or raises TTSRouteError with the exact
code/HTTP-status contract of spec section 9. The total deadline starts at
route entry (before waiting for the lock) and covers upload, submit, poll,
view, ffprobe and FFmpeg. One lock per gateway process serializes whole
clone requests; /health never takes it.
"""
from __future__ import annotations

import base64
import binascii
import math
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import audio, registry, tts_workflow
from .comfyui import ComfyUIError, input_names, input_values
from .config import GatewaySettings

ALLOWED_FIELDS = frozenset({
    "model", "text", "mode", "language", "output_format",
    "ref_audio_base64", "ref_text", "temperature", "top_p",
})
MODE_FLAG_FIELDS = frozenset({"custom_voice", "voice_design"})
EXTERNAL_FIELDS = frozenset({"ref_audio_url", "r2"})
OUTPUT_FORMATS = ("mp3", "wav")
MAX_REF_AUDIO_BYTES = 16 * 1024 * 1024
CLONE_NODE = "AILab_Qwen3TTSVoiceClone_Advanced"
LOAD_AUDIO_NODE_NAME = "LoadAudio"
SAVE_NODE_NAME = "SaveAudioAdvanced"
CLONE_REQUIRED_INPUTS = ("target_text", "reference_text", "reference_audio",
                         "language")
_DATA_URL_PREFIX = "data:"
_MIME_SUFFIXES = {"audio/wav": ".wav", "audio/x-wav": ".wav",
                  "audio/mpeg": ".mp3", "audio/mp3": ".mp3"}
_REF_CONTENT_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg"}

ERROR_STATUS = {
    "mode_unsupported": 422,
    "validation_error": 422,
    "unknown_model": 422,
    "external_url_unsupported": 422,
    "dependency_missing": 503,
    "comfyui_unreachable": 503,
    "comfyui_request_timeout": 504,
    "comfyui_timeout": 504,
    "comfyui_error": 502,
    "generation_failed": 500,
}


class TTSRouteError(Exception):
    """Route-level error carrying a spec section 9 code and HTTP status."""

    def __init__(self, message: str, code: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details
        self.http_status = ERROR_STATUS.get(code, 500)


def _from_comfyui(e: ComfyUIError) -> TTSRouteError:
    code = e.code
    if code in ("execution_error", "node_missing"):
        code = "generation_failed"
    return TTSRouteError(e.message, code, details=e.details)


def deadline_seconds(item_count: int) -> int:
    """270 * max(1, item_count // 3 + 1), chunk size from the registry."""
    entry = registry.model_by_key(registry.ROUTE_TARGETS["tts"])
    chunk = entry.deadline_chunk_seconds if entry else 270
    return chunk * max(1, item_count // 3 + 1)


@dataclass(frozen=True)
class TTSRequest:
    texts: tuple[str, ...]
    languages: tuple[str, ...]
    output_format: str
    model: str
    ref_audio_bytes: bytes
    ref_suffix: str
    ref_text: str
    temperature: float | None
    top_p: float | None


@dataclass(frozen=True)
class Preflight:
    language_choices: tuple[str, ...]


def _validation(message: str) -> TTSRouteError:
    return TTSRouteError(message, "validation_error")


def _dependency(message: str) -> TTSRouteError:
    return TTSRouteError(message, "dependency_missing")


def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def parse_request(body) -> TTSRequest:
    """Validate the clone request body (spec section 5). Raises TTSRouteError."""
    if not isinstance(body, dict):
        raise _validation("request body must be a JSON object")
    mode_flags = sorted(set(body) & MODE_FLAG_FIELDS)
    if mode_flags:
        raise TTSRouteError(
            "this gateway supports voice clone only; custom voice and voice"
            f" design are not implemented locally (field(s):"
            f" {', '.join(mode_flags)})",
            "mode_unsupported")
    external = sorted(set(body) & EXTERNAL_FIELDS)
    if external:
        raise TTSRouteError(
            f"field {external[0]!r} is not supported: the local gateway"
            " clones from ref_audio_base64 and returns base64 only; it"
            " never downloads from or uploads to external storage",
            "external_url_unsupported")
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise _validation(f"unknown request field(s): {', '.join(unknown)}")
    mode = body.get("mode", "clone")
    if mode != "clone":
        raise TTSRouteError(
            f"mode {mode!r} is not supported; this gateway implements"
            " 'clone' only",
            "mode_unsupported")

    entry = registry.model_by_key(registry.ROUTE_TARGETS["tts"])
    batch_limit = entry.max_batch_items if entry else 8
    text = body.get("text")
    if isinstance(text, str):
        if not text.strip():
            raise _validation("text must be a non-empty string")
        texts = [text]
    elif isinstance(text, list):
        if not 1 <= len(text) <= batch_limit:
            raise _validation(
                f"text must have 1..{batch_limit} items, got {len(text)}")
        for i, item in enumerate(text):
            if not isinstance(item, str) or not item.strip():
                raise _validation(f"text[{i}] must be a non-empty string")
        texts = list(text)
    else:
        raise _validation(
            "text is required: a non-empty string or a list of them")

    language = body.get("language", "Auto")
    if isinstance(language, str):
        if not language.strip():
            raise _validation("language must be a non-empty string")
        languages = [language] * len(texts)
    elif isinstance(language, list):
        if len(language) != len(texts):
            raise _validation(
                f"language list must match text length {len(texts)},"
                f" got {len(language)}")
        for i, item in enumerate(language):
            if not isinstance(item, str) or not item.strip():
                raise _validation(
                    f"language[{i}] must be a non-empty string")
        languages = list(language)
    else:
        raise _validation("language must be a string or a list of strings")

    output_format = body.get("output_format", "mp3")
    if output_format not in OUTPUT_FORMATS:
        raise _validation(
            f"output_format must be one of {OUTPUT_FORMATS},"
            f" got {output_format!r}")

    model = body.get("model")
    if model is None:
        model = registry.ROUTE_TARGETS["tts"]
    elif not isinstance(model, str) or not model:
        raise _validation("model must be a non-empty string")
    else:
        model_entry = registry.model_by_key(model)
        if (model_entry is None or model_entry.capability != "tts"
                or "clone" not in model_entry.modes):
            raise TTSRouteError(
                f"unknown model {model!r}; this route accepts only clone-capable"
                f" TTS models (for example {registry.ROUTE_TARGETS['tts']})",
                "unknown_model")

    ref_b64 = body.get("ref_audio_base64")
    if not isinstance(ref_b64, str) or not ref_b64:
        raise _validation("ref_audio_base64 is required for voice clone")
    ref_suffix = ".mp3"
    payload_text = ref_b64
    if payload_text.startswith(_DATA_URL_PREFIX):
        comma = payload_text.find(",")
        if comma < 0:
            raise _validation("ref_audio_base64 data URL has no comma")
        mime = payload_text[len(_DATA_URL_PREFIX):comma]
        payload_text = payload_text[comma + 1:]
        ref_suffix = _MIME_SUFFIXES.get(mime, ".mp3")
    try:
        ref_bytes = base64.b64decode(payload_text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise _validation(f"ref_audio_base64 is not valid base64: {e}") from e
    if not ref_bytes:
        raise _validation("ref_audio_base64 decodes to empty audio")
    if len(ref_bytes) > MAX_REF_AUDIO_BYTES:
        raise _validation(
            f"ref_audio_base64 decodes to {len(ref_bytes)} bytes; the limit"
            f" is {MAX_REF_AUDIO_BYTES}")

    ref_text = body.get("ref_text")
    if not isinstance(ref_text, str) or not ref_text.strip():
        raise _validation(
            "ref_text is required: the transcript of the reference audio")

    temperature = body.get("temperature")
    if temperature is not None:
        if not _finite_number(temperature):
            raise _validation("temperature must be a finite number")
        if not 0.1 <= temperature <= 2.0:
            raise _validation("temperature must be within 0.1-2.0")
    top_p = body.get("top_p")
    if top_p is not None:
        if not _finite_number(top_p):
            raise _validation("top_p must be a finite number")
        if not 0.0 <= top_p <= 1.0:
            raise _validation("top_p must be within 0.0-1.0")

    return TTSRequest(
        texts=tuple(texts), languages=tuple(languages),
        output_format=output_format, model=model,
        ref_audio_bytes=ref_bytes, ref_suffix=ref_suffix,
        ref_text=ref_text, temperature=temperature, top_p=top_p)


def preflight(settings: GatewaySettings, client_factory) -> Preflight:
    """Clone-only dependency check (spec section 7); runs before any submit."""
    missing_tools = [tool for tool in ("ffmpeg", "ffprobe")
                     if shutil.which(tool) is None]
    if missing_tools:
        raise _dependency(
            "voice clone needs ffmpeg and ffprobe on this gateway process's"
            f" PATH; missing: {', '.join(missing_tools)}")
    entry = registry.model_by_key(registry.ROUTE_TARGETS["tts"])
    if entry is None or settings.models_root is None:
        raise _dependency(
            "COMFYUI_MODELS_ROOT is not configured; the clone weights"
            " cannot be verified")
    for rel in entry.required_nonempty_directories:
        directory = Path(settings.models_root) / rel
        try:
            nonempty = directory.is_dir() and any(directory.iterdir())
        except OSError:
            nonempty = False
        if not nonempty:
            raise _dependency(
                f"clone weights directory {rel!r} under COMFYUI_MODELS_ROOT"
                " is missing or empty; the AILab node would fall back to"
                " snapshot_download")
    try:
        with client_factory(settings.base_url) as client:
            clone_meta = client.object_info(CLONE_NODE)
            load_meta = client.object_info(LOAD_AUDIO_NODE_NAME)
            save_meta = client.object_info(SAVE_NODE_NAME)
    except ComfyUIError as e:
        if e.code == "node_missing":
            raise _dependency(
                f"ComfyUI lacks a node required by voice clone: {e.message}"
                " Install/enable the AILab Qwen3-TTS custom node package."
            ) from e
        raise _from_comfyui(e) from e
    clone_inputs = input_names(clone_meta)
    for required in CLONE_REQUIRED_INPUTS:
        if required not in clone_inputs:
            raise _dependency(
                f"{CLONE_NODE} does not expose the {required!r} input")
    if "1.7B" not in (input_values(clone_meta, "model_size") or []):
        raise _dependency(f"{CLONE_NODE}.model_size does not offer 1.7B")
    languages = input_values(clone_meta, "language") or []
    if not languages:
        raise _dependency(f"{CLONE_NODE}.language exposes no choices")
    if "audio" not in input_names(load_meta):
        raise _dependency("LoadAudio does not expose the 'audio' input")
    formats = input_values(save_meta, "format") or []
    for needed in ("mp3", "flac"):
        if needed not in formats:
            raise _dependency(
                f"SaveAudioAdvanced.format does not offer {needed!r}")
    return Preflight(language_choices=tuple(str(v) for v in languages))


def _validate_languages(request: TTSRequest, choices: tuple[str, ...]) -> None:
    allowed = set(choices)
    for i, language in enumerate(request.languages):
        if language not in allowed:
            raise _validation(
                f"language[{i}] {language!r} is not offered by {CLONE_NODE}"
                f" (choices: {', '.join(choices)})")


def _require_remaining(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TTSRouteError(
            f"clone deadline reached at stage {stage}", "comfyui_timeout")


def _upload_reference(client, request: TTSRequest) -> str:
    """Upload the reference once; join the server-chosen name/subfolder."""
    filename = f"ccvt_clone_ref_{uuid.uuid4().hex}{request.ref_suffix}"
    content_type = _REF_CONTENT_TYPES.get(request.ref_suffix,
                                          "application/octet-stream")
    response = client.upload(filename, request.ref_audio_bytes, content_type)
    name = response.get("name")
    subfolder = response.get("subfolder", "")
    if not isinstance(name, str) or not name:
        raise TTSRouteError(
            "ComfyUI accepted the reference upload but returned no usable"
            f" name: {str(response)[:200]}",
            "generation_failed")
    if isinstance(subfolder, str) and subfolder:
        return f"{subfolder}/{name}"
    return name


def generate_clone(settings: GatewaySettings, client_factory, body: dict,
                   lock) -> dict:
    """Validate, serialize on `lock`, and run the whole clone request."""
    started = time.monotonic()
    request = parse_request(body)  # 422-class errors raise before the lock
    deadline = started + deadline_seconds(len(request.texts))
    remaining = max(0.0, deadline - time.monotonic())
    if not lock.acquire(timeout=remaining):
        raise TTSRouteError(
            "timed out waiting for the gateway's TTS lock; the clone request"
            " was never submitted", "comfyui_timeout")
    try:
        _require_remaining(deadline, "lock")
        return _generate_locked(settings, client_factory, request, deadline,
                                started)
    finally:
        lock.release()


def _generate_locked(settings: GatewaySettings, client_factory,
                     request: TTSRequest, deadline: float,
                     started: float) -> dict:
    _require_remaining(deadline, "preflight")
    preflight_result = preflight(settings, client_factory)
    _validate_languages(request, preflight_result.language_choices)
    budget = audio.ViewBudget()
    outputs: list[dict] = []
    with client_factory(settings.base_url) as client:
        reference_audio = _upload_reference(client, request)
        with tempfile.TemporaryDirectory(prefix="ccvt_tts_") as tmp:
            workdir = Path(tmp)
            for index, text in enumerate(request.texts):
                _require_remaining(deadline, f"item {index}")
                try:
                    prompt = tts_workflow.build_clone_prompt(
                        text=text, ref_text=request.ref_text,
                        reference_audio=reference_audio,
                        language=request.languages[index],
                        output_format=request.output_format,
                        output_prefix=f"ccvt/tts/{uuid.uuid4().hex}/{index}",
                        temperature=request.temperature,
                        top_p=request.top_p)
                    prompt_id = client.submit(prompt)
                    entry = client.wait(prompt_id, deadline)
                    audio_bytes, duration = audio.finalize_output(
                        client, entry, request.output_format, workdir,
                        f"item_{index}", budget, deadline)
                except ComfyUIError as e:
                    raise _from_comfyui(e) from e
                except ValueError as e:
                    raise TTSRouteError(
                        f"clone audio processing failed: {e}",
                        "generation_failed") from e
                outputs.append({
                    "audio_base64":
                        base64.b64encode(audio_bytes).decode("ascii"),
                    "duration_seconds": duration,
                })
    payload = {
        "success": True,
        "mode": "clone",
        "model": request.model,
        "processing_time_seconds": round(time.monotonic() - started, 3),
        "outputs": outputs,
    }
    if len(outputs) == 1:
        payload["audio_base64"] = outputs[0]["audio_base64"]
        payload["duration_seconds"] = outputs[0]["duration_seconds"]
    return payload
```

- [ ] **Step 4: Implement the server route**

Replace the whole of `local-gateway/local_gateway/server.py` with:

```python
"""FastAPI app factory, GET /health and POST /tts/generate.

The factory takes the settings and the ComfyUI client factory as optional
arguments so tests inject fakes. With no settings argument, local-gateway/.env
is loaded lazily inside the request, so importing the module or creating an
app never reads the developer's real .env and never opens a socket. The TTS
route only assembles dependencies and render JSON; orchestration and error
contract live in tts.py. app.state.tts_lock serializes clone requests per
process; /health never takes it.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import tts
from .comfyui import ComfyUIClient
from .config import GatewaySettings, load_settings
from .health import build_health_payload

ClientFactory = Callable[[str], ComfyUIClient]
logger = logging.getLogger(__name__)


def default_client_factory(base_url: str) -> ComfyUIClient:
    return ComfyUIClient(base_url)


def create_app(settings: GatewaySettings | None = None,
               client_factory: ClientFactory | None = None) -> FastAPI:
    app_settings = settings
    app_client_factory = client_factory if client_factory is not None else default_client_factory

    app = FastAPI(title="ccvt local gateway", version="0.1.0")
    app.state.tts_lock = threading.Lock()

    @app.get("/health")
    def health() -> JSONResponse:
        # Plain def on purpose: Starlette runs blocking requests calls in a
        # threadpool, so slow ComfyUI checks never block the event loop.
        active_settings = app_settings if app_settings is not None else load_settings()
        payload, status_code = build_health_payload(active_settings, app_client_factory)
        return JSONResponse(content=payload, status_code=status_code)

    @app.post("/tts/generate")
    def tts_generate(body: dict) -> JSONResponse:
        # Plain def: the clone request blocks (lock + polling) and must not
        # block the event loop; /health stays responsive without the lock.
        active_settings = app_settings if app_settings is not None else load_settings()
        try:
            payload = tts.generate_clone(active_settings, app_client_factory,
                                         body, app.state.tts_lock)
        except tts.TTSRouteError as e:
            error = {"success": False, "error": e.message, "code": e.code}
            if e.details:
                error["details"] = e.details
            return JSONResponse(content=error, status_code=e.http_status)
        except Exception as e:
            logger.exception("Unexpected error while generating TTS clone")
            error = {"success": False, "error": "internal gateway error",
                     "code": "generation_failed"}
            return JSONResponse(content=error, status_code=500)
        return JSONResponse(content=payload, status_code=200)

    return app


app = create_app()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: PASS, all gateway tests (including the pre-existing health/server tests; note `create_app` still makes no network request at import/factory time).

- [ ] **Step 6: Commit**

```bash
git add local-gateway/local_gateway/tts.py local-gateway/local_gateway/server.py local-gateway/tests/test_tts.py
git commit -m "FEAT: POST /tts/generate clone orchestration and route"
```

---

### Task 6: qwen3_tts local clone provider

**Files:**
- Modify: `tools/qwen3_tts.py`
- Test: `tests/python/test_qwen3_tts_local.py` (append classes)

**Interfaces:**
- Consumes: Task 1's `--cloud local` choice and setup guard; existing `cloud_gpu.call_cloud_endpoint` (lazy import inside `generate_audio`), `file_transfer.upload_to_storage` / `download_from_r2` / `download_from_url` / `get_r2_payload_config` (already imported at module top of qwen3_tts).
- Produces: `generate_audio(..., model: str | None = None)`: model enters `payload["input"]["model"]` only when not None, for every provider; local mode: inline `ref_audio_base64`, no `ref_audio_url`, no `r2`, base64-only result handling, early clone-only rejection. CLI: `--model` free string; local pre-HTTP validation in `main()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/python/test_qwen3_tts_local.py` (before the `if __name__` block):

```python
def run_generate(text, output, ref_path, cloud_output=None, **kwargs):
    """Run qwen3_tts.generate_audio with a faked cloud call.

    Returns (result, captured, up, dl_r2, dl_url); captured holds the
    provider, payload and timeout seen by the faked call_cloud_endpoint.
    """
    if cloud_output is None:
        cloud_output = {"success": True, "outputs": [{
            "audio_base64": base64.b64encode(b"MP3DATA").decode("ascii"),
            "duration_seconds": 1.25,
        }]}
    captured = {}

    def fake_call(provider, payload, tool_name, timeout, **rest):
        captured["provider"] = provider
        captured["payload"] = payload
        captured["timeout"] = timeout
        return cloud_output, 0.5

    with mock.patch("cloud_gpu.call_cloud_endpoint", side_effect=fake_call), \
            mock.patch.object(qwen3_tts, "upload_to_storage") as up, \
            mock.patch.object(qwen3_tts, "download_from_r2") as dl_r2, \
            mock.patch.object(qwen3_tts, "download_from_url") as dl_url, \
            mock.patch.object(qwen3_tts, "get_r2_payload_config",
                              return_value=None):
        result = qwen3_tts.generate_audio(
            text=text, output_path=output, ref_audio=str(ref_path),
            ref_text=kwargs.pop("ref_text", "参考"), verbose=False, **kwargs)
    return result, captured, up, dl_r2, dl_url


class LocalPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_local_clone_payload_is_base64_only(self):
        result, captured, up, dl_r2, dl_url = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            model="qwen3-tts-1.7B")
        self.assertTrue(result["success"], result)
        payload = captured["payload"]["input"]
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["ref_audio_base64"],
                         base64.b64encode(b"REFERENCE-AUDIO").decode("ascii"))
        self.assertEqual(payload["ref_text"], "参考")
        self.assertEqual(payload["model"], "qwen3-tts-1.7B")
        self.assertNotIn("ref_audio_url", payload)
        self.assertNotIn("r2", payload)
        self.assertNotIn("speaker", payload)
        up.assert_not_called()
        dl_r2.assert_not_called()
        dl_url.assert_not_called()
        self.assertEqual(captured["provider"], "local")

    def test_local_clone_without_model_omits_model(self):
        result, captured, up, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local")
        self.assertTrue(result["success"])
        self.assertNotIn("model", captured["payload"]["input"])

    def test_local_timeout_scales_with_batch(self):
        _, captured, _, _, _ = run_generate(
            ["一", "二", "三", "四", "五"],
            [str(self.out)] * 5, self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [
                {"audio_base64": base64.b64encode(b"M").decode("ascii"),
                 "duration_seconds": 1.0}] * 5})
        self.assertEqual(captured["timeout"], 300 * max(1, 5 // 3 + 1))

    def test_local_sampling_params_forwarded(self):
        _, captured, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            temperature=0.6, top_p=0.9)
        payload = captured["payload"]["input"]
        self.assertEqual(payload["temperature"], 0.6)
        self.assertEqual(payload["top_p"], 0.9)


class LocalResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_local_base64_result_writes_file(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local")
        self.assertTrue(result["success"])
        self.assertEqual(self.out.read_bytes(), b"MP3DATA")

    def test_local_result_with_audio_url_fails_explicitly(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [{
                "audio_url": "https://files.example/x.mp3",
                "duration_seconds": 1.0,
            }]})
        self.assertFalse(result["success"])
        self.assertIn("base64", result["error"])

    def test_local_result_with_r2_key_fails_explicitly(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [{
                "r2_key": "qwen3-tts/out.mp3",
                "duration_seconds": 1.0,
            }]})
        self.assertFalse(result["success"])
        self.assertIn("base64", result["error"])

    def test_local_result_without_audio_fails(self):
        result, _, _, _, _ = run_generate(
            "你好", str(self.out), self.ref, cloud="local",
            cloud_output={"success": True, "outputs": [{"duration_seconds": 1.0}]})
        self.assertFalse(result["success"])


class LocalRejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def reject(self, **kwargs):
        with mock.patch("cloud_gpu.call_cloud_endpoint",
                        side_effect=AssertionError("no HTTP allowed")):
            return qwen3_tts.generate_audio(
                text="你好", output_path=str(self.out), verbose=False, **kwargs)

    def test_local_without_ref_audio_is_rejected_before_http(self):
        result = self.reject(cloud="local", speaker="Ryan")
        self.assertFalse(result["success"])
        self.assertIn("clone", result["error"])

    def test_local_without_ref_text_is_rejected_before_http(self):
        result = self.reject(cloud="local", ref_audio=str(self.ref),
                             ref_text=None)
        self.assertFalse(result["success"])
        self.assertIn("clone", result["error"])

    def test_local_design_instruct_is_rejected_before_http(self):
        result = self.reject(cloud="local", design_instruct="warm narrator",
                             ref_audio=str(self.ref), ref_text="参考")
        self.assertFalse(result["success"])
        self.assertIn("clone", result["error"])


class ModalUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_modal_clone_still_uploads_and_sends_url(self):
        captured = {}

        def fake_call(provider, payload, tool_name, timeout, **rest):
            captured["payload"] = payload
            return {"success": True, "outputs": [{
                "audio_base64": base64.b64encode(b"MP3DATA").decode("ascii"),
                "duration_seconds": 1.0,
            }]}, 0.5

        with mock.patch("cloud_gpu.call_cloud_endpoint",
                        side_effect=fake_call), \
                mock.patch.object(qwen3_tts, "upload_to_storage",
                                  return_value=("https://r2.example/ref.mp3",
                                                "qwen3-tts/input/ref.mp3")) as up, \
                mock.patch.object(qwen3_tts, "get_r2_payload_config",
                                  return_value=None):
            result = qwen3_tts.generate_audio(
                text="你好", output_path=str(self.out), ref_audio=str(self.ref),
                ref_text="参考", verbose=False, cloud="modal")
        self.assertTrue(result["success"])
        up.assert_called_once()
        payload = captured["payload"]["input"]
        self.assertEqual(payload["ref_audio_url"], "https://r2.example/ref.mp3")
        self.assertNotIn("ref_audio_base64", payload)


class CliArgsTests(unittest.TestCase):
    def test_model_is_a_free_string_and_cloud_accepts_local(self):
        argv = ["qwen3_tts.py", "--text", "hi", "--output", "o.mp3",
                "--cloud", "local", "--model", "qwen3-tts-1.7B",
                "--ref-audio", "r.mp3", "--ref-text", "t"]
        with mock.patch.object(sys, "argv", argv):
            args = qwen3_tts.parse_args()
        self.assertEqual(args.model, "qwen3-tts-1.7B")
        self.assertEqual(args.cloud, "local")

    def test_main_rejects_local_without_ref_before_http(self):
        argv = ["qwen3_tts.py", "--cloud", "local", "--text", "hi",
                "--output", "o.mp3"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                qwen3_tts.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("clone", err.getvalue())

    def test_main_rejects_local_design_instruct(self):
        argv = ["qwen3_tts.py", "--cloud", "local", "--text", "hi",
                "--output", "o.wav", "--design-instruct", "warm",
                "--ref-audio", "r.mp3", "--ref-text", "t"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                qwen3_tts.main()
        self.assertEqual(ctx.exception.code, 1)
```

One header change in this step: add `import tempfile` to the module's import block (after `import sys`); the new `setUp` methods use it directly.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py -v
```

Expected: FAIL: `TypeError: generate_audio() got an unexpected keyword argument 'model'`; local tests fail with R2-upload attempts or missing `ref_audio_base64` in payload; CLI tests fail because `--model`/local validation do not exist yet.

- [ ] **Step 3: Implement the qwen3_tts.py changes**

Six edits in `tools/qwen3_tts.py`:

(a) Signature: in `generate_audio(...)`, after the existing final parameter `max_wpm: float | None = None,` add `model: str | None = None`. Appending it after `max_wpm` preserves every existing positional argument, including `cloud`, `progress` and `max_wpm` itself.

(b) Local clone-only guard: directly after the mode-determination block:

```python
        # Determine mode
        if design_instruct is not None:
            if is_batch:
                return {"success": False, "error": "voice_design mode expects a single seed text, not a batch"}
            mode = "voice_design"
        elif ref_audio:
            mode = "clone"
        else:
            mode = "custom_voice"
```

insert:

```python
        # Local gateway is clone-only (P2): reject before any upload or HTTP.
        if cloud == "local" and mode != "clone":
            return {"success": False, "error": (
                "--cloud local supports voice clone only: pass --ref-audio and"
                f" --ref-text (requested mode: {mode})")}
```

(c) R2 config: replace:

```python
        # Get R2 config (used for file transfer regardless of cloud provider)
        r2_payload = get_r2_payload_config()
```

with:

```python
        # R2 config for cloud providers only; local never touches external storage
        r2_payload = None if cloud == "local" else get_r2_payload_config()
```

(d) Reference handling: replace:

```python
        # Upload reference audio for clone mode
        ref_audio_url = None
        if mode == "clone":
            if not Path(ref_audio).exists():
                return {"success": False, "error": f"Reference audio not found: {ref_audio}"}
            if not ref_text:
                return {"success": False, "error": "ref_text is required for voice cloning"}

            ref_audio_url, ref_r2_key = upload_to_storage(ref_audio, "qwen3-tts/input")
            if not ref_audio_url:
                return {"success": False, "error": "Failed to upload reference audio"}
            if ref_r2_key:
                r2_keys_to_cleanup.append(ref_r2_key)
```

with:

```python
        # Reference audio for clone mode: inline base64 locally, R2 upload on cloud
        ref_audio_url = None
        ref_audio_b64 = None
        if mode == "clone":
            if not Path(ref_audio).exists():
                return {"success": False, "error": f"Reference audio not found: {ref_audio}"}
            if not ref_text:
                return {"success": False, "error": "ref_text is required for voice cloning"}

            if cloud == "local":
                ref_audio_b64 = base64.b64encode(Path(ref_audio).read_bytes()).decode("ascii")
            else:
                ref_audio_url, ref_r2_key = upload_to_storage(ref_audio, "qwen3-tts/input")
                if not ref_audio_url:
                    return {"success": False, "error": "Failed to upload reference audio"}
                if ref_r2_key:
                    r2_keys_to_cleanup.append(ref_r2_key)
```

(e) Payload: replace:

```python
        if mode == "clone":
            payload["input"]["ref_audio_url"] = ref_audio_url
            payload["input"]["ref_text"] = ref_text
        elif mode == "voice_design":
```

with:

```python
        if mode == "clone":
            if cloud == "local":
                payload["input"]["ref_audio_base64"] = ref_audio_b64
            else:
                payload["input"]["ref_audio_url"] = ref_audio_url
            payload["input"]["ref_text"] = ref_text
        elif mode == "voice_design":
```

and after the `top_p` payload block:

```python
        if top_p is not None:
            payload["input"]["top_p"] = top_p
```

add:

```python
        if model is not None:
            payload["input"]["model"] = model
```

(f) Result download loop: replace:

```python
        # Download all outputs
        downloaded_items = []
        for i, (item, dest_path) in enumerate(zip(outputs_list, out_paths)):
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            downloaded = False

            r2_key = item.get("r2_key")
            url = item.get("audio_url")

            if r2_key:
                if verbose:
                    print(f"Downloading output {i+1}/{len(outputs_list)} from R2...", file=sys.stderr)
                downloaded = download_from_r2(r2_key, dest_path)
                if downloaded:
                    r2_keys_to_cleanup.append(r2_key)
                    if verbose:
                        size_kb = Path(dest_path).stat().st_size // 1024
                        print(f"  Downloaded: {dest_path} ({size_kb}KB)", file=sys.stderr)

            if not downloaded and url:
                downloaded = download_from_url(url, dest_path, verbose=verbose)
                # Same object the r2_key names -- register it however we fetched it.
                if downloaded and r2_key:
                    r2_keys_to_cleanup.append(r2_key)

            if not downloaded:
                audio_base64 = item.get("audio_base64")
                if audio_base64:
                    Path(dest_path).write_bytes(base64.b64decode(audio_base64))
                    downloaded = True
                    if verbose:
                        size_kb = Path(dest_path).stat().st_size // 1024
                        print(f"  Decoded from base64: {dest_path} ({size_kb}KB)", file=sys.stderr)

            if not downloaded:
                return {"success": False, "error": f"No audio in output[{i}]: {list(item.keys())}"}
```

with:

```python
        # Download all outputs
        downloaded_items = []
        for i, (item, dest_path) in enumerate(zip(outputs_list, out_paths)):
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            downloaded = False

            if cloud == "local":
                # Local results must be base64-only; any external reference
                # is a contract violation, not a download source.
                if item.get("r2_key") or item.get("audio_url"):
                    return {"success": False, "error": (
                        f"local gateway returned an external reference"
                        f" (r2_key/audio_url) for output[{i}]; local results"
                        " must be base64-only")}
                audio_base64 = item.get("audio_base64")
                if not audio_base64:
                    return {"success": False, "error": (
                        f"No audio_base64 in local output[{i}]:"
                        f" {list(item.keys())}")"}
                Path(dest_path).write_bytes(base64.b64decode(audio_base64))
                downloaded = True
                if verbose:
                    size_kb = Path(dest_path).stat().st_size // 1024
                    print(f"  Decoded from base64: {dest_path} ({size_kb}KB)", file=sys.stderr)
            else:
                r2_key = item.get("r2_key")
                url = item.get("audio_url")

                if r2_key:
                    if verbose:
                        print(f"Downloading output {i+1}/{len(outputs_list)} from R2...", file=sys.stderr)
                    downloaded = download_from_r2(r2_key, dest_path)
                    if downloaded:
                        r2_keys_to_cleanup.append(r2_key)
                        if verbose:
                            size_kb = Path(dest_path).stat().st_size // 1024
                            print(f"  Downloaded: {dest_path} ({size_kb}KB)", file=sys.stderr)

                if not downloaded and url:
                    downloaded = download_from_url(url, dest_path, verbose=verbose)
                    # Same object the r2_key names -- register it however we fetched it.
                    if downloaded and r2_key:
                        r2_keys_to_cleanup.append(r2_key)

                if not downloaded:
                    audio_base64 = item.get("audio_base64")
                    if audio_base64:
                        Path(dest_path).write_bytes(base64.b64decode(audio_base64))
                        downloaded = True
                        if verbose:
                            size_kb = Path(dest_path).stat().st_size // 1024
                            print(f"  Decoded from base64: {dest_path} ({size_kb}KB)", file=sys.stderr)

            if not downloaded:
                return {"success": False, "error": f"No audio in output[{i}]: {list(item.keys())}"}
```

(g) argparse `--model`: after the `--format` argument block add:

```python
    # Model passthrough (free string; the provider validates)
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name passed through to the provider (e.g. qwen3-tts-1.7B). Default: provider default.",
    )
```

(h) `main()` local validation: after the existing mode conflict check:

```python
    # Mode conflict checks
    if args.design_instruct and args.ref_audio:
        print("Error: --design-instruct and --ref-audio are mutually exclusive", file=sys.stderr)
        sys.exit(1)
```

insert:

```python
    # Local gateway is clone-only (P2): validate before any HTTP request.
    if args.cloud == "local":
        if args.design_instruct:
            print(
                "Error: --cloud local supports voice clone only;"
                " --design-instruct needs a cloud provider",
                file=sys.stderr,
            )
            sys.exit(1)
        if not (args.ref_audio and args.ref_text):
            print(
                "Error: --cloud local supports voice clone only:"
                " --ref-audio and --ref-text are both required",
                file=sys.stderr,
            )
            sys.exit(1)
```

(i) `main()` call site: add `model=args.model,` to the `generate_audio(...)` call, next to `cloud=args.cloud,`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_qwen3_tts_local.py tests/python/test_cloud_gpu_local.py -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
```

Expected: PASS (full root suite confirms no regression in other tools).

- [ ] **Step 5: Commit**

```bash
git add tools/qwen3_tts.py tests/python/test_qwen3_tts_local.py
git commit -m "FEAT: qwen3_tts local clone provider"
```

---

### Task 7: voiceover local clone with qwen model and timeout passthrough

**Files:**
- Modify: `tools/voiceover.py`
- Test: `tests/python/test_voiceover_local.py` (create)

**Interfaces:**
- Consumes: Task 6's `qwen3_tts.generate_audio(..., model=None)` (called via `from qwen3_tts import generate_audio` inside the voiceover helper functions; patch target `qwen3_tts.generate_audio`).
- Produces:
  - `voiceover.local_clone_error(provider: str, cloud: str, ref_audio, ref_text, brand_design: bool = False) -> str | None`
  - `generate_single_audio_qwen3(..., model: str | None = None, timeout: int = 300)`: forwards `model=` and `timeout=` to `qwen3_tts.generate_audio`
  - `generate_batch_audio_qwen3(..., model: str | None = None, timeout: int = 300)` (the old `timeout_per_scene: int = 120` parameter is gone)
  - `process_scene_directory(..., qwen_model: str | None = None, timeout: int = 300)`
  - argparse: `--cloud` accepts `local`; new `--qwen-model` (free string, default None); new `--timeout` (int, default 300); `--model` stays ElevenLabs-only.

- [ ] **Step 1: Write the failing tests**

Create `tests/python/test_voiceover_local.py`:

```python
"""voiceover local clone: guards, --qwen-model/--timeout passthrough.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_local.py -v

The qwen3_tts generator is faked; no network, no ElevenLabs SDK.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402
import voiceover  # noqa: E402


class LocalCloneErrorTests(unittest.TestCase):
    def test_cloud_providers_are_unchanged(self):
        self.assertIsNone(voiceover.local_clone_error(
            "qwen3", "modal", None, None))
        self.assertIsNone(voiceover.local_clone_error(
            "qwen3", "runpod", None, None))

    def test_elevenlabs_is_unchanged_even_on_local(self):
        self.assertIsNone(voiceover.local_clone_error(
            "elevenlabs", "local", None, None))

    def test_local_without_ref_audio_or_ref_text_is_clone_only_error(self):
        message = voiceover.local_clone_error("qwen3", "local", None, None)
        self.assertIsNotNone(message)
        self.assertIn("clone", message)
        self.assertIn("--ref-audio", message)
        self.assertIn("--ref-text", message)

    def test_local_with_only_ref_audio_is_still_an_error(self):
        self.assertIsNotNone(voiceover.local_clone_error(
            "qwen3", "local", "ref.mp3", None))

    def test_local_with_both_refs_is_fine(self):
        self.assertIsNone(voiceover.local_clone_error(
            "qwen3", "local", "ref.mp3", "transcript"))

    def test_brand_design_on_local_is_rejected(self):
        message = voiceover.local_clone_error(
            "qwen3", "local", None, None, brand_design=True)
        self.assertIsNotNone(message)
        self.assertIn("design", message.lower())


class QwenPassthroughTests(unittest.TestCase):
    def capture_generate(self):
        captured = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return {"success": True, "output": "out.mp3",
                    "duration_seconds": 1.5}

        return captured, mock.patch.object(qwen3_tts, "generate_audio",
                                           side_effect=fake_generate)

    def test_single_passes_model_and_timeout(self):
        captured, patcher = self.capture_generate()
        with patcher:
            result = voiceover.generate_single_audio_qwen3(
                script="hi", output_path=Path("out.mp3"),
                ref_audio="ref.mp3", ref_text="t", cloud="local",
                model="qwen3-tts-1.7B", timeout=300)
        self.assertTrue(result["success"])
        self.assertEqual(captured["model"], "qwen3-tts-1.7B")
        self.assertEqual(captured["timeout"], 300)
        self.assertEqual(captured["cloud"], "local")
        self.assertEqual(captured["ref_audio"], "ref.mp3")

    def test_single_defaults_match_the_gateway_formula(self):
        captured, patcher = self.capture_generate()
        with patcher:
            voiceover.generate_single_audio_qwen3(
                script="hi", output_path=Path("out.mp3"),
                ref_audio="ref.mp3", ref_text="t", cloud="local")
        self.assertEqual(captured["timeout"], 300)
        self.assertIsNone(captured["model"])

    def test_batch_default_timeout_is_300_not_120(self):
        captured, patcher = self.capture_generate()
        with patcher:
            results = voiceover.generate_batch_audio_qwen3(
                scripts=["a", "b"],
                output_paths=[Path("a.mp3"), Path("b.mp3")],
                ref_audio="ref.mp3", ref_text="t", cloud="local")
        self.assertEqual(len(results), 2)
        self.assertEqual(captured["timeout"], 300)
        self.assertEqual(captured["text"], ["a", "b"])

    def test_batch_passes_model_and_timeout(self):
        captured, patcher = self.capture_generate()
        with patcher:
            voiceover.generate_batch_audio_qwen3(
                scripts=["a"], output_paths=[Path("a.mp3")],
                ref_audio="ref.mp3", ref_text="t", cloud="local",
                model="qwen3-tts-1.7B", timeout=420)
        self.assertEqual(captured["model"], "qwen3-tts-1.7B")
        self.assertEqual(captured["timeout"], 420)


    def test_single_helper_rejects_missing_refs_before_generator(self):
        captured, patcher = self.capture_generate()
        with patcher as generator:
            result = voiceover.generate_single_audio_qwen3(
                script="hi", output_path=Path("out.mp3"), cloud="local")
        self.assertFalse(result["success"])
        generator.assert_not_called()

    def test_batch_helper_rejects_missing_refs_before_generator(self):
        captured, patcher = self.capture_generate()
        with patcher as generator:
            results = voiceover.generate_batch_audio_qwen3(
                scripts=["a"], output_paths=[Path("a.mp3")], cloud="local")
        self.assertFalse(results[0]["success"])
        generator.assert_not_called()

    def test_scene_directory_helper_rejects_missing_refs_before_io(self):
        captured, patcher = self.capture_generate()
        with patcher as generator:
            results, _, _ = voiceover.process_scene_directory(
                Path("directory-that-must-not-be-read"), cloud="local")
        self.assertFalse(results[0]["success"])
        generator.assert_not_called()


class VoiceoverArgsTests(unittest.TestCase):
    def parse(self, argv):
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv):
            return voiceover.parse_args()

    def test_qwen_model_is_free_string_and_timeout_defaults_300(self):
        args = self.parse(["--provider", "qwen3", "--script", "s.txt",
                           "--output", "o.mp3"])
        self.assertIsNone(args.qwen_model)
        self.assertEqual(args.timeout, 300)
        self.assertEqual(args.cloud, "modal")

    def test_cloud_accepts_local_and_custom_timeout(self):
        args = self.parse(["--provider", "qwen3", "--cloud", "local",
                           "--qwen-model", "qwen3-tts-1.7B", "--timeout",
                           "420", "--script", "s.txt", "--output", "o.mp3",
                           "--ref-audio", "r.mp3", "--ref-text", "t"])
        self.assertEqual(args.cloud, "local")
        self.assertEqual(args.qwen_model, "qwen3-tts-1.7B")
        self.assertEqual(args.timeout, 420)

    def test_elevenlabs_model_argument_is_unchanged(self):
        args = self.parse(["--model", "eleven_v4", "--script", "s.txt",
                           "--output", "o.mp3"])
        self.assertEqual(args.model, "eleven_v4")


class MainGuardTests(unittest.TestCase):
    def run_main(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv), \
                mock.patch("sys.stderr", err):
            try:
                voiceover.main()
            except SystemExit as e:
                return e.code, err.getvalue()
        self.fail("main() must exit for these local misuse cases")

    def test_local_without_refs_exits_1_with_clone_only_error(self):
        code, text = self.run_main([
            "--provider", "qwen3", "--cloud", "local", "--script",
            "does-not-need-to-exist.txt", "--output", "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("clone", text)

    def test_local_design_flag_style_is_covered_by_guard(self):
        # Builtin speaker (no refs) on local is a clone-only error too.
        code, text = self.run_main([
            "--provider", "qwen3", "--cloud", "local", "--speaker", "Ryan",
            "--script", "s.txt", "--output", "o.mp3"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_local.py -v
```

Expected: FAIL: `AttributeError: module 'voiceover' has no attribute 'local_clone_error'`; `TypeError` on `model=`/`timeout=` kwargs; argparse rejects `--qwen-model`/`--cloud local`.

- [ ] **Step 3: Implement the voiceover.py changes**

Seven edits in `tools/voiceover.py`:

(a) `--cloud` argument: replace:

```python
    parser.add_argument(
        "--cloud",
        type=str,
        default="modal",
        choices=["runpod", "modal"],
        help="Cloud GPU provider for Qwen3-TTS (default: modal)",
    )
```

with:

```python
    parser.add_argument(
        "--cloud",
        type=str,
        default="modal",
        choices=["runpod", "modal", "local"],
        help="Cloud GPU provider for Qwen3-TTS (default: modal; local ="
             " loopback gateway, voice clone only)",
    )
```

(b) New arguments right after the `--top-p` block:

```python
    parser.add_argument(
        "--qwen-model",
        type=str,
        default=None,
        help="Qwen3-TTS model name passed through on every qwen3 path"
             " (e.g. qwen3-tts-1.7B). The gateway validates it; default"
             " omits the field.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Qwen3-TTS per-job timeout in seconds; batches scale it by"
             " count, matching the gateway (default: 300)",
    )
```

(c) Guard helper: add after `resolve_model_id` (top-level function):

```python
def local_clone_error(provider: str, cloud: str, ref_audio, ref_text,
                      brand_design: bool = False) -> str | None:
    """CLI error message when a qwen3 local call is not clone-only, else None.

    The local gateway implements voice clone only (P2): built-in speakers
    and voice design stay cloud features. ElevenLabs and cloud qwen3 are
    never affected.
    """
    if provider != "qwen3" or cloud != "local":
        return None
    if brand_design:
        return (
            "--cloud local supports voice clone only; brand clone.design"
            " (VoiceDesign) is not available on the local gateway. Provide"
            " --ref-audio/--ref-text, or drop --cloud local for this voice."
        )
    if not ref_audio or not ref_text:
        return (
            "--cloud local supports voice clone only: --ref-audio and"
            " --ref-text are both required (built-in speakers and voice"
            " design are cloud-only)."
        )
    return None
```

(d) `generate_single_audio_qwen3`: replace:

```python
def generate_single_audio_qwen3(
    script: str,
    output_path: Path,
    speaker: str = "Ryan",
    language: str = "Auto",
    instruct: str = "",
    ref_audio: str | None = None,
    ref_text: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    cloud: str = "runpod",
    max_wpm: float | None = None,
    progress=None,
) -> dict:
    """Generate a single audio file from script text using Qwen3-TTS. Returns result dict."""
    from qwen3_tts import generate_audio

    output_path.parent.mkdir(parents=True, exist_ok=True)

    return generate_audio(
        text=script,
        output_path=str(output_path),
        speaker=speaker,
        language=language,
        instruct=instruct,
        ref_audio=ref_audio,
        ref_text=ref_text,
        verbose=False,
        temperature=temperature,
        top_p=top_p,
        cloud=cloud,
        max_wpm=max_wpm,
        progress=progress,
    )
```

with:

```python
def generate_single_audio_qwen3(
    script: str,
    output_path: Path,
    speaker: str = "Ryan",
    language: str = "Auto",
    instruct: str = "",
    ref_audio: str | None = None,
    ref_text: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    cloud: str = "runpod",
    max_wpm: float | None = None,
    model: str | None = None,
    timeout: int = 300,
    progress=None,
) -> dict:
    """Generate a single audio file from script text using Qwen3-TTS. Returns result dict."""
    error = local_clone_error("qwen3", cloud, ref_audio, ref_text)
    if error:
        return {"success": False, "error": error}

    from qwen3_tts import generate_audio

    output_path.parent.mkdir(parents=True, exist_ok=True)

    return generate_audio(
        text=script,
        output_path=str(output_path),
        speaker=speaker,
        language=language,
        instruct=instruct,
        ref_audio=ref_audio,
        ref_text=ref_text,
        verbose=False,
        temperature=temperature,
        top_p=top_p,
        cloud=cloud,
        model=model,
        timeout=timeout,
        max_wpm=max_wpm,
        progress=progress,
    )
```

(e) `generate_batch_audio_qwen3`: replace its signature and call:

```python
def generate_batch_audio_qwen3(
    scripts: list[str],
    output_paths: list[Path],
    speaker: str = "Ryan",
    language: str = "Auto",
    ref_audio: str | None = None,
    ref_text: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    cloud: str = "runpod",
    max_wpm: float | None = None,
    model: str | None = None,
    timeout: int = 300,
    progress=None,
) -> list[dict]:
```

At the start of the function, before importing `qwen3_tts`, creating directories or doing any batch work, add the same direct-helper guard used by the single-file path:

```python
    error = local_clone_error("qwen3", cloud, ref_audio, ref_text)
    if error:
        return [{"success": False, "error": error}]
```

Then, inside, replace `timeout=timeout_per_scene,` with `timeout=timeout,` and add `model=model,` next to `cloud=cloud,` in the `generate_audio(...)` call. The old parameter `timeout_per_scene: int = 120` is deleted (the default is now 300, matching the gateway; the docstring line "timeout_per_scene" mentions stay out).

(f) `process_scene_directory`: keep the existing ElevenLabs `model` parameter unchanged and add these separate parameters after `max_wpm: float | None = None` and before `progress=None`, preserving the existing positional order:

```python
    qwen_model: str | None = None,
    timeout: int = 300,
```

At the very start of the function, before scanning the directory, add:

```python
    error = local_clone_error("qwen3", cloud, ref_audio, ref_text)
    if error:
        return [{"success": False, "error": error}], 0.0, 0
```

Forward them: in the `generate_batch_audio_qwen3(...)` call add `model=qwen_model, timeout=timeout,`; in the serial `generate_single_audio_qwen3(...)` call (the `provider == "qwen3"` branch) add `model=qwen_model, timeout=timeout,`. Do not pass `qwen_model` to ElevenLabs.

(g) `main()` guards and threading. First, the brand VoiceDesign branch: the block currently reads `if "design" in clone_cfg:` followed by two comment lines, then `design = clone_cfg["design"]`. Insert the local refusal between the comments and the `design = ...` line so the block becomes exactly:

```python
                if "design" in clone_cfg:
                    # (keep the branch's existing two comment lines as they are)
                    # The local gateway is clone-only (P2): refuse instead of
                    # silently generating a cloud reference.
                    design_guard = local_clone_error(
                        provider, args.cloud, None, None, brand_design=True)
                    if design_guard:
                        print(f"Error: {design_guard}", file=sys.stderr)
                        sys.exit(1)
                    design = clone_cfg["design"]
```

Second, the general guard: insert right before the `# Provider-specific setup` comment:

```python
    # Local gateway is clone-only (P2): every qwen3 local path (single,
    # scene-dir, batch, brand) must carry a reference voice.
    if provider == "qwen3":
        guard = local_clone_error(provider, args.cloud, args.ref_audio,
                                  args.ref_text)
        if guard:
            print(f"Error: {guard}", file=sys.stderr)
            sys.exit(1)
```

Third, thread the new options into the generation calls: in both `process_scene_directory(...)` call sites in `main()` (the dry-run one and the real one) add `model=args.qwen_model, timeout=args.timeout,` after `cloud=args.cloud,`; and in the single-file `generate_single_audio_qwen3(...)` call add `model=args.qwen_model, timeout=args.timeout,` after `cloud=args.cloud,`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_local.py -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
```

Expected: PASS (full suite confirms brand-voice and other tools are unchanged).

- [ ] **Step 5: Commit**

```bash
git add tools/voiceover.py tests/python/test_voiceover_local.py
git commit -m "FEAT: voiceover local clone with qwen model and timeout passthrough"
```

---

### Task 8: health dependencies, fallback factory, object_info dedupe; README and roadmap docs

**Files:**
- Modify: `local-gateway/local_gateway/health.py`
- Modify: `local-gateway/tests/test_health.py`
- Modify: `local-gateway/README.md`
- Modify: `docs/local-comfyui-provider-roadmap.md`

**Interfaces:**
- Consumes: Task 3 registry clone entry; existing health `_build` structure.
- Produces: `/health` payload gains `"dependencies": {"ffmpeg": bool, "ffprobe": bool}`; missing tools produce `dependency_missing` errors and force global `ready=False`; route `available` additionally requires both tools; the internal-error fallback builds each model's skeleton from a factory (no shared lists); `IMPLEMENTED_ROUTES` remains `frozenset()` until Task 9.

- [ ] **Step 1: Write the failing tests**

In `local-gateway/tests/test_health.py`, first make the aggregation hermetic: add to `HealthAggregationTests` and `ServerTests` a `setUp`:

```python
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)
```

(`mock` is already imported at module top.) Then append to `HealthAggregationTests`:

```python
    def test_dependencies_block_reports_both_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            client = FakeClient(object_info=object_info_fixture())
            with mock.patch("shutil.which", return_value="/fake/bin/ffmpeg"):
                payload, status = health.build_health_payload(
                    self.settings_for(root), lambda url: client)
        self.assertEqual(status, 200)
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": True})
        self.assertTrue(payload["ready"])

    def test_missing_ffprobe_is_dependency_missing_and_blocks_ready(self):
        def fake_which(name):
            return "/fake/bin/ffmpeg" if name == "ffmpeg" else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            client = FakeClient(object_info=object_info_fixture())
            with mock.patch("shutil.which", side_effect=fake_which):
                payload, status = health.build_health_payload(
                    self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": False})
        self.assertTrue(payload["models_ready"])  # models describe weights only
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("dependency_missing", codes)
        action = next(e for e in payload["errors"]
                      if e["code"] == "dependency_missing")["action"]
        self.assertIn("ffprobe", action)
        self.assertTrue(all(not r["available"] for r in payload["routes"].values()))

    def test_fallback_skeleton_lists_are_independent_per_model(self):
        def boom(url):
            raise ZeroDivisionError("division by zero")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, _ = health.build_health_payload(self.settings_for(root), boom)
        models = list(payload["models"].values())
        self.assertGreaterEqual(len(models), 2)
        first, second = models[0], models[1]
        self.assertIsNot(first["missing_files"], second["missing_files"])
        first["missing_files"].append("leak")
        self.assertEqual(second["missing_files"], [])
        self.assertIn("dependencies", payload)

    def test_object_info_is_fetched_once_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(object_info=object_info_fixture())
            health.build_health_payload(self.settings_for(root),
                                        lambda url: client)
        self.assertTrue(client.object_info_calls)
        self.assertEqual(len(client.object_info_calls),
                         len(set(client.object_info_calls)))
        self.assertEqual(set(client.object_info_calls),
                         set(registry.required_nodes()))
```

And update the route-status assertion in `test_route_targets_and_empty_implemented_set`: keep it as-is for now (it asserts `IMPLEMENTED_ROUTES == frozenset()`); Task 9 is the only place allowed to change it. Add a comment line directly above that assertion:

```python
        # Task 9 (real acceptance) flips this to frozenset({"tts"}) and
        # updates this assertion in the same commit.
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: FAIL: `KeyError: 'dependencies'` in the new tests (payload has no dependencies block), and `test_dependencies_block_reports_both_tools` fails at the equality assert.

- [ ] **Step 3: Implement the health.py changes**

In `local-gateway/local_gateway/health.py`:

(a) Add `import shutil` to the stdlib imports.

(b) Replace the skeleton constant:

```python
_MODEL_SKELETON = {
    "ready": False,
    "files_ready": False,
    "nodes_ready": False,
    "optional_files_present": [],
    "missing_files": [],
    "missing_nodes": [],
    "missing_loader_values": [],
}
```

with a factory:

```python
def _model_skeleton() -> dict:
    """Fresh skeleton per model: every list is a new object, so one model's
    fallback can never mutate another's (no shared shallow-copied lists)."""
    return {
        "ready": False,
        "files_ready": False,
        "nodes_ready": False,
        "optional_files_present": [],
        "missing_files": [],
        "missing_nodes": [],
        "missing_loader_values": [],
    }


def _tool_dependencies() -> dict:
    """ffmpeg/ffprobe availability in this gateway process's PATH."""
    return {"ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None}
```

(c) In `build_health_payload`'s exception fallback, replace:

```python
            "models": {entry.key: dict(_MODEL_SKELETON)
                       for entry in registry.MODEL_ENTRIES},
```

with:

```python
            "models": {entry.key: _model_skeleton()
                       for entry in registry.MODEL_ENTRIES},
            "dependencies": _tool_dependencies(),
```

(d) In `_build`, after the `errors = [...]` first line add:

```python
    dependencies = _tool_dependencies()
    for tool, present in dependencies.items():
        if not present:
            errors.append(_err(
                "dependency_missing", "gateway",
                f"{tool} is not available on this gateway process's PATH.",
                f"Install {tool} (ffmpeg/ffprobe ship together) and restart"
                f" the gateway; voice clone requires it for probing and WAV"
                f" transcode.",
            ))
```

(e) In `_build`, change the routes computation and `ready`: replace:

```python
    models_ready = all(m["ready"] for m in models.values())
    routes = {
        route: {
            "implemented": route in registry.IMPLEMENTED_ROUTES,
            "available": (route in registry.IMPLEMENTED_ROUTES
                          and models[registry.ROUTE_TARGETS[route]]["ready"]),
        }
        for route in registry.ROUTE_TARGETS
    }
    ready = settings.config_valid and comfyui["reachable"] and models_ready
```

with:

```python
    models_ready = all(m["ready"] for m in models.values())
    dependencies_ready = all(dependencies.values())
    routes = {
        route: {
            "implemented": route in registry.IMPLEMENTED_ROUTES,
            "available": (route in registry.IMPLEMENTED_ROUTES
                          and models[registry.ROUTE_TARGETS[route]]["ready"]
                          and dependencies_ready),
        }
        for route in registry.ROUTE_TARGETS
    }
    ready = (settings.config_valid and comfyui["reachable"]
             and models_ready and dependencies_ready)
```

(f) In `_build`'s payload dict, add `"dependencies": dependencies,` after the `"models": models,` line.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: PASS, all gateway tests.

- [ ] **Step 5: Update the gateway README**

In `local-gateway/README.md`, replace the opening paragraph:

```markdown
A loopback FastAPI gateway in front of a local ComfyUI install. This is the
P1 walking skeleton: `GET /health` only. The four generation routes
(`/tts/generate`, `/image/generate`, `/image/edit`, `/video/generate`) are
not implemented yet, and `/health` reports each of them as
`implemented: false`.
```

with:

```markdown
A loopback FastAPI gateway in front of a local ComfyUI install. P1 provides
`GET /health`; P2 adds the first generation route, `POST /tts/generate`,
which supports voice clone only (mode `clone`, `mp3` or `wav`, reference
audio inline as `ref_audio_base64`, results inline as `audio_base64`).
Custom voice and voice design are not implemented on this gateway. The
image and video routes stay `implemented: false` until later phases.
```

And append a new section before `## Tests`:

```markdown
## TTS voice clone

Point the toolkit at the route (root `.env`):

```
LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate
```

Then, for example:

```bash
uv run tools/qwen3_tts.py --cloud local --text "Hello" \
  --ref-audio ref.mp3 --ref-text "the transcript" --output out.mp3
```

Requirements beyond ComfyUI and the Qwen3-TTS weights:

- `ffmpeg` and `ffprobe` must be on the gateway process's PATH; `/health`
  reports both under `dependencies` and the gateway is not `ready` without
  them.
- The `Qwen3-TTS-12Hz-1.7B-Base` and `Qwen3-TTS-Tokenizer-12Hz` directories
  under `COMFYUI_MODELS_ROOT/TTS/` must exist and be non-empty.
- One clone request runs at a time per gateway process (serialized on
  purpose: threads, batches and VRAM compete); `/health` stays responsive.
```

- [ ] **Step 6: Rewrite the roadmap P2 section as clone-only**

In `docs/local-comfyui-provider-roadmap.md`:

(a) In the phase table, replace the P2 row:

```markdown
| **P2** TTS 路由 | `/tts/generate`、三种 TTS 模式、`qwen3_tts.py` 与 `voiceover.py` 接 local | custom_voice、voiceover、voice_design 均产出非空音频；local clone 不触发外部上传 | M |
```

with:

```markdown
| **P2** TTS 路由 | `/tts/generate`、clone 单模式、`qwen3_tts.py` 与 `voiceover.py` 接 local | clone MP3、clone WAV、voiceover clone 三项真实生成通过；local clone 不触发外部上传 | M |
```

(b) Replace the whole `### P2 TTS 路由` section (checkbox list plus its acceptance code block, from the `- [ ] **开工第一步**` line down to the paragraph ending `未跑时文档不得写成已实测。`) with:

```markdown
### P2 TTS 路由（clone-only 修订）

依据 2026-11-17 clone-only 设计规格（docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md）执行，CustomVoice 与 VoiceDesign 移出本期。

- [ ] **开工第一步**：用 `/object_info` 校验 `AILab_Qwen3TTSVoiceClone_Advanced`、`LoadAudio`、`SaveAudioAdvanced` 及真实参数。校验不过，不写路由。
- [ ] `POST /tts/generate`：仅 `mode=clone`；注册表首条目 `qwen3-tts-1.7B`，固定 `model_size=1.7B`，`modes=(clone,)`，`output_formats=(mp3, wav)`。非 clone 返回 422 `mode_unsupported`；`ref_audio_url`/`r2` 返回 422 `external_url_unsupported`。
- [ ] 模板原创生成，不复制第三方 example workflow JSON。MP3 由 ComfyUI 直接输出（`format="mp3"` + `format.quality="V0"`）；WAV 由 FLAC 中间文件用 FFmpeg `pcm_s16le` 真实转码，不改扩展名伪装格式。
- [ ] clone：`ref_audio_base64` 上传至 ComfyUI input，再由 `LoadAudio` 提供 AUDIO；每请求上传一次，批量逐项有序执行，任一失败无部分结果；deadline 为 `270 * max(1, item_count // 3 + 1)`，覆盖 lock、上传、submit/poll/view/ffprobe/FFmpeg；单进程 lock 串行化。
- [ ] `tools/qwen3_tts.py`：`--cloud` 加 local，新增自由字符串 `--model` 并传入 payload。**local clone 直接编码参考音频，跳过 `upload_to_storage()` 和结果 R2 上传**；云路径保持原行为；`--setup --cloud local` 退出码 2。
- [ ] `tools/voiceover.py`：`--cloud` 加 local，新增 `--qwen-model` 透传 Qwen 变体，新增默认 300 的 `--timeout`；local 所有 clone 路径必须 `--ref-audio`+`--ref-text`；brand VoiceDesign 与内置 speaker 在 local 下拒绝。现有 `--model` 是 ElevenLabs 专用，不复用。
- [ ] 单测：模板补丁、output format、model/timeout 透传、local clone base64 分支、外部上传下载函数零调用、网关错误契约、health dependencies 与 fallback 工厂。全部 fake 网络。

验收（三项真实 clone 生成，素材见规格第 14 节）：

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p2
REF_AUDIO='C:/Users/kenyo/Downloads/Music/voice_02.mp3'
REF_TEXT='为什么不用suno及类似的音乐生成平台? 因为贵，对游戏需要的音乐生成不友好，而且suno这种直接生成成曲的基本无法对成品做调整'

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "这段声音是在本地网关生成的克隆音频，用来确认参考音频上传和声音克隆已经完整跑通。" \
  --ref-audio "$REF_AUDIO" --ref-text "$REF_TEXT" \
  --output scratch/local-comfyui/p2/clone.mp3

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "这是同一条本地克隆链路生成的 WAV 文件，用来验证 FLAC 到 WAV 的真实转码过程。" \
  --ref-audio "$REF_AUDIO" --ref-text "$REF_TEXT" --format wav \
  --output scratch/local-comfyui/p2/clone.wav

printf '%s\n' '测试 voiceover 本地克隆链路' > scratch/local-comfyui/p2/voiceover.txt
uv run tools/voiceover.py --provider qwen3 --cloud local --qwen-model qwen3-tts-1.7B \
  --script scratch/local-comfyui/p2/voiceover.txt \
  --ref-audio "$REF_AUDIO" --ref-text "$REF_TEXT" \
  --output scratch/local-comfyui/p2/voiceover.mp3
```

三次生成都须成功，ffprobe 时长大于 0，clone.wav 编码为 `pcm_s16le`；最终 `/health` 断言见规格第 14.3 节。checkbox 只能在三项生成、health 与完整回归全部通过后勾选。
```

(c) In the same roadmap's P0 acceptance block, extend the copy lint arguments to all four design documents, matching the amended P0/P1 spec and the actual first commit:

```bash
uv run tools/copy_lint.py \
  docs/local-comfyui-provider-plan.md \
  docs/local-comfyui-provider-roadmap.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md
```

Do not change any checked P0 item's meaning; this only closes the deferred two-document lint mismatch.

Verify both documents lint clean:

```bash
uv run tools/copy_lint.py local-gateway/README.md docs/local-comfyui-provider-roadmap.md
```

Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add local-gateway/local_gateway/health.py local-gateway/tests/test_health.py
git commit -m "FEAT: health tool dependencies and independent model fallbacks"
git add local-gateway/README.md docs/local-comfyui-provider-roadmap.md
git commit -m "DOCS: document the local TTS clone route and clone-only P2 acceptance"
```

---

### Task 9: Real-machine acceptance, IMPLEMENTED_ROUTES flip, checkboxes and final regression

**Files:**
- Modify: `local-gateway/local_gateway/registry.py` (one line: `IMPLEMENTED_ROUTES`)
- Modify: `local-gateway/tests/test_health.py` (the one assertion from Task 8)
- Modify: `docs/local-comfyui-provider-roadmap.md` (tick the P2 checkboxes)

**Interfaces:**
- Consumes: everything from Tasks 1-8; the real machine: ComfyUI 0.45.0 on `127.0.0.1:9188` with the AILab Qwen3-TTS custom nodes and weights, `local-gateway/.env` with `COMFYUI_MODELS_ROOT`, ffmpeg/ffprobe on PATH, the reference MP3 `C:/Users/kenyo/Downloads/Music/voice_02.mp3`.
- Produces: `IMPLEMENTED_ROUTES = frozenset({"tts"})`; `/health` reporting `routes.tts == {"implemented": true, "available": true}`; the three real artifacts under `scratch/local-comfyui/p2/` (never committed); ticked P2 checkboxes.

- [ ] **Step 1: Pre-flight the real machine (read-only)**

In separate terminals / one at a time, confirm before generating anything:

```bash
set -euo pipefail
curl --noproxy '*' --fail http://127.0.0.1:9188/system_stats >/dev/null && echo "ComfyUI up"
command -v ffmpeg
command -v ffprobe
test -s 'C:/Users/kenyo/Downloads/Music/voice_02.mp3' && echo "ref audio present"
MODELS_ROOT="$(grep -E '^COMFYUI_MODELS_ROOT=' local-gateway/.env | cut -d= -f2)"
test -n "$(ls "$MODELS_ROOT/TTS/Qwen3-TTS-12Hz-1.7B-Base")" && echo "Base weights non-empty"
test -n "$(ls "$MODELS_ROOT/TTS/Qwen3-TTS-Tokenizer-12Hz")" && echo "Tokenizer non-empty"
curl --noproxy '*' --fail "http://127.0.0.1:9188/object_info/AILab_Qwen3TTSVoiceClone_Advanced" | head -c 400
```

Expected: every check echoes/prints. If the weights directories sit at a different parent than `TTS/`, stop and reconcile `registry.required_nonempty_directories` with the real layout (edit the two registry strings and the Task 3 test strings together, re-run the gateway suite) before continuing; do not proceed with a preflight that can never pass.

- [ ] **Step 2: Start the gateway**

In a dedicated terminal:

```bat
local-gateway\start_gateway.bat
```

Keep it running for the rest of the task. Sanity check:

```bash
curl --noproxy '*' --fail-with-body http://127.0.0.1:8123/health > scratch/local-comfyui/p2/health-pre.json
TEMP='E:\GitHub' TMP='E:\GitHub' uv run python -I -c '
import json
d = json.load(open("scratch/local-comfyui/p2/health-pre.json", encoding="utf-8"))
print(json.dumps({k: d[k] for k in ("ready", "models_ready", "dependencies", "routes")}, indent=2))
'
```

Expected: `ready=true` (if false, follow `errors[].action` before generating), `dependencies == {"ffmpeg": true, "ffprobe": true}`, all four routes `implemented=false` at this point.

- [ ] **Step 3: Run the three real clone generations**

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p2
REF_AUDIO='C:/Users/kenyo/Downloads/Music/voice_02.mp3'
REF_TEXT='为什么不用suno及类似的音乐生成平台? 因为贵，对游戏需要的音乐生成不友好，而且suno这种直接生成成曲的基本无法对成品做调整'

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "这段声音是在本地网关生成的克隆音频，用来确认参考音频上传和声音克隆已经完整跑通。" \
  --ref-audio "$REF_AUDIO" --ref-text "$REF_TEXT" \
  --output scratch/local-comfyui/p2/clone.mp3

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "这是同一条本地克隆链路生成的 WAV 文件，用来验证 FLAC 到 WAV 的真实转码过程。" \
  --ref-audio "$REF_AUDIO" --ref-text "$REF_TEXT" --format wav \
  --output scratch/local-comfyui/p2/clone.wav

printf '%s\n' '测试 voiceover 本地克隆链路' > scratch/local-comfyui/p2/voiceover.txt
uv run tools/voiceover.py --provider qwen3 --cloud local --qwen-model qwen3-tts-1.7B \
  --script scratch/local-comfyui/p2/voiceover.txt \
  --ref-audio "$REF_AUDIO" --ref-text "$REF_TEXT" \
  --output scratch/local-comfyui/p2/voiceover.mp3
```

Expected: all three commands exit 0 and print their generated file.

- [ ] **Step 4: Assert codecs and durations on the three artifacts**

```bash
set -euo pipefail
for name in clone voiceover; do
  file="scratch/local-comfyui/p2/$name.mp3"
  test -s "$file"
  codec="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$file")"
  test "$codec" = "mp3"
  duration="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file")"
  uv run python -I -c 'import sys; assert float(sys.argv[1]) > 0' "$duration"
done

file=scratch/local-comfyui/p2/clone.wav
test -s "$file"
test "$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$file")" = "pcm_s16le"
duration="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file")"
uv run python -I -c 'import sys; assert float(sys.argv[1]) > 0' "$duration"
```

Expected: every assertion passes (MP3s are `mp3` with duration > 0; the WAV is `pcm_s16le` with duration > 0).

- [ ] **Step 5: Flip IMPLEMENTED_ROUTES to tts**

In `local-gateway/local_gateway/registry.py`, replace:

```python
# Routes whose POST endpoint actually works. P1 implements none of them; a
# later phase flips a route only after its real generation acceptance passes.
IMPLEMENTED_ROUTES: frozenset[str] = frozenset()
```

with:

```python
# Routes whose POST endpoint actually works. tts flipped after the three real
# clone generations, health and full regression of the P2 acceptance passed;
# image and video stay false until their own phases.
IMPLEMENTED_ROUTES: frozenset[str] = frozenset({"tts"})
```

In `local-gateway/tests/test_health.py`, update the assertion flagged in Task 8: replace:

```python
        # Task 9 (real acceptance) flips this to frozenset({"tts"}) and
        # updates this assertion in the same commit.
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset())
```

with:

```python
        # Flipped to tts only after the real P2 clone acceptance (Task 9).
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset({"tts"}))
```

Also update the two route-status assertions that hard-code all-false (`test_full_ready_returns_200_with_whitelisted_fields` and `test_health_200_when_ready`): in both, replace:

```python
        self.assertEqual(payload["routes"], {
            "tts": {"implemented": False, "available": False},
            "image_generate": {"implemented": False, "available": False},
            "image_edit": {"implemented": False, "available": False},
            "video": {"implemented": False, "available": False},
        })
```

(or the `body["routes"]` / `assertTrue(all(...))` equivalents in the ServerTests version) with:

```python
        self.assertEqual(payload["routes"], {
            "tts": {"implemented": True, "available": True},
            "image_generate": {"implemented": False, "available": False},
            "image_edit": {"implemented": False, "available": False},
            "video": {"implemented": False, "available": False},
        })
```

and in `test_health_200_when_ready` replace the `assertTrue(all(not r["implemented"] ...))` line with:

```python
        self.assertTrue(body["routes"]["tts"]["implemented"]
                        and body["routes"]["tts"]["available"])
        self.assertTrue(all(not body["routes"][name]["implemented"]
                            for name in ("image_generate", "image_edit", "video")))
```

- [ ] **Step 6: Run the full regression (spec §14.3)**

```bash
set -euo pipefail
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md local-gateway/README.md
```

Expected: all unittest suites PASS, ruff clean, copy lint clean.

- [ ] **Step 7: Final health JSON assertion**

```bash
set -euo pipefail
curl --noproxy '*' --fail-with-body \
  http://127.0.0.1:8123/health > scratch/local-comfyui/p2/health.json

uv run python -I -c '
import json
p = "scratch/local-comfyui/p2/health.json"
d = json.load(open(p, encoding="utf-8"))
assert d["ready"] and d["models_ready"]
assert d["dependencies"] == {"ffmpeg": True, "ffprobe": True}
assert d["routes"]["tts"] == {"implemented": True, "available": True}
for name in ("image_generate", "image_edit", "video"):
    assert d["routes"][name] == {"implemented": False, "available": False}
print("health OK")
'
```

Expected: `health OK`. (The gateway loaded `IMPLEMENTED_ROUTES` at process start; if it was started before Step 5, restart `start_gateway.bat` first.)

- [ ] **Step 8: Tick the roadmap P2 checkboxes**

In `docs/local-comfyui-provider-roadmap.md`, in the `### P2 TTS 路由（clone-only 修订）` section, change all seven `- [ ]` items to `- [x]` (they are now true: object_info verified in Step 1, route/CLI/deadline/lock implemented in Tasks 3-7, unit tests green in Step 6, three real generations + health passed in Steps 3-7). Do not touch the P3/P4/P5 sections.

- [ ] **Step 9: Final status check and commit**

```bash
git status --short --branch --untracked-files=all
```

Expected: only the three tracked files of this task modified (`local-gateway/local_gateway/registry.py`, `local-gateway/tests/test_health.py`, `docs/local-comfyui-provider-roadmap.md`); no artifacts outside `scratch/` (which stays ignored).

```bash
git add local-gateway/local_gateway/registry.py local-gateway/tests/test_health.py docs/local-comfyui-provider-roadmap.md
git commit -m "FEAT: mark tts route implemented after real clone acceptance"
```

---

## Self-Review (performed while writing this plan)

1. **Spec coverage**: §1 goals: Task 5 (route), Task 3 (Advanced + single/ordered batch + mp3/wav), Task 3/6 (model name + sampling passthrough without cloud defaults), Task 6/7 (base64-only, no external storage), Task 6/7 + Task 9 (three real generations, CLIs complete), Task 8/9 (dual test domains + ruff + copy lint + real /health before implemented=true). §2 non-goals enforced by Global Constraints and by clone-only guards in Tasks 3/5/6/7. §3 decisions 1-6 all reflected (V0/FLAC in Task 3; MP3 direct + WAV transcode in Task 4; real material only in Task 9; cleanup scoped in Tasks 1/2/8). §4 module boundaries match the File Structure table; `tts_workflow` is pure, `audio` has no business params, `tts` writes no response, `server` only assembles. §5 contract fully enumerated in `parse_request` tests + implementation (including explicit-null handling, unknown fields, custom_voice/voice_design → mode_unsupported, external fields). §6 template rules, upload-once name/subfolder join, ordered per-item submit/poll/view, fail-fast, app-state lock: Tasks 3/5. §7 deadline formula + stage checks + sleep truncation (Task 2) + subprocess timeouts (Task 4) + preflight scope: Tasks 2/4/5. §8 history path, bounded view 64/128 MiB, MP3 passthrough, FLAC→pcm_s16le, ffprobe > 0, base64 without prefix, success shape + single-text mirror: Tasks 2/4/5. §9 full error table + client classification + no traceback/credential leaks: Tasks 2/5. §10 CLI requirements: Tasks 1/6/7 (including `--setup --cloud local` exit 2, doc default modal). §11 registry metadata, dependencies block, ready gating, fallback factory, object_info once, IMPLEMENTED_ROUTES gating: Tasks 3/8/9. §12 test lists map to the five gateway test files and two root test files. §13 items 1-7: item 1 Task 1 Step 5, item 2 Task 1 Step 5, item 3 Task 1, item 4 Task 2, item 5 Task 8, item 6 Task 2 (dynamic combo in `input_values` + test) and Task 3 (flat format/quality in template tests), item 7 Task 1. §14 acceptance: Task 9 verbatim. §15 commit boundaries: task split matches (cleanup / workflow+registry / audio+client / orchestration+route / CLIs / acceptance+health+docs).
2. **Placeholder scan**: no TBD/TODO/"add tests"/"similar to Task N" steps; every code step carries the full code or an exact find-and-replace patch; every run step names the command and the expected outcome.
3. **Cross-task signature consistency**: verified: `input_values`/`input_names` (Task 2) are imported by health (Task 2) and tts (Task 5); registry fields (Task 3) are consumed by tts (Task 5: `required_nonempty_directories`, `max_batch_items`, `deadline_chunk_seconds`) and health (Task 3/8); `tts_workflow.build_clone_prompt`/`locate_history_audio`/`SAVE_NODE` (Task 3) match audio.py and tts.py usage (Task 4/5); `audio.ViewBudget`/`finalize_output` (Task 4) match tts.py (Task 5) and the fake clients' `view(..., max_bytes=...)` signatures; `tts.TTSRouteError`/`generate_clone` (Task 5) match server.py; `generate_audio(..., model=...)` (Task 6) matches voiceover's forwarded kwargs (Task 7); the FakeTTSClient `wait(prompt_id, deadline)` matches the real client's signature. Two deliberate same-commit couplings are called out inline: Task 9 updates the `IMPLEMENTED_ROUTES` registry line and its test assertion together, and Task 8 plants the comment marking that spot.
4. **Main-agent review corrections**: DynamicCombo now uses the real `COMFY_DYNAMICCOMBO_V3` option-key shape; every item gets a unique `output_prefix`; special-field validation precedes generic unknown-field validation; only clone-capable TTS models are accepted; lock acquisition has a deadline; base64 decoding is strict; single/batch/scene-directory helpers all enforce clone-only; `qwen_model` no longer conflicts with ElevenLabs `model`; new keyword parameters follow existing positional parameters; P0 lint/preflight/BAT documentation is synchronized.
