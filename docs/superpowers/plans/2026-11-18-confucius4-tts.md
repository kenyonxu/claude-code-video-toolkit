# Confucius4-TTS Local Voice Clone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Qwen ComfyUI TTS route with a fully independent local Confucius4-TTS voice-clone chain: a dedicated Transformers worker (`ConfuciusTTS` loaded once, port 8765), a torch-free gateway that talks to it over multipart and returns base64 WAV/MP3, a new `tools/confucius4_tts.py` CLI and `voiceover.py --provider confucius4`, proven by three real clone generations.

**Architecture:** A new `confucius_worker.py` process (isolated prefix `E:/GitHub/_confucius4_tts_venv` cloned from the validated `confuciustts` conda env, repository-local offline HF cache at `models/hf`, basic Transformers only) exposes `GET /health` and `POST /api/tts` returning PCM16 mono 22050 Hz WAV bytes. The loopback gateway (still no torch) validates requests, serializes them on a bounded lock, posts one multipart per item through a new `ConfuciusClient` (never `ComfyUIClient`), then validates/transcodes the WAV bytes (WAV passthrough or FFmpeg libmp3lame MP3) and answers with base64. `qwen3_tts.py` loses `--cloud local` (Modal/RunPod unchanged); `cloud_gpu` maps only `confucius4_tts` to `LOCAL_TTS_ENDPOINT_URL`.

**Tech Stack:** Python >=3.10, uv (plain `unittest`, never pytest), FastAPI + TestClient (httpx + python-multipart in the gateway dev group) for gateway and worker, `requests` (`trust_env=False`, one Session per call) for both HTTP hops, FFmpeg/ffprobe subprocess (`shell=False`), torch/transformers only inside the isolated worker environment, Git Bash on Windows.

**Spec:** `docs/superpowers/specs/2026-11-18-confucius4-tts-design.md` (authoritative for every value below). Companion docs: `docs/local-comfyui-provider-roadmap.md`, `docs/local-comfyui-provider-p0-p1-design.md`, `docs/local-comfyui-provider-plan.md`.

## Global Constraints

Copied verbatim from the spec; every task implicitly includes all of these.

- **Backend:** "后端为基础 Transformers，通过薄 HTTP wrapper 复用 `confuciustts.cli.inference.ConfuciusTTS`"。禁止 vLLM、ComfyUI、AILab 节点、Qwen workflow、Qwen sampling、`model_size`。`local-gateway` 进程无 torch；worker 在 `E:/GitHub/_confucius4_tts_venv`（从已验证 `confuciustts` 环境克隆，保留 cu128），启动在 HF/Transformers 导入前设置 `HF_HUB_CACHE=<CONFUCIUS4_TTS_ROOT>/models/hf`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，"不在生成时下载权重"，也不回落到用户 home cache。
- **Worker contract:** 只加载一次 `ConfuciusTTS`（`device="cuda"`，绝对 `config/inference_config.yaml`）；`GET /health` 返回 `status`（`starting` 503 / `ok` 200 `sample_rate=22050` / `error` 503 脱敏无 traceback）；`POST /api/tts` 只收 multipart `text`/`lang`/`reference`，lock 串行 `generate()`，返回 "PCM16、mono、22050 Hz WAV bytes"；参考音频写平台临时目录、请求后删除、不写 Confucius 根目录；"输出明确编码为 PCM16"。
- **Client:** `ConfuciusClient` "不复用或伪装 `ComfyUIClient`"；`health() -> dict`、`generate(text, lang, reference_bytes, filename, content_type, deadline) -> bytes`；`requests.Session.trust_env=False`，"每次请求独立 Session 并关闭"；短 connect timeout、read timeout 用剩余预算；"POST 不重试"；"单文件最多 64 MiB，所有 item 合计最多 128 MiB"；错误不透传 traceback 或绝对路径。
- **Error table:** `mode_unsupported` 422, `validation_error` 422, `unknown_model` 422, `external_url_unsupported` 422, `dependency_missing` 503, `tts_backend_unavailable` 503, `tts_backend_timeout` 504, `tts_backend_error` 502, `generation_failed` 500。`comfyui_*` 码不用于 TTS。
- **Request contract:** 顶层只允许 `model`（缺省 `confucius4-tts`，否则 `unknown_model`）、`text`（必填，"非空字符串或最多 8 项非空字符串数组，每项最多 1024 字符"）、`mode`（缺省 `clone`）、`language`（默认 `zh`，"限 14 个小写代码：`zh/en/ja/ko/de/fr/es/id/it/ms/pt/ru/th/vi`"，标量广播、数组等长）、`output_format`（`mp3` 默认或 `wav`）、`ref_audio_base64`（必填，可带 data-URL，"解码后最大 16 MiB"）；`ref_audio_url`/`r2` → `external_url_unsupported`；`ref_text`/`speaker`/`instruct`/`temperature`/`top_p`/CustomVoice/VoiceDesign → `mode_unsupported`；其他未知字段 → `validation_error`；"严格 base64 解码，data-URL 的 MIME 与 `;base64` 参数必须分离"，一切校验在后端请求前完成。
- **Batch/deadline/lock:** "每个 item 独立执行 multipart generate、下载和最终编码，保持顺序。任一 item 失败时整个请求失败"；"deadline_seconds = 270 * max(1, item_count // 3 + 1)"，"从等待 lock 前开始，覆盖 preflight、multipart、生成、下载、ffprobe 和 FFmpeg"；"lock 等待自身也必须有界"；poll/FFmpeg 按剩余时间截断；"`/health` 不获取 TTS lock"；"不自动 `/interrupt`，不重试生成 POST"。
- **Audio:** WAV 直通（要求 PCM16、mono、22050 Hz）；MP3 用 FFmpeg `libmp3lame` 真转码；最终文件 "ffprobe，要求 `duration_seconds > 0`"；"临时文件 + 原子替换"；"单文件 64 MiB、单请求 128 MiB；临时目录在成功和失败时均清理"；不读 ComfyUI history、不用 FLAC 中间文件、不沿用 `tts_workflow.locate_history_audio`。
- **CLI/voiceover:** `confucius4_tts.py` 提供 `--cloud local`、`--model confucius4-tts`、`--lang zh`、`--ref-audio`、`--output`、`--format mp3|wav`、`--timeout 300`，`tool_name="confucius4_tts"`；`voiceover` 新增 `provider=confucius4` 与 `--tts-model`（取代 `--qwen-model`，保留 ElevenLabs `--model`）；local 所有 single/batch/scene/brand clone 必须 `--ref-audio` 但不要求 `--ref-text`；内置 speaker、brand VoiceDesign 及其他非 clone 路径在 local 下明确拒绝；"qwen3 + local 不得再作为可用路径"；`qwen3_tts` 保留 Modal/RunPod 不变。
- **Registry/health:** TTS 条目 `confucius4-tts`：`modes=(clone,)`、`output_formats=(mp3, wav)`、14 语言、`max_batch_items=8`、`deadline_chunk_seconds=270`；无 AILab 节点、Qwen 权重目录、model_size、Qwen sampling。`/health` 新增 `tts_backend: { base_url, status, sample_rate }`；`routes.tts.available` 要求 implemented、`tts_backend.status=ok`、`sample_rate=22050`、ffmpeg/ffprobe；全局 `ready` 不再要求 `comfyui.reachable`，但仍要求 config/models/dependencies，"图像/视频节点缺项只在各自 preflight 处理，声明文件仍计入 models_ready"；"只有本规格第 14 节全部通过后，`IMPLEMENTED_ROUTES` 才加入 `tts`"，其他三项仍为 false。
- **Tests:** "所有网络、模型加载、模型目录和 worker 均使用 fake"（只有第 14 节真实验收豁免）；所有 uv-run 测试前缀 `TEMP='E:\GitHub' TMP='E:\GitHub'`；unittest，禁止 pytest；ffmpeg/ffprobe 子进程真跑（本地进程非网络）。
- **Real acceptance:** 参考音频只读 `C:/Users/kenyo/Downloads/Music/voice_02.mp3`，不传 `ref_text`；先启动 8765 worker 再启动 8123 gateway，"P2 不需要启动 ComfyUI"，不停止已有 9188 进程；三项生成 = `confucius4_tts` clone MP3、clone WAV、`voiceover` clone；验收后才勾 P2。
- **Out of scope:** 不写 fork/P0；不改 `video_job.py`、`verify_setup.py`、根 `.env.example`、toolkit registry；不动 Modal/RunPod 请求/R2/下载/价格；不提前 P3/P4/P5；不删除 P0/P1 通用 `ComfyUIClient`、registry 图像/视频条目、health 骨架或通用测试；权重与参考音频许可证发布前单独核查。
- **Process:** 分支 `feat/local-comfyui-provider-p2`（已检出，无 fork/branch 步骤）；提交前缀大写 `DOCS:`/`FEAT:`/`FIX:`；本计划文件纳入首个实现阶段文档提交；推送与 PR 不在范围内。当前基线 `e09ce2c` 是旧 Qwen 9189 路线的交错验收提交，保留历史但不继承其 implemented 结论；Task 4 必须先把 `IMPLEMENTED_ROUTES` 恢复为空，Task 8 只能用新的 Confucius 证据再次 flip，禁止 reset/revert 删除旧历史。

## File Structure

| File | Responsibility |
|---|---|
| `local-gateway/local_gateway/confucius_worker.py` (create) | Worker: `ConfuciusTTS` 加载一次、`/health`、multipart `/api/tts`、PCM16 WAV 编码、checkpoints 目录联接、离线环境变量 |
| `local-gateway/local_gateway/confucius_client.py` (create) | 网关侧 HTTP 客户端：health、单次 multipart generate、有界二进制响应、错误映射 |
| `local-gateway/local_gateway/tts.py` (rewrite) | Confucius 请求校验、preflight、deadline 公式、有界 lock、逐项编排、错误表 |
| `local-gateway/local_gateway/audio.py` (rewrite) | WAV 头校验（PCM16/mono/22050）、WAV 直通、WAV→MP3 libmp3lame、ffprobe、原子替换、字节预算 |
| `local-gateway/local_gateway/registry.py` (modify) | TTS 条目换成 `confucius4-tts`（含 `languages`），图像/视频不变 |
| `local-gateway/local_gateway/config.py` (modify) | 新增 `tts_worker_url`（`CONFUCIUS4_TTS_BASE_URL`，默认 `http://127.0.0.1:8765`） |
| `local-gateway/local_gateway/health.py` (modify) | `tts_backend` 块、TTS 就绪改为 worker ok、image/video ready 只看文件、新 ready 公式 |
| `local-gateway/local_gateway/server.py` (rewrite) | 分别装配 ComfyUI factory 与 Confucius factory，`/tts/generate` 用后者 |
| `local-gateway/local_gateway/tts_workflow.py` (delete) | 被 Confucius 路线整体替换 |
| `local-gateway/local_gateway/workflows/tts_voice_clone.json` (delete) | 旧 Qwen ComfyUI workflow 随路线删除 |
| `local-gateway/start_confucius_worker.bat` (create) | 用隔离环境启动 8765 worker |
| `.gitignore` (modify) | 忽略仓库根 `/checkpoints/` 目录联接，防止模型权重入库 |
| `local-gateway/pyproject.toml` (modify) | dev 组加 `python-multipart`（TestClient multipart） |
| `local-gateway/.env.example` (modify) | 加 `CONFUCIUS4_TTS_BASE_URL` |
| `local-gateway/tests/test_confucius_worker.py` (create) | worker 契约（fake engine，无 torch） |
| `local-gateway/tests/test_confucius_client.py` (create) | client 契约（fake Session） |
| `local-gateway/tests/test_tts.py` (rewrite) | 校验矩阵、preflight、编排、lock、路由映射 |
| `local-gateway/tests/test_audio.py` (rewrite) | WAV 校验、直通、转码、预算、原子性 |
| `local-gateway/tests/test_health.py` (rewrite) | registry 新形状、tts_backend、ready 公式、fallback |
| `local-gateway/tests/test_config.py` (modify) | `CONFUCIUS4_TTS_BASE_URL` 用例 |
| `local-gateway/tests/test_tts_workflow.py` (delete) | 随模块删除 |
| `tools/confucius4_tts.py` (create) | 新本地 CLI（clone-only，base64 transport） |
| `tools/voiceover.py` (modify) | provider `confucius4`、`--tts-model`、local guard 重写、confucius4 single/batch/scene/brand 分支 |
| `tools/qwen3_tts.py` (modify) | 移除 `--cloud local` 及 local setup/base64/R2 分支 |
| `tools/cloud_gpu.py` (modify) | `_LOCAL_ENV_VARS` TTS 键换 `confucius4_tts`，local 提示改 worker+gateway |
| `tests/python/test_qwen3_tts_local.py` (delete) | 旧 local 断言全部失效 |
| `tests/python/test_qwen3_tts_no_local.py` (create) | 直接 API 的 `qwen3 + local` 在 R2/HTTP 前拒绝 |
| `tests/python/test_voiceover_local.py` (delete) | 旧 Qwen local voiceover 断言全部失效 |
| `tests/python/test_confucius4_tts_local.py` (create) | 新 CLI local 测试 |
| `tests/python/test_voiceover_confucius4.py` (create) | voiceover confucius4 测试 |
| `tests/python/test_cloud_gpu_local.py` (modify) | env 映射与提示断言更新 |
| `docs/superpowers/specs/2026-11-18-confucius4-tts-design.md` (modify) | 同步上游整合指南、仓库内 models/hf 与隔离环境准备 |
| `docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md` (modify) | superseded 标注 |
| `docs/superpowers/plans/2026-11-17-local-comfyui-p2-voice-clone.md` (modify) | superseded 标注，旧 Task 9 不再执行 |
| `docs/local-comfyui-provider-roadmap.md` (modify) | P2 改 Confucius、附录 A/B 修正、P5 `--tts-model` |
| `docs/local-comfyui-provider-plan.md` (modify) | 架构改为独立 Confucius worker |
| `docs/local-comfyui-provider-p0-p1-design.md` (modify) | superseding 说明 |
| `local-gateway/README.md` (modify) | 启动顺序、配置、示例改 Confucius |
| `docs/superpowers/plans/2026-11-18-confucius4-tts.md` (this file) | Task 1 首个文档提交收录 |

CI workflows 需要改吗：不需要。`python-tests.yml` 已覆盖 `tests/python/**` 与 `local-gateway/**`，`lint-python.yml` 已 lint `local-gateway/`。

## Execution conventions

- 仓库根 `E:\GitHub\claude-code-video-toolkit`，shell Git Bash，所有命令从仓库根运行；当前分支 `feat/local-comfyui-provider-p2`。
- 根域单测：`TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/<file>.py -v`
- 网关单测：`TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p '<file>.py' -v`
- 绝不用 pytest。网关测试不读真实 `local-gateway/.env`（显式 env dict）；根域测试不读真实 `.env`。
- 修改共享文件时以符号/片段为锚点，不按行号。
- 验收产物放 `scratch/local-comfyui/p2/`（已 gitignore）。

---

### Task 1: Docs deprecation and this plan's first commit

**Files:**
- Modify: `docs/superpowers/specs/2026-11-18-confucius4-tts-design.md`（preflight 已按上游新指南完成 `models/hf`、`HF_HUB_CACHE`、克隆隔离环境与验收工具名同步，本任务保留并提交）
- Modify: `docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md`
- Modify: `docs/superpowers/plans/2026-11-17-local-comfyui-p2-voice-clone.md`
- Modify: `docs/local-comfyui-provider-p0-p1-design.md`
- Modify: `docs/local-comfyui-provider-plan.md`
- Add: `docs/superpowers/plans/2026-11-18-confucius4-tts.md`（本文件）

**Interfaces:**
- Consumes: 无（纯文档）。
- Produces: 所有旧 P2 文档带 superseded 标注，读者被指到 `docs/superpowers/specs/2026-11-18-confucius4-tts-design.md`；后续任务的文档引用以此为准。

- [ ] **Step 1: 标注旧 P2 spec superseded**

在 `docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md` 的标题行 `# P2 设计规格：local ComfyUI Voice Clone 路由` 之后、现有 blockquote 之前插入：

```markdown
> **SUPERSEDED (2026-11-18)：本文的 Qwen ComfyUI 路线已被
> [2026-11-18-confucius4-tts-design.md](./2026-11-18-confucius4-tts-design.md)
> 取代。TTS 改走独立 Confucius4-TTS worker（基础 Transformers，端口 8765），不再使用
> AILab 节点、ComfyUI workflow 或 `ref_text`。本文仅作历史记录，不得据此实施。**
```

- [ ] **Step 2: 标注旧 P2 plan superseded**

在 `docs/superpowers/plans/2026-11-17-local-comfyui-p2-voice-clone.md` 标题行 `# Local ComfyUI Provider P2 (Voice Clone) Implementation Plan` 之后插入：

```markdown
> **SUPERSEDED (2026-11-18)：本计划被
> [2026-11-18-confucius4-tts.md](./2026-11-18-confucius4-tts.md) 取代。TTS 路线改为
> 独立 Confucius4-TTS worker。旧 Task 9 虽在路线变更后交错完成并产生 `e09ce2c`，
> 但那是 Qwen ComfyUI 9189 沙箱的验收，不满足新规格，不得作为 Confucius
> implemented/available 或 P2 勾选依据。本文仅作历史记录，旧路线不得继续执行。**
```

- [ ] **Step 3: P0/P1 spec 加 superseding 说明**

在 `docs/local-comfyui-provider-p0-p1-design.md` 的第一条 blockquote（`> 状态：...` 行）之后插入一行：

```markdown
> 修订（2026-11-18）：P2 的 TTS 后端已改为独立 Confucius4-TTS worker，见 [2026-11-18 Confucius4 规格](./superpowers/specs/2026-11-18-confucius4-tts-design.md)。本文记录的 P0/P1 已完成事实不变；其中涉及 P2 TTS 经 ComfyUI 的展望以新规格为准。仅就 P2 之后的 `/health`，原 `ready` 公式中的 `comfyui.reachable` 合取由独立 worker readiness 取代，ComfyUI 可达性保留为诊断信息，不再要求为 P2 启动 ComfyUI。
```

- [ ] **Step 4: 原始 plan 架构说明**

在 `docs/local-comfyui-provider-plan.md` 的第一条 blockquote（`> 前置事实...` 行）之后插入：

```markdown
> 修订（2026-11-18）：本计划的 TTS 部分（原设想全部经 ComfyUI Qwen 节点）已被独立 Confucius4-TTS worker 路线取代，见 [2026-11-18 Confucius4 规格](./superpowers/specs/2026-11-18-confucius4-tts-design.md)。图像/视频经 ComfyUI 的设想不变。
```

- [ ] **Step 5: copy lint 检查改动文档**

```bash
uv run tools/copy_lint.py \
  docs/superpowers/specs/2026-11-18-confucius4-tts-design.md \
  docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p2-voice-clone.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/local-comfyui-provider-plan.md
```

Expected: exit 0（无 dash 违规输出；本计划的文本已按 copy lint 规则书写）。

- [ ] **Step 6: 提交（含本计划文件）**

```bash
git add docs/superpowers/plans/2026-11-18-confucius4-tts.md \
  docs/superpowers/specs/2026-11-18-confucius4-tts-design.md \
  docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p2-voice-clone.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/local-comfyui-provider-plan.md
git commit -m "DOCS: supersede the Qwen ComfyUI TTS route with the Confucius4 plan"
```

---

### Task 2: Remove the qwen3 local route and re-map the local TTS env var

**Files:**
- Modify: `tools/qwen3_tts.py`
- Modify: `tools/voiceover.py`
- Modify: `tools/cloud_gpu.py`
- Modify: `tests/python/test_cloud_gpu_local.py`
- Delete: `tests/python/test_qwen3_tts_local.py`
- Delete: `tests/python/test_voiceover_local.py`
- Create: `tests/python/test_qwen3_tts_no_local.py`

**Interfaces:**
- Consumes: 现有 `qwen3_tts.generate_audio`、`voiceover.local_clone_error(provider, cloud, ref_audio, ref_text, brand_design=False)`、`cloud_gpu._LOCAL_ENV_VARS`。
- Produces: `cloud_gpu._LOCAL_ENV_VARS == {"confucius4_tts": "LOCAL_TTS_ENDPOINT_URL", "qwen_image": ..., "image_edit": ..., "h3_video": ...}`；`voiceover.local_clone_error(provider, cloud, ref_audio, brand_design=False)`（新签名，`qwen3`+`local` 拒绝、`confucius4` 尚未实现/provider 未知时只对 qwen3+local 拒绝）；`qwen3_tts` `--cloud` 只剩 `runpod|modal`。Task 5/6 依赖 `_LOCAL_ENV_VARS["confucius4_tts"]`。

- [ ] **Step 1: 写失败测试（test_cloud_gpu_local.py 新映射与新提示）**

在 `tests/python/test_cloud_gpu_local.py` 中替换 `ProviderConfigTests.test_local_env_var_mapping_is_the_four_capability_urls` 整个方法为：

```python
    def test_local_env_var_mapping_is_the_four_capability_urls(self):
        self.assertEqual(cg._LOCAL_ENV_VARS, {
            "confucius4_tts": "LOCAL_TTS_ENDPOINT_URL",
            "qwen_image": "LOCAL_IMAGE_ENDPOINT_URL",
            "image_edit": "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
            "h3_video": "LOCAL_VIDEO_ENDPOINT_URL",
        })
```

再把 `run_local` 的默认值改为新工具：

```python
def run_local(session, endpoint_url, tool_name="confucius4_tts", payload=None,
              timeout=600, progress=None):
```

`ProviderConfigTests.test_local_config_is_none_when_env_missing` 与 `LocalDispatchTests.test_call_cloud_endpoint_dispatches_local_end_to_end` 的 local `tool_name` 也都从 `qwen3_tts` 改为 `confucius4_tts`；后者其余 URL、payload、timeout 和 progress 断言不变。`gcp` 未知 provider 与 RunPod/Modal 的 `qwen3_tts` 用例保持不变。

`test_missing_url_error_names_tool_and_env_var_before_any_request` 中 `tool_name="qwen3_tts"` 改为 `tool_name="confucius4_tts"`、断言 `self.assertIn("confucius4_tts", result["error"])`。最后替换 `test_connection_failure_mentions_comfyui_and_start_script` 为：

```python
    def test_connection_failure_mentions_worker_and_start_scripts(self):
        session = FakeSession(exc=requests.exceptions.ConnectionError("connection refused"))
        result, _ = run_local(session, self.url)
        self.assertIn("start_gateway.bat", result["error"])
        self.assertIn("start_confucius_worker.bat", result["error"])
```

再创建 `tests/python/test_qwen3_tts_no_local.py`，防止直接 Python API 绕过 CLI 后落入 R2：

```python
"""qwen3_tts: local is rejected before storage, network or reference upload."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import qwen3_tts  # noqa: E402


class NoLocalRouteTests(unittest.TestCase):
    def reject(self, **kwargs):
        with mock.patch("qwen3_tts.get_r2_payload_config") as r2_config, \
                mock.patch("qwen3_tts.upload_to_storage") as upload, \
                mock.patch("cloud_gpu.call_cloud_endpoint") as call:
            result = qwen3_tts.generate_audio(
                text="hello", output_path="unused.mp3", cloud="local",
                verbose=False, **kwargs)
        self.assertFalse(result["success"])
        self.assertIn("confucius4", result["error"])
        r2_config.assert_not_called()
        upload.assert_not_called()
        call.assert_not_called()

    def test_custom_voice_local_is_rejected_before_r2_or_http(self):
        self.reject()

    def test_clone_local_is_rejected_before_reference_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "ref.wav"
            reference.write_bytes(b"reference")
            self.reject(ref_audio=str(reference), ref_text="hello")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py tests/python/test_qwen3_tts_no_local.py -v
```

Expected: FAIL（`confucius4_tts` 未注册、提示仍含 ComfyUI；直接 `qwen3 + local` 尚未统一拒绝）。

- [ ] **Step 3: 修改 cloud_gpu.py**

`tools/cloud_gpu.py` 中 `_LOCAL_ENV_VARS` 替换为：

```python
_LOCAL_ENV_VARS = {
    "confucius4_tts": "LOCAL_TTS_ENDPOINT_URL",
    "qwen_image": "LOCAL_IMAGE_ENDPOINT_URL",
    "image_edit": "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
    "h3_video": "LOCAL_VIDEO_ENDPOINT_URL",
}
```

`_LOCAL_GATEWAY_HINT` 替换为：

```python
_LOCAL_GATEWAY_HINT = (
    " Is the local stack running? Start the Confucius4-TTS worker"
    " (local-gateway/start_confucius_worker.bat, 127.0.0.1:8765) first,"
    " then the gateway (local-gateway/start_gateway.bat, 127.0.0.1:8123)."
)
```

同时把模块 docstring 的 `local: loopback local-gateway endpoints backed by local ComfyUI (new)` 改为 `local: loopback local-gateway endpoints backed by local worker/ComfyUI capabilities`，把 `_call_local` docstring 与 Timeout 文案中的 `ComfyUI job` 都改为 `backend job`。只改诊断措辞，不改变 timeout、取消或重试语义。

- [ ] **Step 4: 运行通过**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v
```

Expected: PASS（全部）。

- [ ] **Step 5: 删除旧 local 测试并确认根域其余测试不受影响**

```bash
git rm tests/python/test_qwen3_tts_local.py tests/python/test_voiceover_local.py
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py tests/python/test_brand_voices.py -v
```

Expected: PASS（旧两个文件已删；brand 配置加载与 TTS provider 无关）。

- [ ] **Step 6: qwen3_tts.py 移除 local 分支（六处精确编辑）**

编辑一，模块 docstring 中这行：

```python
Cloud providers: Modal (default), RunPod, or the local gateway (--cloud local; voice clone only).
```

替换为：

```python
Cloud providers: Modal (default) or RunPod. Local voice clone lives in tools/confucius4_tts.py.
```

编辑二，`generate_audio` 中：

```python
        # R2 config for cloud providers only; local never touches external storage
        r2_payload = None if cloud == "local" else get_r2_payload_config()
```

替换为：

```python
        r2_payload = get_r2_payload_config()
```

编辑三，把整个旧 local clone guard（`# Local gateway is clone-only (P2)...` 注释起 6 行）替换为位于 R2 配置之前、所有模式和参考音频上传之前的统一拒绝：

```python
        if cloud == "local":
            return {"success": False, "error": (
                "qwen3 no longer has a local route: use tools/confucius4_tts.py"
                " or voiceover.py --provider confucius4 --cloud local")}
```

把编辑二得到的 `r2_payload = get_r2_payload_config()` 放在这个 guard 之后。

编辑四，参考音频块：

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

替换为：

```python
        # Reference audio for clone mode: R2 upload (cloud only; there is no
        # local route in this tool anymore)
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

编辑五，payload 组装：

```python
        if mode == "clone":
            if cloud == "local":
                payload["input"]["ref_audio_base64"] = ref_audio_b64
            else:
                payload["input"]["ref_audio_url"] = ref_audio_url
            payload["input"]["ref_text"] = ref_text
```

替换为：

```python
        if mode == "clone":
            payload["input"]["ref_audio_url"] = ref_audio_url
            payload["input"]["ref_text"] = ref_text
```

编辑六，下载循环中删除整个 `if cloud == "local":` 分支（从 `if cloud == "local":` 注释 `# Local results must be base64-only...` 起，到该分支 `print(f"  Decoded from base64: {dest_path} ({size_kb}KB)", file=sys.stderr)` 行止，含分支内所有行），保留其后的云端分支：删除 `else:` 行，并将云端分支体统一减少一级缩进，使其直接位于 `for` 循环内。同时在 argparse `--cloud` 定义中：

```python
        choices=["runpod", "modal", "local"],
        help="Cloud GPU provider (default: modal; local = loopback gateway, voice clone only)",
```

替换为：

```python
        choices=["runpod", "modal"],
        help="Cloud GPU provider (default: modal)",
```

删除 epilog 中这段示例：

```text
  # Local gateway voice clone (requires local ComfyUI + gateway)
  uv run tools/qwen3_tts.py --cloud local --text "Hello" --ref-audio ref.mp3 --ref-text "transcript" --output cloned.mp3
```

删除 `main()` 中 `--setup` 的 local 分支：

```python
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
```

删除 `main()` 中整个块：

```python
    # Local gateway is clone-only (P2): validate before any HTTP request.
    if args.cloud == "local":
        if args.design_instruct:
            ...
        if not (args.ref_audio and args.ref_text):
            ...
            sys.exit(1)
```

（该块自注释行起至其最后一个 `sys.exit(1)` 止。）

- [ ] **Step 7: voiceover.py 重写 local guard**

替换 `local_clone_error` 整个函数为：

```python
def local_clone_error(provider: str, cloud: str, ref_audio,
                      brand_design: bool = False) -> str | None:
    """CLI error message when a local voice call is not clone-only, else None.

    confucius4 is the only local TTS provider: it needs --ref-audio and no
    ref_text. qwen3 no longer has a local route (the gateway TTS route moved
    to Confucius4-TTS). ElevenLabs, 60db and cloud qwen3 are never affected.
    """
    if provider == "confucius4":
        if cloud != "local":
            return ("confucius4 runs on the local gateway only: pass"
                    f" --cloud local (requested cloud: {cloud})")
        if brand_design:
            return ("--provider confucius4 supports voice clone only; brand"
                    " clone.design (VoiceDesign) is not available locally."
                    " Provide --ref-audio, or use a cloud provider for this"
                    " voice.")
        if not ref_audio:
            return ("--provider confucius4 supports voice clone only:"
                    " --ref-audio is required (built-in speakers and voice"
                    " design are not available locally).")
        return None
    if provider == "qwen3" and cloud == "local":
        return ("qwen3 no longer has a local route: use --provider"
                " confucius4 --cloud local for local voice clone.")
    return None
```

更新全部 5 个调用点（删掉 `ref_text` 实参）：

- `generate_single_audio_qwen3` 内：`error = local_clone_error("qwen3", cloud, ref_audio)`（原为 `local_clone_error("qwen3", cloud, ref_audio, ref_text)`）。
- `generate_batch_audio_qwen3` 内：`error = local_clone_error("qwen3", cloud, ref_audio)`（同样删除 `ref_text` 实参）。
- `process_scene_directory` 内：同样删除该调用的 `ref_text` 实参；Task 6 再把 provider 从固定 `qwen3` 泛化。
- `main()` 的 `# Local gateway is clone-only (P2)` guard：`guard = local_clone_error(provider, args.cloud, args.ref_audio)`（原为 `local_clone_error(provider, args.cloud, args.ref_audio, args.ref_text)`）。
- `main()` brand 分支：`design_guard = local_clone_error(provider, args.cloud, None, brand_design=True)`（原多一个 `None`）。

- [ ] **Step 8: 语法与 lint 检查**

```bash
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
```

Expected: 无输出（`base64` 仍在 `qwen3_tts` 云端 fallback 分支使用；若报 unused import 不在本 lint 集内，不处理）。

- [ ] **Step 9: 回归并提交**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
git add tools/qwen3_tts.py tools/voiceover.py tools/cloud_gpu.py \
  tests/python/test_cloud_gpu_local.py tests/python/test_qwen3_tts_no_local.py
git commit -m "FEAT: drop the qwen3 local TTS route and remap LOCAL_TTS_ENDPOINT_URL to confucius4_tts"
```

Expected: 两个套件全绿（网关侧尚未改动）。

---

### Task 3: Confucius worker and ConfuciusClient

**Files:**
- Create: `local-gateway/local_gateway/confucius_worker.py`
- Create: `local-gateway/local_gateway/confucius_client.py`
- Create: `local-gateway/start_confucius_worker.bat`
- Modify: `.gitignore`
- Modify: `local-gateway/pyproject.toml`
- Test: `local-gateway/tests/test_confucius_worker.py`（create）
- Test: `local-gateway/tests/test_confucius_client.py`（create）

**Interfaces:**
- Consumes: `fastapi`、`uvicorn`、`requests`（均已在网关依赖内）；`confuciustts.cli.inference.ConfuciusTTS`（仅 worker 隔离环境内惰性导入）。
- Produces（Task 4 依赖）:
  - `confucius_client.ConfuciusClient(base_url: str, connect_timeout: float = 5.0, max_response_bytes: int = 64MiB)`，方法 `health() -> dict`、`generate(*, text: str, lang: str, reference_bytes: bytes, filename: str, content_type: str, deadline: float) -> bytes`；异常 `confucius_client.ConfuciusError(message, code, details=None)`，code ∈ {`tts_backend_unavailable`, `tts_backend_timeout`, `tts_backend_error`}。
  - `confucius_worker.create_worker_app(state: WorkerState | None = None) -> FastAPI`；`WorkerState(status, sample_rate, error, engine, lock)`；`pcm16_wav_bytes(pcm: bytes, sample_rate: int) -> bytes`；`LANGUAGES`（14 码元组）；`apply_offline_env(confucius_root: Path | None = None) -> None`；`ensure_checkpoints_link(ccvt_root: Path, confucius_root: Path) -> None`；`create_directory_junction(link: Path, target: Path) -> None`；`main(argv=None) -> None`。

- [ ] **Step 1: dev 依赖加 python-multipart 并写 worker 失败测试**

`local-gateway/pyproject.toml` 的 dev 组改为：

```toml
[dependency-groups]
# fastapi.testclient.TestClient needs httpx; multipart form routes (worker
# tests) need python-multipart. The gateway runtime never does.
dev = ["httpx>=0.27", "python-multipart>=0.0.9"]
```

创建 `local-gateway/tests/test_confucius_worker.py`：

```python
"""confucius_worker: health states, multipart /api/tts, temp cleanup, lock.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_worker.py' -v

No torch anywhere: the engine is a fake and module import must never pull
ConfuciusTTS (that import lives inside ConfuciusEngine only).
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
                mock.patch.object(cw, "ensure_checkpoints_link",
                                  side_effect=AssertionError("no default root")), \
                mock.patch.object(cw.sys, "stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cw.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_ensure_checkpoints_link_skips_existing_and_creates_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ccvt_root = Path(tmp) / "repo"
            confucius_root = Path(tmp) / "confucius"
            ccvt_root.mkdir()
            (confucius_root / "checkpoints").mkdir(parents=True)
            cw.ensure_checkpoints_link(ccvt_root, confucius_root)  # creates
            self.assertTrue((ccvt_root / "checkpoints").exists())
            with mock.patch.object(
                    cw, "create_directory_junction",
                    side_effect=AssertionError("must not run again")):
                cw.ensure_checkpoints_link(ccvt_root, confucius_root)
        self.assertEqual(cw.LANGUAGES, ("zh", "en", "ja", "ko", "de", "fr",
                                        "es", "id", "it", "ms", "pt", "ru",
                                        "th", "vi"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_worker.py' -v
```

Expected: FAIL/ERROR（`ModuleNotFoundError: No module named 'local_gateway.confucius_worker'`）。

- [ ] **Step 3: 实现 confucius_worker.py**

创建 `local-gateway/local_gateway/confucius_worker.py`：

```python
"""Confucius4-TTS worker: loads ConfuciusTTS once; /health and POST /api/tts.

Runs in an isolated environment cloned from the validated confuciustts
conda env (E:/GitHub/_confucius4_tts_venv) with basic
Transformers -- never vLLM, never ComfyUI, never a Qwen workflow. The
gateway process never imports this module at runtime; gateway tests import
it with a fake engine, so every torch-dependent import stays inside
ConfuciusEngine and this module imports cleanly without torch.

Endpoints:
    GET  /health   {"status": "starting"|"ok"|"error", "sample_rate": 22050}
    POST /api/tts  multipart text/lang/reference -> PCM16 mono 22050 Hz WAV
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SAMPLE_RATE = 22050
MAX_TEXT_CHARS = 1024
MAX_REFERENCE_BYTES = 16 * 1024 * 1024
LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "es", "id", "it",
             "ms", "pt", "ru", "th", "vi")


def pcm16_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Encode raw little-endian int16 mono samples as a PCM16 WAV stream."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


class ConfuciusEngine:
    """Thin adapter over confuciustts.cli.inference.ConfuciusTTS.

    All torch/transformers imports live here so importing this module stays
    torch-free (gateway tests substitute a fake engine).
    """

    def __init__(self, config_path: Path, device: str = "cuda"):
        from confuciustts.cli.inference import ConfuciusTTS

        self._model = ConfuciusTTS(config_path=str(config_path), device=device)
        # Upstream inference.py sets this from audio.target_sample_rate.
        self.sample_rate = int(self._model.sample_rate)
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError(
                f"Confucius4-TTS must emit {SAMPLE_RATE} Hz, got"
                f" {self.sample_rate} Hz")

    def generate(self, text: str, lang: str, prompt_path: str) -> bytes:
        import numpy as np

        audio = self._model.generate(text, lang, prompt_path)
        samples = audio.detach().cpu().numpy().reshape(-1)
        samples = np.clip(samples.astype("float64"), -1.0, 1.0)
        pcm = (samples * 32767.0).astype("<i2").tobytes()
        return pcm16_wav_bytes(pcm, self.sample_rate)


@dataclass
class WorkerState:
    status: str = "starting"          # starting | ok | error
    sample_rate: int | None = None
    error: str | None = None          # sanitized, never a traceback
    engine: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _sanitize(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def start_loader(state: WorkerState, config_path: Path,
                 device: str = "cuda") -> threading.Thread:
    """Load the model once in the background; /health serves meanwhile."""

    def run() -> None:
        try:
            engine = ConfuciusEngine(config_path, device)
        except Exception as e:  # loading failure -> error state, no traceback out
            logger.exception("ConfuciusTTS failed to load")
            state.status = "error"
            state.error = _sanitize(e)
            return
        state.engine = engine
        state.sample_rate = engine.sample_rate
        state.status = "ok"

    thread = threading.Thread(target=run, daemon=True, name="confucius-loader")
    thread.start()
    return thread


def create_worker_app(state: WorkerState | None = None) -> FastAPI:
    worker_state = state if state is not None else WorkerState()
    app = FastAPI(title="ccvt confucius4-tts worker", version="0.1.0")
    app.state.worker = worker_state

    @app.get("/health")
    def health() -> JSONResponse:
        payload = {"status": worker_state.status,
                   "sample_rate": worker_state.sample_rate}
        if worker_state.status == "error" and worker_state.error:
            payload["error"] = worker_state.error
        status_code = 200 if worker_state.status == "ok" else 503
        return JSONResponse(content=payload, status_code=status_code)

    @app.post("/api/tts")
    def tts(text: str = Form(...), lang: str = Form(...),
            reference: UploadFile = File(...)) -> Response:
        text = text or ""
        problems: list[str] = []
        if not text.strip():
            problems.append("text must be a non-empty string")
        if len(text) > MAX_TEXT_CHARS:
            problems.append(f"text exceeds {MAX_TEXT_CHARS} characters")
        if lang not in LANGUAGES:
            problems.append(
                f"lang must be one of {', '.join(LANGUAGES)}; got {lang!r}")
        payload = reference.file.read(MAX_REFERENCE_BYTES + 1) or b""
        if not payload:
            problems.append("reference audio is empty")
        if len(payload) > MAX_REFERENCE_BYTES:
            problems.append(
                f"reference audio exceeds {MAX_REFERENCE_BYTES} bytes")
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"error": "; ".join(problems), "code": "validation"})
        if worker_state.status != "ok" or worker_state.engine is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "worker is not ready (status:"
                        f" {worker_state.status})", "code": "not_ready"})
        suffix = Path(reference.filename or "reference.audio").suffix or ".audio"
        with tempfile.NamedTemporaryFile(prefix="ccvt_ref_", suffix=suffix,
                                         delete=False) as tmp:
            tmp.write(payload)
            prompt_path = tmp.name
        try:
            with worker_state.lock:
                wav = worker_state.engine.generate(text, lang, prompt_path)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Confucius generation failed")
            raise HTTPException(
                status_code=500,
                detail={"error": "generation failed on the worker",
                        "code": "generation_failed"})
        finally:
            Path(prompt_path).unlink(missing_ok=True)
        return Response(content=wav, media_type="audio/wav")

    return app


def apply_offline_env(confucius_root: Path | None = None) -> None:
    """Pin repo-local weights and forbid downloads before any HF import."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if confucius_root is not None:
        os.environ["HF_HUB_CACHE"] = str(confucius_root / "models" / "hf")


def create_directory_junction(link: Path, target: Path) -> None:
    """Windows directory junction (no admin rights needed) or a symlink."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, shell=False)
        if result.returncode != 0 or not link.exists():
            raise RuntimeError(
                f"mklink /J {link} -> {target} failed:"
                f" {result.stderr.strip()[:200]}")
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def ensure_checkpoints_link(ccvt_root: Path, confucius_root: Path) -> None:
    """Satisfy ./checkpoints relative paths without touching the Confucius
    checkout or switching the interpreter's working directory."""
    link = ccvt_root / "checkpoints"
    if link.exists():
        return
    create_directory_junction(link, confucius_root / "checkpoints")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Confucius4-TTS local worker (basic Transformers)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=None,
                        help="Confucius4-TTS checkout; required here or via"
                             " $CONFUCIUS4_TTS_ROOT (never hardcoded in the"
                             " committed launcher)")
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)

    root_value = args.root or os.environ.get("CONFUCIUS4_TTS_ROOT")
    if not root_value:
        parser.error("--root or CONFUCIUS4_TTS_ROOT is required")
    root = Path(root_value)
    if not root.is_dir():
        print(f"Error: Confucius4-TTS root not found: {root}", file=sys.stderr)
        sys.exit(2)
    apply_offline_env(root)
    config_path = root / "config" / "inference_config.yaml"
    sys.path.insert(0, str(root))  # confuciustts + external/ live in the repo
    try:
        ensure_checkpoints_link(Path.cwd(), root)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    state = WorkerState()
    start_loader(state, config_path, args.device)
    import uvicorn

    uvicorn.run(create_worker_app(state), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行 worker 测试通过**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_worker.py' -v
```

Expected: PASS（uv 自动同步新 dev 依赖并更新 `uv.lock`）。

- [ ] **Step 5: 写 client 失败测试**

创建 `local-gateway/tests/test_confucius_client.py`：

```python
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


def client_with(session) -> cc.ConfuciusClient:
    with mock.patch.object(cc.requests, "Session", return_value=session):
        return cc.ConfuciusClient("http://127.0.0.1:8765")


class HealthTests(unittest.TestCase):
    def test_ok_payload_is_returned(self):
        session = FakeSession(response=FakeStreamResponse(
            json_data={"status": "ok", "sample_rate": 22050}))
        payload = client_with(session).health()
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
                self.assertEqual(client_with(session).health(), payload)

    def test_http_503_without_json_is_unavailable(self):
        session = FakeSession(response=FakeStreamResponse(
            status_code=503, json_error=True))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            client_with(session).health()
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_connection_error_is_unavailable(self):
        session = FakeSession(
            exc=requests.exceptions.ConnectionError("refused"))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            client_with(session).health()
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_timeout_is_timeout(self):
        session = FakeSession(
            exc=requests.exceptions.ReadTimeout("slow"))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            client_with(session).health()
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")

    def test_non_object_json_is_protocol_error(self):
        session = FakeSession(response=FakeStreamResponse(json_data=[1]))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            client_with(session).health()
        self.assertEqual(ctx.exception.code, "tts_backend_error")

    def test_missing_status_string_is_protocol_error(self):
        session = FakeSession(response=FakeStreamResponse(json_data={}))
        with self.assertRaises(cc.ConfuciusError) as ctx:
            client_with(session).health()
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
```

- [ ] **Step 6: 运行确认失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_client.py' -v
```

Expected: FAIL/ERROR（`No module named 'local_gateway.confucius_client'`）。

- [ ] **Step 7: 实现 confucius_client.py**

创建 `local-gateway/local_gateway/confucius_client.py`：

```python
"""HTTP client for the Confucius4-TTS worker (health + one multipart post).

Deliberately independent of ComfyUIClient: no prompts, no history, no
/view, no upload. Each call opens one requests.Session with trust_env=False
and closes it; the POST is sent exactly once (no retry) with a short
connect timeout and the remaining request budget as the read timeout.
Response bodies are streamed and bounded; worker tracebacks and absolute
paths never cross this boundary.
"""
from __future__ import annotations

import time

import requests

CONNECT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 256 * 1024


class ConfuciusError(Exception):
    """Structured worker error: message + route-level code."""

    def __init__(self, message: str, code: str = "tts_backend_error",
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


def _from_requests(e: Exception) -> ConfuciusError:
    if isinstance(e, requests.exceptions.Timeout):
        return ConfuciusError(
            f"Confucius worker HTTP request timed out: {e}",
            code="tts_backend_timeout")
    if isinstance(e, requests.exceptions.ConnectionError):
        return ConfuciusError(
            f"Cannot reach the Confucius worker: {e}",
            code="tts_backend_unavailable")
    return ConfuciusError(
        f"Confucius worker request failed: {e}", code="tts_backend_error")


def _status_code_to_error(status_code: int) -> str:
    if status_code == 503:
        return "tts_backend_unavailable"
    if status_code == 504:
        return "tts_backend_timeout"
    return "tts_backend_error"


class ConfuciusClient:
    """Small typed wrapper over the worker HTTP API."""

    def __init__(self, base_url: str, connect_timeout: float = CONNECT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self.base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._max_response_bytes = max_response_bytes

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        return session

    def health(self) -> dict:
        """GET /health as a dict; transport failures raise ConfuciusError."""
        try:
            with self._session() as session:
                response = session.get(
                    self.base_url + "/health",
                    timeout=(self._connect_timeout, 10.0))
        except requests.exceptions.RequestException as e:
            raise _from_requests(e) from e
        try:
            if response.status_code not in (200, 503):
                raise ConfuciusError(
                    "Confucius worker /health returned HTTP"
                    f" {response.status_code}",
                    code=_status_code_to_error(response.status_code))
            try:
                # A worker deliberately serves starting/error as HTTP 503 with
                # a valid JSON state. Preserve that state for health aggregate;
                # preflight, not this transport method, decides readiness.
                payload = response.json()
            except ValueError as e:
                code = ("tts_backend_unavailable"
                        if response.status_code == 503 else "tts_backend_error")
                raise ConfuciusError(
                    "Confucius worker /health returned a non-JSON body",
                    code=code) from e
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise ConfuciusError(
                "Confucius worker /health returned JSON that is not an"
                " object", code="tts_backend_error")
        if not isinstance(payload.get("status"), str) or not payload["status"]:
            raise ConfuciusError(
                "Confucius worker /health has no status string",
                code="tts_backend_error")
        return payload

    def generate(self, *, text: str, lang: str, reference_bytes: bytes,
                 filename: str, content_type: str,
                 deadline: float) -> bytes:
        """One multipart POST /api/tts; returns bounded PCM16 WAV bytes."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConfuciusError(
                "clone deadline reached before the worker POST",
                code="tts_backend_timeout")
        try:
            with self._session() as session:
                response = session.post(
                    self.base_url + "/api/tts",
                    data={"text": text, "lang": lang},
                    files={"reference": (filename, reference_bytes,
                                         content_type)},
                    stream=True,
                    timeout=(self._connect_timeout, remaining))
        except requests.exceptions.RequestException as e:
            raise _from_requests(e) from e
        try:
            if response.status_code != 200:
                raise ConfuciusError(
                    f"Confucius worker returned HTTP {response.status_code}"
                    " for POST /api/tts; response body is intentionally not"
                    " forwarded",
                    code=_status_code_to_error(response.status_code))
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                if time.monotonic() >= deadline:
                    raise ConfuciusError(
                        "clone deadline reached while reading the worker WAV",
                        code="tts_backend_timeout")
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise ConfuciusError(
                        "Confucius worker response exceeds the"
                        f" {self._max_response_bytes}-byte limit after"
                        f" {total} bytes", code="tts_backend_error")
                chunks.append(chunk)
        except requests.exceptions.RequestException as e:
            raise _from_requests(e) from e
        finally:
            response.close()
        payload = b"".join(chunks)
        if not payload.startswith(b"RIFF"):
            raise ConfuciusError(
                "Confucius worker returned a non-WAV body",
                code="tts_backend_error")
        return payload
```

- [ ] **Step 8: 运行 client 测试通过**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_confucius_client.py' -v
```

Expected: PASS。

- [ ] **Step 9: 创建 start_confucius_worker.bat**

创建 `local-gateway/start_confucius_worker.bat`（LF 换行不强制，与 start_gateway.bat 一致）：

```bat
@echo off
rem Start the Confucius4-TTS worker on loopback 8765 in its isolated env.
rem E:\GitHub\_confucius4_tts_venv is cloned from the validated confuciustts
rem conda env and holds torch 2.7.0+cu128/transformers;
rem the gateway process itself never imports them. The worker resolves
rem ./checkpoints relative to the working directory, so launch it from the
rem repo root (the launcher picks the cwd once; the worker never chdirs).
cd /d "%~dp0.."
if not defined CONFUCIUS4_TTS_ROOT (
  echo Error: set CONFUCIUS4_TTS_ROOT to your local Confucius4-TTS checkout. 1>&2
  exit /b 2
)
rem The checkout path is process-local configuration, never a committed default.
"E:\GitHub\_confucius4_tts_venv\Scripts\python.exe" "%~dp0local_gateway\confucius_worker.py" --root "%CONFUCIUS4_TTS_ROOT%" --host 127.0.0.1 --port 8765
```

在根 `.gitignore` 的 `scratch/` 规则后追加，防止目录联接后的权重被 `git add -A` 捕获：

```gitignore
# Confucius4-TTS relative checkpoint junction (local model weights)
/checkpoints/
```

- [ ] **Step 10: 全网关回归、lint、提交**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
git add local-gateway/local_gateway/confucius_worker.py \
  local-gateway/local_gateway/confucius_client.py \
  local-gateway/start_confucius_worker.bat .gitignore \
  local-gateway/pyproject.toml local-gateway/uv.lock \
  local-gateway/tests/test_confucius_worker.py \
  local-gateway/tests/test_confucius_client.py
git commit -m "FEAT: add the Confucius4-TTS worker and its gateway client"
```

Expected: 网关套件全绿（worker/client 为纯新增，旧测试不受影响）。

---

### Task 4: Gateway swap: registry, config, audio, tts orchestration, health, server

**Files:**
- Rewrite: `local-gateway/local_gateway/registry.py`（TTS 条目）、`local-gateway/local_gateway/audio.py`、`local-gateway/local_gateway/tts.py`、`local-gateway/local_gateway/server.py`
- Modify: `local-gateway/local_gateway/config.py`、`local-gateway/local_gateway/health.py`
- Delete: `local-gateway/local_gateway/tts_workflow.py`、`local-gateway/local_gateway/workflows/tts_voice_clone.json`、`local-gateway/tests/test_tts_workflow.py`
- Test (rewrite): `local-gateway/tests/test_tts.py`、`local-gateway/tests/test_audio.py`、`local-gateway/tests/test_health.py`
- Test (modify): `local-gateway/tests/test_config.py`

**Interfaces:**
- Consumes: Task 3 的 `ConfuciusClient`/`ConfuciusError`。
- Produces（Task 8 与真实验收依赖）:
  - `registry.MODEL_ENTRIES` 首条 `ModelEntry(key="confucius4-tts", capability="tts", modes=("clone",), languages=(14 码), output_formats=("mp3", "wav"), max_batch_items=8, deadline_chunk_seconds=270)`；`ROUTE_TARGETS["tts"] == "confucius4-tts"`；`IMPLEMENTED_ROUTES` 本任务仍为 `frozenset()`。
  - `config.GatewaySettings.tts_worker_url: str`；`load_settings` 读 `CONFUCIUS4_TTS_BASE_URL`（默认 `http://127.0.0.1:8765`）。
  - `audio.finalize_output(wav_bytes: bytes, output_format: str, workdir: Path, stem: str, budget: audio.ByteBudget, deadline: float) -> tuple[bytes, float]`；`audio.parse_wav_header(data) -> dict`；`audio.validate_wav(data) -> None`；`audio.AudioError(message, code)`，code ∈ {`generation_failed`, `tts_backend_timeout`}；`audio.ITEM_MAX_BYTES == 64MiB`、`audio.REQUEST_BUDGET_BYTES == 128MiB`。
  - `tts.generate_clone(settings, tts_client_factory, body, lock) -> dict`；`tts.parse_request(body) -> TTSRequest(texts, languages, output_format, model, ref_audio_bytes, ref_suffix)`；`tts.preflight(settings, tts_client_factory) -> None`；`tts.deadline_seconds(item_count) -> int`；`tts.TTSRouteError(message, code, details=None)`。
  - `health.build_health_payload(settings, client_factory, tts_client_factory) -> tuple[dict, int]`；payload 新增 `tts_backend: {base_url, status, sample_rate}`。
  - `server.create_app(settings=None, client_factory=None, tts_client_factory=None) -> FastAPI`；`server.default_tts_client_factory(base_url) -> ConfuciusClient`。

- [ ] **Step 1: 写失败的 registry/config 测试**

在 `local-gateway/tests/test_config.py` 末尾（`if __name__ == "__main__":` 之前）追加：

```python
class TtsWorkerUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def env(self, **overrides):
        env = {"COMFYUI_MODELS_ROOT": self.tmp.name}
        env.update(overrides)
        return env

    def test_default_worker_url_is_local_8765(self):
        settings = config.load_settings(env=self.env())
        self.assertEqual(settings.tts_worker_url, "http://127.0.0.1:8765")
        self.assertTrue(settings.config_valid)

    def test_worker_url_env_override(self):
        settings = config.load_settings(env=self.env(
            CONFUCIUS4_TTS_BASE_URL="http://localhost:8765"))
        self.assertEqual(settings.tts_worker_url, "http://localhost:8765")

    def test_invalid_worker_url_is_config_invalid(self):
        settings = config.load_settings(env=self.env(
            CONFUCIUS4_TTS_BASE_URL="http://10.0.0.5:8765"))
        self.assertFalse(settings.config_valid)
        self.assertIn("CONFUCIUS4_TTS_BASE_URL",
                      " ".join(e.message for e in settings.errors))
```

- [ ] **Step 2: 运行确认失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_config.py' -v
```

Expected: FAIL（`AttributeError: 'GatewaySettings' object has no attribute 'tts_worker_url'` 或等价失败）。health 的 registry 断言随 Step 4 的完整测试文件一起替换，此步不跑 health。

- [ ] **Step 3: 实现 registry 与 config**

`registry.py`：`ModelEntry` dataclass 在 `deadline_chunk_seconds: int = 0` 之后加一行字段：

```python
    languages: tuple[str, ...] = ()
```

同时把 `required_nonempty_directories` 上方仍提到 P2 Qwen/AILab 权重与 `snapshot_download` 的旧注释改为通用说明：`# Directories that must exist and contain at least one entry when used by a model entry. Confucius owns its HF cache and leaves this empty.`；不再保留旧 AILab 权重说明。

把 `qwen3-tts-1.7B` 的整个 `ModelEntry(...)` 替换为：

```python
    ModelEntry(
        key="confucius4-tts",
        capability="tts",
        modes=("clone",),
        languages=("zh", "en", "ja", "ko", "de", "fr", "es", "id",
                   "it", "ms", "pt", "ru", "th", "vi"),
        output_formats=("mp3", "wav"),
        max_batch_items=8,
        deadline_chunk_seconds=270,
    ),
```

`ROUTE_TARGETS` 的 `"tts"` 行改为 `"tts": "confucius4-tts",`。当前基线 `e09ce2c` 已把旧 Qwen route 标为 implemented；必须显式把 `IMPLEMENTED_ROUTES` 改回 `frozenset()`，注释改为 `# Confucius4-TTS remains unimplemented until the new spec section 14 acceptance passes.`。这是新路线重新打开验收门槛，不是删除或改写旧提交历史。

文件 docstring 首段中 "The P1 node sets are the health baseline only; P2-P4 must re-verify /object_info before building workflows." 替换为 "The image/video node sets are the health baseline only; P3/P4 must re-verify /object_info before building workflows. The TTS entry describes the Confucius4-TTS worker, not ComfyUI nodes."

`config.py`：`GatewaySettings` 在 `base_url: str` 之后加字段 `tts_worker_url: str`。`load_settings` 中 base_url 校验块之后追加：

```python
    raw_worker = (env.get("CONFUCIUS4_TTS_BASE_URL", "").strip()
                  or DEFAULT_TTS_WORKER_URL)
    tts_worker_url = raw_worker.rstrip("/")
    if not _base_url_ok(tts_worker_url):
        errors.append(SettingsError(
            "config_invalid",
            f"CONFUCIUS4_TTS_BASE_URL must be an http loopback URL like"
            f" {DEFAULT_TTS_WORKER_URL}, without credentials, query, fragment"
            f" or a path; got {tts_worker_url!r}.",
            "Fix CONFUCIUS4_TTS_BASE_URL in local-gateway/.env.",
        ))
```

并在 `DEFAULT_BASE_URL` 常量后加 `DEFAULT_TTS_WORKER_URL = "http://127.0.0.1:8765"`，`return GatewaySettings(...)` 中加 `tts_worker_url=tts_worker_url,`。

- [ ] **Step 4: 重写 test_health.py（完整新文件）**

用以下完整内容替换 `local-gateway/tests/test_health.py`（保留原文件中 image/video 相关断言的意图，新增 tts_backend 与 ready 新公式；`build_model_tree` 不再建 TTS 目录）：

```python
"""local_gateway registry, health aggregation and FastAPI /health tests.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v

Fakes only: temp model trees, scripted client metadata, no network, no real
.env, no real ComfyUI, no real worker.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import config, health  # noqa: E402
from local_gateway.comfyui import ComfyUIError  # noqa: E402
from local_gateway import registry  # noqa: E402
from local_gateway.registry import RequiredOption  # noqa: E402
from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_three_first_release_models_with_capabilities(self):
        self.assertEqual({e.key: e.capability for e in registry.MODEL_ENTRIES}, {
            "confucius4-tts": "tts",
            "qwen-image-2.1": "image",
            "minimax-h3-fl2va": "video",
        })

    def test_tts_entry_is_confucius4_clone_only(self):
        entry = registry.model_by_key("confucius4-tts")
        self.assertEqual(entry.required_directories, ())
        self.assertEqual(entry.required_nonempty_directories, ())
        self.assertEqual(entry.required_nodes, ())
        self.assertEqual(entry.required_options, ())
        self.assertEqual(entry.modes, ("clone",))
        self.assertEqual(entry.output_formats, ("mp3", "wav"))
        self.assertEqual(entry.languages, ("zh", "en", "ja", "ko", "de", "fr",
                                           "es", "id", "it", "ms", "pt", "ru",
                                           "th", "vi"))
        self.assertEqual(entry.max_batch_items, 8)
        self.assertEqual(entry.deadline_chunk_seconds, 270)

    def test_image_entry_files_nodes_and_options(self):
        entry = registry.model_by_key("qwen-image-2.1")
        self.assertEqual(entry.required_files, (
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ))
        self.assertIn(RequiredOption(
            "UNETLoader", "unet_name",
            ("qwen_image_2.1_turbo_int8_convrot.safetensors",)), entry.required_options)

    def test_video_entry_keeps_double_extension_and_optional_audio_vae(self):
        entry = registry.model_by_key("minimax-h3-fl2va")
        self.assertIn("vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
                      entry.required_files)
        self.assertEqual(entry.optional_files,
                         ("vae/minimax_h3_audio_vae_fp32.safetensors",))

    def test_route_targets_and_empty_implemented_set(self):
        self.assertEqual(registry.ROUTE_TARGETS, {
            "tts": "confucius4-tts",
            "image_generate": "qwen-image-2.1",
            "image_edit": "qwen-image-2.1",
            "video": "minimax-h3-fl2va",
        })
        # Task 8 (real acceptance) flips this to frozenset({"tts"}) and
        # updates this assertion in the same commit.
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset())

    def test_required_nodes_is_unique_and_has_no_tts_nodes(self):
        nodes = registry.required_nodes()
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertIn("UNETLoader", nodes)
        self.assertNotIn("AILab_Qwen3TTSVoiceClone_Advanced", nodes)

    def test_model_by_key_unknown_returns_none(self):
        self.assertIsNone(registry.model_by_key("no-such-model"))


FULL_STATS = {
    "system": {
        "os": "nt",
        "ram_total": 68611555328,
        "ram_free": 34000000000,
        "comfyui_version": "0.45.0",
        "python_version": "3.12",
        "argv": ["python", "main.py"],
        "exec_path": "C:/python/python.exe",
    },
    "devices": [
        {"name": "NVIDIA GeForce RTX 4090", "type": "cuda",
         "vram_total": 25769803776, "vram_free": 24000000000},
    ],
}


def object_info_fixture(**overrides):
    base = {
        "UNETLoader": {"input": {"required": [
            ["unet_name", ["qwen_image_2.1_turbo_int8_convrot.safetensors",
                           "minimax_h3_fl2va_pruned_int8_convrot.safetensors"]],
            ["weight_dtype", ["default", "fp8_e4m3fn"]],
        ]}},
        "CLIPLoader": {"input": {"required": [
            ["clip_name", ["qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
                           "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
                           "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"]],
            ["type", ["stable_diffusion", "qwen_image", "minimax"]],
        ]}},
        "VAELoader": {"input": {"required": [
            ["vae_name", ["qwen_image_2.1_vae_bf16.safetensors",
                          "minimax_h3_video_vae_fp16.safetensors",
                          "minimax_h3_image_vae_fp16.safetensors.safetensors"]],
        ]}},
        "TextEncodeQwenImage21": {"input": {"required": [
            ["width", "INT"], ["resolution", ["1024", "1280"]],
        ]}},
        "KSampler": {"input": {"required": [["seed", "INT"]]}},
        "SaveImage": {"input": {"required": [["filename_prefix", "STRING"]]}},
        "LoadImage": {"input": {"required": [["image", ["example.png"]]]}},
        "EmptyMiniMaxH3LatentAV": {"input": {"required": [["length", "INT"]]}},
        "MiniMaxH3ImageToVideo": {"input": {"required": [["denoise", "FLOAT"]]}},
        "VAEDecode": {"input": {"required": [["samples", "LATENT"]]}},
        "VHS_VideoCombine": {"input": {"required": [
            ["format", ["video/h264-mp4", "video/webm"]],
        ]}},
    }
    runtime = {}
    for node, meta in base.items():
        required = {}
        for spec in meta["input"]["required"]:
            required[spec[0]] = [spec[1], *spec[2:]]
        runtime[node] = {"input": {"required": required}}
    runtime.update(overrides)
    return runtime


def build_model_tree(root, *, include=("image", "video"),
                     optional_audio_vae=False):
    if "image" in include:
        for rel in (
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
    if "video" in include:
        for rel in (
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
    if optional_audio_vae:
        audio_vae = root / "vae" / "minimax_h3_audio_vae_fp32.safetensors"
        audio_vae.parent.mkdir(parents=True, exist_ok=True)
        audio_vae.write_bytes(b"x")


class FakeClient:
    """Scripted stand-in for ComfyUIClient: metadata in, no sockets."""

    def __init__(self, object_info=None, stats=None, exc=None):
        self.object_info_data = object_info if object_info is not None else {}
        self.stats = stats if stats is not None else FULL_STATS
        self.exc = exc
        self.object_info_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def system_stats(self):
        if self.exc is not None:
            raise self.exc
        return self.stats

    def object_info(self, node_name=None):
        self.object_info_calls.append(node_name)
        if node_name not in self.object_info_data:
            raise ComfyUIError(f"Invalid node class {node_name!r} requested",
                               code="node_missing")
        return self.object_info_data[node_name]


class FakeTTSClient:
    """Scripted stand-in for ConfuciusClient."""

    def __init__(self, health_payload=None, exc=None):
        self.health_payload = (health_payload if health_payload is not None
                               else {"status": "ok", "sample_rate": 22050})
        self.exc = exc

    def health(self):
        if self.exc is not None:
            raise self.exc
        return self.health_payload


class HealthAggregationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings_for(self, root, base_url=None, worker_url=None):
        env = {"COMFYUI_MODELS_ROOT": str(root)}
        if base_url is not None:
            env["COMFYUI_BASE_URL"] = base_url
        if worker_url is not None:
            env["CONFUCIUS4_TTS_BASE_URL"] = worker_url
        return config.load_settings(env=env)

    def payload_for(self, root, client, tts=None, base_url=None):
        tts_client = tts if tts is not None else FakeTTSClient()
        return health.build_health_payload(
            self.settings_for(root, base_url=base_url), lambda url: client,
            lambda url: tts_client)

    def test_full_ready_with_comfyui_down_returns_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            payload, status = self.payload_for(
                root, FakeClient(exc=ComfyUIError("refused",
                                                  code="comfyui_unreachable")))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["models_ready"])
        self.assertFalse(payload["comfyui"]["reachable"])
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": True})
        self.assertEqual(payload["tts_backend"], {
            "base_url": "http://127.0.0.1:8765",
            "status": "ok",
            "sample_rate": 22050,
        })
        self.assertEqual(payload["routes"], {
            "tts": {"implemented": False, "available": False},
            "image_generate": {"implemented": False, "available": False},
            "image_edit": {"implemented": False, "available": False},
            "video": {"implemented": False, "available": False},
        })

    def test_with_comfyui_up_stats_are_reported_and_models_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(status, 200)
        self.assertTrue(payload["comfyui"]["reachable"])
        self.assertEqual(payload["comfyui"]["version"], "0.45.0")
        self.assertEqual(payload["comfyui"]["gpu"]["name"],
                         "NVIDIA GeForce RTX 4090")
        self.assertEqual(set(payload["models"]),
                         {"confucius4-tts", "qwen-image-2.1",
                          "minimax-h3-fl2va"})
        self.assertTrue(all(m["ready"] for m in payload["models"].values()))
        self.assertEqual(payload["errors"], [])

    def test_worker_starting_blocks_ready_with_unavailable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()),
                tts=FakeTTSClient({"status": "starting", "sample_rate": None}))
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["tts_backend"]["status"], "starting")
        self.assertFalse(payload["models"]["confucius4-tts"]["ready"])
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("tts_backend_unavailable", codes)

    def test_worker_wrong_sample_rate_blocks_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()),
                tts=FakeTTSClient({"status": "ok", "sample_rate": 24000}))
        self.assertEqual(status, 503)
        self.assertFalse(payload["models"]["confucius4-tts"]["ready"])
        self.assertIn("tts_backend_unavailable",
                      [e["code"] for e in payload["errors"]])

    def test_worker_unreachable_maps_to_unavailable(self):
        from local_gateway.confucius_client import ConfuciusError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()),
                tts=FakeTTSClient(exc=ConfuciusError(
                    "Cannot reach the Confucius worker: refused",
                    code="tts_backend_unavailable")))
        self.assertEqual(status, 503)
        self.assertEqual(payload["tts_backend"]["status"], "unreachable")
        self.assertFalse(payload["models_ready"])

    def test_worker_base_url_comes_from_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            env = {"COMFYUI_MODELS_ROOT": str(root),
                   "CONFUCIUS4_TTS_BASE_URL": "http://127.0.0.1:9999"}
            settings = config.load_settings(env=env)
            payload, _ = health.build_health_payload(
                settings, lambda url: FakeClient(
                    object_info=object_info_fixture()),
                lambda url: FakeTTSClient())
        self.assertEqual(payload["tts_backend"]["base_url"],
                         "http://127.0.0.1:9999")

    def test_missing_video_files_report_relative_paths_and_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("image",))
            payload, status = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(status, 503)
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["files_ready"])
        self.assertEqual(video["missing_files"], [
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ])

    def test_image_video_node_gaps_do_not_block_ready(self):
        # Missing image/video nodes are handled by their own route preflight
        # (P3/P4); health only reports them.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture()
            meta.pop("VHS_VideoCombine")
            payload, status = self.payload_for(
                root, FakeClient(object_info=meta))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["nodes_ready"])
        self.assertEqual(video["missing_nodes"], ["VHS_VideoCombine"])
        self.assertIn("node_missing", [e["code"] for e in payload["errors"]])

    def test_missing_ffprobe_blocks_ready(self):
        def fake_which(name):
            return "/fake/bin/ffmpeg" if name == "ffmpeg" else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            with mock.patch("shutil.which", side_effect=fake_which):
                payload, status = self.payload_for(
                    root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["dependencies"],
                         {"ffmpeg": True, "ffprobe": False})
        self.assertTrue(all(not r["available"] for r in payload["routes"].values()))

    def test_missing_models_root_config_blocks_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            settings = config.load_settings(env={})
            payload, status = health.build_health_payload(
                settings, lambda url: FakeClient(object_info=object_info_fixture()),
                lambda url: FakeTTSClient())
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        # Legacy COMFYUI_MODELS_ROOT config still gates the aggregate P0-P4
        # health contract, but it must never make the Confucius model unready.
        self.assertTrue(payload["models"]["confucius4-tts"]["ready"])
        self.assertIn("config_missing",
                      [e["code"] for e in payload["errors"]])

    def test_unexpected_exception_is_internal_error_without_traceback(self):
        def boom(url):
            raise ZeroDivisionError("division by zero")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = self.payload_for(root, boom)
        self.assertEqual(status, 503)
        error = payload["errors"][0]
        self.assertEqual(error["code"], "internal_error")
        self.assertIn("ZeroDivisionError", error["message"])
        self.assertNotIn("Traceback", str(payload))
        self.assertIn("tts_backend", payload)

    def test_object_info_is_fetched_once_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(object_info=object_info_fixture())
            self.payload_for(root, client)
        self.assertEqual(len(client.object_info_calls),
                         len(set(client.object_info_calls)))
        self.assertEqual(set(client.object_info_calls),
                         set(registry.required_nodes()))


class ServerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/fake/bin/tool")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_app(self, root, client, tts=None):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": str(root)})
        return server.create_app(
            settings=settings, client_factory=lambda url: client,
            tts_client_factory=lambda url: (tts if tts is not None
                                            else FakeTTSClient()))

    def test_health_200_when_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            with TestClient(self.make_app(root, FakeClient(
                    object_info=object_info_fixture()))) as tc:
                response = tc.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ready"], True)
        self.assertEqual(body["tts_backend"]["status"], "ok")
        self.assertEqual(set(body["routes"]),
                         {"tts", "image_generate", "image_edit", "video"})

    def test_creating_the_app_makes_no_network_request_and_does_not_read_real_env(self):
        def never(url):
            raise AssertionError("create_app must not contact any backend")

        with mock.patch.object(server, "load_settings") as load:
            app = server.create_app(client_factory=never,
                                    tts_client_factory=never)
        load.assert_not_called()
        self.assertTrue(app.title)

    def test_default_client_factories_build_real_clients(self):
        comfy = server.default_client_factory("http://127.0.0.1:9188")
        try:
            self.assertEqual(comfy.base_url, "http://127.0.0.1:9188")
        finally:
            comfy.close()
        tts_client = server.default_tts_client_factory(
            "http://127.0.0.1:8765")
        self.assertEqual(tts_client.base_url, "http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: 运行确认 health 失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_config.py' -v
```

Expected: health FAIL（无 `tts_backend`、签名不符）；config PASS（Step 3 已实现）。

- [ ] **Step 6: 实现 health.py 修改**

`health.py` 顶部导入区加 `from .confucius_client import ConfuciusError`（放在 `from .comfyui import ...` 之后）。`build_health_payload` 与 `_build` 的签名都改为（保持 docstring 不变）：

```python
def build_health_payload(settings: GatewaySettings,
                         client_factory: ClientFactory,
                         tts_client_factory: ClientFactory) -> tuple[dict, int]:
```

`_build` 内、comfyui 检查块之后插入 tts_backend 聚合（在 `models: dict[str, dict] = {}` 之前）：

```python
    tts_backend = {"base_url": settings.tts_worker_url,
                   "status": "unreachable", "sample_rate": None}
    try:
        tts_health = tts_client_factory(settings.tts_worker_url).health()
        tts_backend["status"] = str(tts_health.get("status"))
        sample_rate = tts_health.get("sample_rate")
        tts_backend["sample_rate"] = (sample_rate
                                      if isinstance(sample_rate, int)
                                      else None)
        if (tts_backend["status"] != "ok"
                or tts_backend["sample_rate"] != 22050):
            errors.append(_err(
                "tts_backend_unavailable", "tts_backend",
                "Confucius4-TTS worker /health must report status=ok and"
                f" sample_rate=22050; got {tts_backend!r}.",
                "Wait for the worker to finish loading and verify its"
                " inference config uses a 22050 Hz target sample rate.",
            ))
    except ConfuciusError as e:
        errors.append(_err(
            e.code, "tts_backend",
            f"Confucius4-TTS worker is not serving clones: {e.message}",
            "Start the worker with local-gateway/start_confucius_worker.bat"
            " (127.0.0.1:8765), wait for its /health to report ok, then"
            " re-check this endpoint.",
        ))
```

模型循环改为：TTS 条目用 worker 状态，image/video 的 `ready` 只看文件（节点信息保留为报告项）。把 `models[entry.key] = {...}` 整块替换为：

```python
        if entry.capability == "tts":
            # Confucius owns its model/cache independently of ComfyUI. Neither
            # COMFYUI_MODELS_ROOT nor ComfyUI node metadata gates TTS readiness.
            ready = (tts_backend["status"] == "ok"
                     and tts_backend["sample_rate"] == 22050)
            models[entry.key] = {
                "ready": ready,
                "files_ready": ready,
                "nodes_ready": ready,
                "optional_files_present": [],
                "missing_files": [],
                "missing_nodes": [],
                "missing_loader_values": [],
            }
        else:
            # Image/video readiness is files-only: their node gaps are
            # handled by their own route preflight (P3/P4), never by the
            # global ready flag (the TTS route must not need ComfyUI).
            models[entry.key] = {
                "ready": files_ready,
                "files_ready": files_ready,
                "nodes_ready": nodes_ready,
                "optional_files_present": optional_present,
                "missing_files": missing_files,
                "missing_nodes": missing_nodes,
                "missing_loader_values": missing_values,
            }
```

末尾公式与 payload 替换为：

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
    # P2 explicitly supersedes P1's comfyui.reachable conjunct: the
    # Confucius worker is independent, so a stopped ComfyUI is diagnostic
    # only. Declared image/video files still feed models_ready; their node
    # gaps stay diagnostic until P3/P4 preflight.
    ready = settings.config_valid and models_ready and dependencies_ready

    payload = {
        "ready": ready,
        "models_ready": models_ready,
        "comfyui": comfyui,
        "tts_backend": tts_backend,
        "models": models,
        "dependencies": dependencies,
        "routes": routes,
        "errors": errors,
    }
    return payload, (200 if ready else 503)
```

`build_health_payload` 的异常 fallback payload 中 `"comfyui": {...}` 之后加：

```python
            "tts_backend": {
                "base_url": settings.tts_worker_url,
                "status": "unreachable",
                "sample_rate": None,
            },
```

- [ ] **Step 7: 运行 health/config 通过**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: PASS。

- [ ] **Step 8: 重写 test_audio.py（完整新文件）**

用以下完整内容替换 `local-gateway/tests/test_audio.py`：

```python
"""audio: WAV header validation, passthrough, MP3 transcode, budget.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v

ffmpeg/ffprobe run as real local subprocesses on tiny generated clips (no
network). The worker WAV input is generated with ffmpeg at 22050 Hz mono.
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

from local_gateway import audio  # noqa: E402

FAR_DEADLINE = 60.0


def make_wav(path: Path, *, rate="22050", channels="1", codec="pcm_s16le"):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=0.3",
         "-ar", rate, "-ac", channels, "-c:a", codec, str(path)],
        check=True, capture_output=True, shell=False)


class ClipMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.wav_path = root / "clip.wav"
        make_wav(cls.wav_path)
        cls.wav_bytes = cls.wav_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def deadline(self):
        return time.monotonic() + FAR_DEADLINE


class ParseWavHeaderTests(ClipMixin, unittest.TestCase):
    def test_worker_wav_parses_to_pcm16_mono_22050(self):
        fmt = audio.parse_wav_header(self.wav_bytes)
        self.assertEqual(fmt["audio_format"], 1)
        self.assertEqual(fmt["channels"], 1)
        self.assertEqual(fmt["sample_rate"], 22050)
        self.assertEqual(fmt["bits_per_sample"], 16)

    def test_not_riff_is_generation_failed(self):
        for bad in (b"", b"NOPE" + b"x" * 20, b"RIFF" + b"\x00" * 4 + b"NOPE"):
            with self.subTest(bad=bad[:8]):
                with self.assertRaises(audio.AudioError) as ctx:
                    audio.parse_wav_header(bad)
                self.assertEqual(ctx.exception.code, "generation_failed")

    def test_validate_wav_rejects_wrong_shape(self):
        variants = [
            ({"channels": "2"}, "mono"),
            ({"rate": "44100"}, "22050"),
            ({"codec": "pcm_f32le"}, "PCM16"),
        ]
        for kwargs, needle in variants:
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "v.wav"
                    make_wav(path, **kwargs)
                    with self.assertRaises(audio.AudioError) as ctx:
                        audio.validate_wav(path.read_bytes())
                self.assertEqual(ctx.exception.code, "generation_failed")
                self.assertIn(needle, ctx.exception.message)


class ByteBudgetTests(unittest.TestCase):
    def test_limits_are_the_spec_values(self):
        self.assertEqual(audio.ITEM_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(audio.REQUEST_BUDGET_BYTES, 128 * 1024 * 1024)

    def test_max_bytes_is_min_of_item_limit_and_remaining(self):
        budget = audio.ByteBudget(limit=100)
        self.assertEqual(budget.max_bytes_for_next(), 100)
        budget.spend(40)
        self.assertEqual(budget.max_bytes_for_next(), 60)
        budget.spend(60)
        self.assertEqual(budget.max_bytes_for_next(), 0)


class FinalizeOutputTests(ClipMixin, unittest.TestCase):
    def finalize(self, output_format, workdir, budget=None, stem="item_0",
                 data=None, deadline=None):
        return audio.finalize_output(
            data if data is not None else self.wav_bytes, output_format,
            workdir, stem, budget or audio.ByteBudget(),
            deadline or self.deadline())

    def test_wav_passthrough_returns_same_bytes_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, duration = self.finalize("wav", Path(tmp))
        self.assertEqual(data, self.wav_bytes)
        self.assertGreater(duration, 0)

    def test_mp3_is_a_real_libmp3lame_transcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            data, duration = self.finalize("mp3", workdir)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                 str(workdir / "item_0.mp3")],
                capture_output=True, text=True, shell=False)
        self.assertEqual(probe.stdout.strip(), "mp3")
        self.assertTrue(data[:3] == b"ID3" or data[:2] == b"\xff\xfb")
        self.assertGreater(duration, 0)

    def test_success_leaves_no_temp_files_in_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self.finalize("mp3", workdir)
            self.finalize("wav", workdir, stem="item_1")
            names = sorted(p.name for p in workdir.iterdir())
        self.assertEqual(names, ["item_0.mp3", "item_1.wav"])

    def test_ffmpeg_failure_leaves_no_final_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with mock.patch.object(
                    subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
                with self.assertRaises(audio.AudioError) as ctx:
                    self.finalize("mp3", workdir)
            self.assertEqual(ctx.exception.code, "tts_backend_timeout")
            self.assertEqual(list(workdir.glob("*.mp3")), [])

    def test_oversize_item_is_generation_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audio, "ITEM_MAX_BYTES", 8):
                with self.assertRaises(audio.AudioError) as ctx:
                    self.finalize("wav", Path(tmp))
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_exhausted_request_budget_is_generation_failed(self):
        budget = audio.ByteBudget(limit=10)
        budget.spend(10)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("wav", Path(tmp), budget=budget)
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_invalid_wav_is_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("wav", workdir, data=b"RIFFnope")
        self.assertEqual(ctx.exception.code, "generation_failed")

    def test_expired_deadline_is_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("wav", Path(tmp),
                              deadline=time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")

    def test_unknown_format_is_generation_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audio.AudioError) as ctx:
                self.finalize("opus", Path(tmp))
        self.assertEqual(ctx.exception.code, "generation_failed")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 9: 运行确认 audio 失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v
```

Expected: FAIL/ERROR（`finalize_output` 旧签名、无 `parse_wav_header`）。

- [ ] **Step 10: 重写 audio.py（完整新文件）**

用以下完整内容替换 `local-gateway/local_gateway/audio.py`：

```python
"""TTS audio pipeline: WAV validation, WAV/MP3 finalization, ffprobe, budget.

Input is always the worker's PCM16 mono 22050 Hz WAV bytes (never a ComfyUI
history entry, never a FLAC intermediate). mp3 is a real FFmpeg libmp3lame
transcode; wav is validated passthrough. Every final file is produced via a
temp name plus os.replace, must pass ffprobe with duration > 0, and every
failure raises AudioError carrying the route's code (generation_failed or
tts_backend_timeout). No TTS business parameters live here (tts.py owns
those).
"""
from __future__ import annotations

import os
import struct
import subprocess
import time
from pathlib import Path

ITEM_MAX_BYTES = 64 * 1024 * 1024          # per worker WAV payload
REQUEST_BUDGET_BYTES = 128 * 1024 * 1024   # all items of one request
WAV_SAMPLE_RATE = 22050
MP3_CODEC = "libmp3lame"
_STDERR_SNIPPET = 200


class AudioError(Exception):
    """Pipeline error with a route-level code."""

    def __init__(self, message: str, code: str = "generation_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


class ByteBudget:
    """Tracks the bytes still allowed for worker WAV payloads in one request."""

    def __init__(self, limit: int = REQUEST_BUDGET_BYTES):
        self.remaining = limit

    def max_bytes_for_next(self) -> int:
        return min(ITEM_MAX_BYTES, max(0, self.remaining))

    def spend(self, n: int) -> None:
        self.remaining -= n


def _remaining(deadline: float, stage: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise AudioError(
            f"clone deadline reached before {stage}",
            code="tts_backend_timeout")
    return left


def parse_wav_header(data: bytes) -> dict:
    """Walk the RIFF chunk list to the fmt chunk; return its PCM fields."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("payload is not a RIFF/WAVE stream")
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk_id == b"fmt ":
            if len(body) < 16:
                raise AudioError("WAV fmt chunk is truncated")
            (audio_format, channels, rate, _byte_rate, _block_align,
             bits) = struct.unpack_from("<HHIIHH", body, 0)
            return {"audio_format": audio_format, "channels": channels,
                    "sample_rate": rate, "bits_per_sample": bits}
        pos += 8 + size + (size & 1)
    raise AudioError("WAV stream has no fmt chunk")


def validate_wav(data: bytes) -> None:
    """Require PCM16, mono, 22050 Hz -- exactly what the worker must emit."""
    fmt = parse_wav_header(data)
    if fmt["audio_format"] != 1 or fmt["bits_per_sample"] != 16:
        raise AudioError(
            "worker WAV must be PCM16 (uncompressed 16-bit), got"
            f" audio_format={fmt['audio_format']}"
            f" bits={fmt['bits_per_sample']}")
    if fmt["channels"] != 1:
        raise AudioError(
            f"worker WAV must be mono, got {fmt['channels']} channels")
    if fmt["sample_rate"] != WAV_SAMPLE_RATE:
        raise AudioError(
            f"worker WAV must be {WAV_SAMPLE_RATE} Hz, got"
            f" {fmt['sample_rate']} Hz")


def ffprobe_duration(path: Path, deadline: float) -> float:
    """Run ffprobe on a file; require returncode 0 and duration > 0."""
    timeout = _remaining(deadline, "ffprobe")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AudioError(
            f"ffprobe timed out after {timeout:.0f}s",
            code="tts_backend_timeout") from e
    except OSError as e:
        raise AudioError(f"ffprobe failed to run: {e}") from e
    if result.returncode != 0:
        raise AudioError(
            "ffprobe rejected the generated audio:"
            f" {result.stderr.strip()[:_STDERR_SNIPPET]}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as e:
        raise AudioError(
            "ffprobe returned no parsable duration:"
            f" {result.stdout.strip()[:_STDERR_SNIPPET]!r}") from e
    if not duration > 0:
        raise AudioError(f"ffprobe reported duration {duration!r}")
    return duration


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _run_ffmpeg(args: list[str], deadline: float, stage: str) -> None:
    timeout = _remaining(deadline, stage)
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AudioError(
            f"{stage} timed out after {timeout:.0f}s",
            code="tts_backend_timeout") from e
    except OSError as e:
        raise AudioError(f"{stage} failed to run: {e}") from e
    if result.returncode != 0:
        raise AudioError(
            f"{stage} failed: {result.stderr.strip()[:_STDERR_SNIPPET]}")


def finalize_output(wav_bytes: bytes, output_format: str, workdir: Path,
                    stem: str, budget: ByteBudget,
                    deadline: float) -> tuple[bytes, float]:
    """Turn one worker WAV payload into verified final bytes + duration."""
    if not wav_bytes:
        raise AudioError("worker returned empty WAV bytes")
    if len(wav_bytes) > budget.max_bytes_for_next():
        raise AudioError(
            f"worker WAV output is {len(wav_bytes)} bytes; the per-item"
            f" limit is {ITEM_MAX_BYTES} bytes and this request has"
            f" {max(0, budget.remaining)} bytes of budget left")
    validate_wav(wav_bytes)
    budget.spend(len(wav_bytes))
    if output_format not in ("mp3", "wav"):
        raise AudioError(f"unsupported output_format: {output_format!r}")
    workdir.mkdir(parents=True, exist_ok=True)
    src_path = workdir / f"{stem}.src.wav"
    _write_atomic(src_path, wav_bytes)
    if output_format == "wav":
        final_path = workdir / f"{stem}.wav"
        os.replace(src_path, final_path)
        duration = ffprobe_duration(final_path, deadline)
        return final_path.read_bytes(), duration
    final_path = workdir / f"{stem}.mp3"
    tmp_path = workdir / f"{stem}.out.mp3"
    _run_ffmpeg(["ffmpeg", "-y", "-i", str(src_path),
                 "-c:a", MP3_CODEC, str(tmp_path)],
                deadline, "FFmpeg MP3 transcode")
    duration = ffprobe_duration(tmp_path, deadline)
    os.replace(tmp_path, final_path)
    src_path.unlink(missing_ok=True)
    return final_path.read_bytes(), duration
```

- [ ] **Step 11: 运行 audio 通过**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_audio.py' -v
```

Expected: PASS。

- [ ] **Step 12: 重写 test_tts.py（完整新文件）**

用以下完整内容替换 `local-gateway/tests/test_tts.py`：

```python
"""tts: Confucius request validation, preflight, orchestration, route.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v

Worker clients and PATH tools are faked/temp. ffmpeg/ffprobe run for real
only inside audio.finalize_output on tiny generated clips.
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

from local_gateway import audio, config, registry, tts  # noqa: E402
from local_gateway.confucius_client import ConfuciusError  # noqa: E402
from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

REF_BYTES = b"ID3fake-audio-bytes"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def valid_body(**overrides):
    body = {"text": "你好，世界", "ref_audio_base64": b64(REF_BYTES)}
    body.update(overrides)
    return body


class WavMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "clip.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.3", "-ar", "22050", "-ac", "1",
             "-c:a", "pcm_s16le", str(out)],
            check=True, capture_output=True, shell=False)
        cls.wav_bytes = out.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class FakeConfuciusClient:
    def __init__(self, *, health_payload=None, wav_bytes=None,
                 generate_exc=None):
        self.health_payload = (health_payload if health_payload is not None
                               else {"status": "ok", "sample_rate": 22050})
        self.wav_bytes = wav_bytes
        self.generate_exc = generate_exc
        self.generate_calls = []

    def health(self):
        if isinstance(self.health_payload, Exception):
            raise self.health_payload
        return self.health_payload

    def generate(self, *, text, lang, reference_bytes, filename,
                 content_type, deadline):
        self.generate_calls.append({
            "text": text, "lang": lang,
            "reference_bytes": reference_bytes,
            "filename": filename, "content_type": content_type,
            "deadline": deadline})
        if self.generate_exc is not None:
            raise self.generate_exc
        return self.wav_bytes


class DeadlineFormulaTests(unittest.TestCase):
    def test_matches_the_spec_formula(self):
        for count, expected in ((1, 270), (3, 540), (4, 540), (8, 810)):
            self.assertEqual(tts.deadline_seconds(count), expected, count)


class ErrorCodeMappingTests(unittest.TestCase):
    def test_spec_section_7_statuses(self):
        expected = {
            "mode_unsupported": 422, "validation_error": 422,
            "unknown_model": 422, "external_url_unsupported": 422,
            "dependency_missing": 503, "tts_backend_unavailable": 503,
            "tts_backend_timeout": 504, "tts_backend_error": 502,
            "generation_failed": 500,
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
        self.assertEqual(request.languages, ("zh",))
        self.assertEqual(request.output_format, "mp3")
        self.assertEqual(request.model, "confucius4-tts")
        self.assertEqual(request.ref_audio_bytes, REF_BYTES)
        self.assertEqual(request.ref_suffix, ".mp3")

    def test_mode_fields_are_mode_unsupported(self):
        for field, value in (
                ("ref_text", "逐字稿"), ("speaker", "Ryan"),
                ("instruct", "warm"), ("temperature", 0.7), ("top_p", 0.9),
                ("custom_voice", {"speaker": "Ryan"}),
                ("voice_design", {"instruct": "warm"})):
            with self.subTest(field=field):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(**{field: value}))
                self.assertEqual(ctx.exception.code, "mode_unsupported")
                self.assertIn(field, ctx.exception.message)

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

    def test_unknown_field_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(speed=1.5))
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertIn("speed", ctx.exception.message)

    def test_unknown_model_is_unknown_model(self):
        for model in ("qwen3-tts-1.7B", "confucius4-tts-xl"):
            with self.subTest(model=model):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(model=model))
                self.assertEqual(ctx.exception.code, "unknown_model")

    def test_non_tts_model_is_rejected(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(model="qwen-image-2.1"))
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_text_batch_and_char_limits(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=[f"t{i}" for i in range(9)]))
        self.assertEqual(ctx.exception.code, "validation_error")
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text="好" * 1025))
        self.assertEqual(ctx.exception.code, "validation_error")
        request = self.parse(valid_body(text=["好" * 1024, "ok"]))
        self.assertEqual(len(request.texts), 2)

    def test_empty_text_items_rejected(self):
        for bad in ("", "   ", ["ok", ""]):
            with self.subTest(bad=bad):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(text=bad))
                self.assertEqual(ctx.exception.code, "validation_error")

    def test_language_matrix(self):
        self.assertEqual(self.parse(valid_body()).languages, ("zh",))
        request = self.parse(valid_body(text=["a", "b"], language="en"))
        self.assertEqual(request.languages, ("en", "en"))
        request = self.parse(valid_body(text=["a", "b"], language=["ja", "ko"]))
        self.assertEqual(request.languages, ("ja", "ko"))
        for bad in ("Auto", "ZH", "klingon"):
            with self.subTest(bad=bad):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(language=bad))
                self.assertEqual(ctx.exception.code, "validation_error")
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(text=["a", "b"], language=["zh"]))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_output_format(self):
        request = self.parse(valid_body(output_format="wav"))
        self.assertEqual(request.output_format, "wav")
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse(valid_body(output_format="flac"))
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_ref_audio_base64_rules(self):
        request = self.parse(valid_body(
            ref_audio_base64="data:audio/wav;base64," + b64(b"RIFF")))
        self.assertEqual(request.ref_audio_bytes, b"RIFF")
        self.assertEqual(request.ref_suffix, ".wav")
        for bad, why in (
                ("data:audio/mpeg,AAAA", "no base64 param"),
                ("data:;base64,AAAA", "no mime"),
                ("not valid base64!", "invalid base64"),
                (b64(b""), "empty")):
            with self.subTest(why=why):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.parse(valid_body(ref_audio_base64=bad))
                self.assertEqual(ctx.exception.code, "validation_error")
        with mock.patch.object(tts, "MAX_REF_AUDIO_BYTES", 8):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.parse(valid_body(ref_audio_base64=b64(b"0123456789")))
            self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(tts.MAX_REF_AUDIO_BYTES, 16 * 1024 * 1024)

    def test_missing_ref_audio_is_validation_error(self):
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.parse({"text": "hi"})
        self.assertEqual(ctx.exception.code, "validation_error")


class PreflightTests(WavMixin, unittest.TestCase):
    def settings(self):
        return config.load_settings(env={"COMFYUI_MODELS_ROOT": "C:/x"})

    def test_missing_ffmpeg_is_dependency_missing_before_any_client(self):
        def never(url):
            raise AssertionError("preflight must fail before any client")

        with mock.patch("shutil.which", side_effect=lambda name: None):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), never)
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_worker_not_ok_is_unavailable(self):
        client = FakeConfuciusClient(
            health_payload={"status": "starting", "sample_rate": None})
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), lambda url: client)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")
        self.assertEqual(ctx.exception.http_status, 503)

    def test_worker_wrong_sample_rate_is_unavailable(self):
        client = FakeConfuciusClient(
            health_payload={"status": "ok", "sample_rate": 24000})
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), lambda url: client)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_worker_connection_error_maps_to_its_code(self):
        client = FakeConfuciusClient(health_payload=ConfuciusError(
            "Cannot reach the Confucius worker: refused",
            code="tts_backend_unavailable"))
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                tts.preflight(self.settings(), lambda url: client)
        self.assertEqual(ctx.exception.code, "tts_backend_unavailable")

    def test_ok_worker_passes(self):
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            tts.preflight(self.settings(),
                          lambda url: FakeConfuciusClient())


class GenerateCloneTests(WavMixin, unittest.TestCase):
    def run_clone(self, client, body, lock=None):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": "C:/x"})
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            return tts.generate_clone(settings, lambda url: client, body,
                                      lock or threading.Lock())

    def client(self, **kwargs):
        kwargs.setdefault("wav_bytes", self.wav_bytes)
        return FakeConfuciusClient(**kwargs)

    def test_single_text_success_shape(self):
        client = self.client()
        payload = self.run_clone(client, valid_body(output_format="wav"))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["model"], "confucius4-tts")
        self.assertGreaterEqual(payload["processing_time_seconds"], 0)
        self.assertEqual(len(payload["outputs"]), 1)
        item = payload["outputs"][0]
        self.assertEqual(item["audio_base64"], b64(self.wav_bytes))
        self.assertGreater(item["duration_seconds"], 0)
        self.assertEqual(payload["audio_base64"], item["audio_base64"])
        self.assertEqual(payload["duration_seconds"],
                         item["duration_seconds"])
        self.assertNotIn("audio_url", payload)
        self.assertNotIn("r2_key", payload)
        self.assertNotIn("ref_audio_url", payload)

    def test_batch_sends_per_item_multipart_with_same_reference(self):
        client = self.client()
        body = valid_body(text=["第一句", "第二句", "第三句"],
                          language=["zh", "en", "ja"])
        payload = self.run_clone(client, body)
        self.assertEqual(
            [(c["text"], c["lang"]) for c in client.generate_calls],
            [("第一句", "zh"), ("第二句", "en"), ("第三句", "ja")])
        references = {c["reference_bytes"] for c in client.generate_calls}
        self.assertEqual(references, {REF_BYTES})  # decoded once, sent per item
        self.assertTrue(all(c["filename"].startswith("ccvt_ref_")
                            for c in client.generate_calls))
        self.assertEqual(len(payload["outputs"]), 3)
        self.assertNotIn("audio_base64", payload)

    def test_item_failure_fails_whole_request_without_partials(self):
        client = self.client(generate_exc=ConfuciusError(
            "worker blew up", code="tts_backend_error"))
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.run_clone(client, valid_body(text=["a", "b"]))
        self.assertEqual(ctx.exception.code, "tts_backend_error")
        self.assertEqual(ctx.exception.http_status, 502)

    def test_deadline_exhaustion_is_timeout_without_generate(self):
        # monotonic answers: route entry, lock-stage check, then beyond
        # the deadline (started + 270s), so _require_remaining raises.
        client = self.client()
        real = time.monotonic()
        values = iter([real, real + 10 ** 6, real + 10 ** 6])
        with mock.patch.object(tts.time, "monotonic",
                               side_effect=lambda: next(values)):
            with self.assertRaises(tts.TTSRouteError) as ctx:
                self.run_clone(client, valid_body())
        self.assertEqual(ctx.exception.code, "tts_backend_timeout")
        self.assertEqual(client.generate_calls, [])

    def test_lock_wait_timeout_is_timeout_without_generate(self):
        lock = threading.Lock()
        lock.acquire()
        client = self.client()
        try:
            with mock.patch.object(tts, "deadline_seconds", return_value=0):
                with self.assertRaises(tts.TTSRouteError) as ctx:
                    self.run_clone(client, valid_body(), lock=lock)
            self.assertEqual(ctx.exception.code, "tts_backend_timeout")
            self.assertEqual(client.generate_calls, [])
        finally:
            lock.release()

    def test_lock_is_released_after_success_and_failure(self):
        lock = threading.Lock()
        self.run_clone(self.client(), valid_body(), lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        with self.assertRaises(tts.TTSRouteError):
            self.run_clone(self.client(generate_exc=ConfuciusError(
                "x", code="tts_backend_error")), valid_body(), lock=lock)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_validation_error_raises_before_the_lock(self):
        class TrackingLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.acquire_calls = []

            def acquire(self, blocking=True, timeout=-1):
                self.acquire_calls.append((blocking, timeout))
                return self._lock.acquire(blocking, timeout)

            def release(self):
                self._lock.release()

        lock = TrackingLock()
        with self.assertRaises(tts.TTSRouteError) as ctx:
            self.run_clone(self.client(), valid_body(mode="x"), lock=lock)
        self.assertEqual(ctx.exception.code, "mode_unsupported")
        self.assertEqual(lock.acquire_calls, [])


class RouteTests(WavMixin, unittest.TestCase):
    def make_client_app(self, client):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": "C:/x"})
        app = server.create_app(settings=settings,
                                tts_client_factory=lambda url: client)
        return TestClient(app)

    def client(self, **kwargs):
        kwargs.setdefault("wav_bytes", self.wav_bytes)
        return FakeConfuciusClient(**kwargs)

    def test_success_returns_200(self):
        with mock.patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.make_client_app(self.client()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertGreater(body["outputs"][0]["duration_seconds"], 0)

    def test_error_codes_map_to_spec_statuses(self):
        cases = [
            (valid_body(mode="custom_voice"), FakeConfuciusClient(), 422,
             "mode_unsupported"),
            (valid_body(ref_text="x"), FakeConfuciusClient(), 422,
             "mode_unsupported"),
            (valid_body(unknown=1), FakeConfuciusClient(), 422,
             "validation_error"),
            (valid_body(model="nope"), FakeConfuciusClient(), 422,
             "unknown_model"),
            (valid_body(ref_audio_url="https://x"), FakeConfuciusClient(),
             422, "external_url_unsupported"),
            (valid_body(), FakeConfuciusClient(
                health_payload={"status": "starting"}), 503,
             "tts_backend_unavailable"),
            (valid_body(), FakeConfuciusClient(
                health_payload=ConfuciusError(
                    "slow", code="tts_backend_timeout")), 504,
             "tts_backend_timeout"),
            (valid_body(), FakeConfuciusClient(
                generate_exc=ConfuciusError(
                    "x", code="tts_backend_error")), 502,
             "tts_backend_error"),
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

    def test_ffmpeg_missing_is_dependency_missing(self):
        with mock.patch("shutil.which", side_effect=lambda name: None):
            with self.make_client_app(self.client()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "dependency_missing")

    def test_unexpected_exception_is_generic_generation_failed(self):
        with mock.patch.object(tts, "generate_clone",
                               side_effect=RuntimeError("secret internals")):
            with self.make_client_app(FakeConfuciusClient()) as tc:
                response = tc.post("/tts/generate", json=valid_body())
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "generation_failed")
        self.assertNotIn("secret internals", body["error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 13: 运行确认 tts 失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_tts.py' -v
```

Expected: FAIL/ERROR（`parse_request` 仍要求 `ref_text`、路由仍走 ComfyUI）。

- [ ] **Step 14: 重写 tts.py（完整新文件）**

用以下完整内容替换 `local-gateway/local_gateway/tts.py`：

```python
"""POST /tts/generate orchestration: validation, preflight, deadline, lock.

Confucius4-TTS clone-only. This module never builds a FastAPI response
(server.py does); it returns a success payload dict or raises TTSRouteError
with the exact code/HTTP-status contract of spec section 7. The total
deadline starts at route entry (before waiting for the lock) and covers
preflight, every per-item multipart POST, the bounded WAV download, ffprobe
and FFmpeg. One lock per gateway process serializes whole clone requests;
/health never takes it (the worker keeps its own generation lock).
"""
from __future__ import annotations

import base64
import binascii
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import audio, registry
from .confucius_client import ConfuciusError
from .config import GatewaySettings

ALLOWED_FIELDS = frozenset({
    "model", "text", "mode", "language", "output_format",
    "ref_audio_base64",
})
# These describe capabilities the Confucius route deliberately does not
# have (Qwen sampling, builtin speakers, transcripts, designed voices):
# they fail as mode_unsupported, not as unknown fields.
MODE_FLAG_FIELDS = frozenset({
    "ref_text", "speaker", "instruct", "temperature", "top_p",
    "custom_voice", "voice_design",
})
EXTERNAL_FIELDS = frozenset({"ref_audio_url", "r2"})
OUTPUT_FORMATS = ("mp3", "wav")
MAX_REF_AUDIO_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 1024
SUPPORTED_LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "es", "id",
                       "it", "ms", "pt", "ru", "th", "vi")
_DATA_URL_PREFIX = "data:"
_MIME_SUFFIXES = {"audio/wav": ".wav", "audio/x-wav": ".wav",
                  "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
                  "audio/flac": ".flac", "audio/x-flac": ".flac"}
_REF_CONTENT_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                      ".flac": "audio/flac"}

ERROR_STATUS = {
    "mode_unsupported": 422,
    "validation_error": 422,
    "unknown_model": 422,
    "external_url_unsupported": 422,
    "dependency_missing": 503,
    "tts_backend_unavailable": 503,
    "tts_backend_timeout": 504,
    "tts_backend_error": 502,
    "generation_failed": 500,
}


class TTSRouteError(Exception):
    """Route-level error carrying a spec section 7 code and HTTP status."""

    def __init__(self, message: str, code: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details
        self.http_status = ERROR_STATUS.get(code, 500)


def _from_confucius(e: ConfuciusError) -> TTSRouteError:
    return TTSRouteError(e.message, e.code, details=e.details)


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


def _validation(message: str) -> TTSRouteError:
    return TTSRouteError(message, "validation_error")


def _dependency(message: str) -> TTSRouteError:
    return TTSRouteError(message, "dependency_missing")


def parse_request(body) -> TTSRequest:
    """Validate the clone request body (spec section 5). Raises TTSRouteError."""
    if not isinstance(body, dict):
        raise _validation("request body must be a JSON object")
    mode_flags = sorted(set(body) & MODE_FLAG_FIELDS)
    if mode_flags:
        raise TTSRouteError(
            "this route supports voice clone only; field(s)"
            f" {', '.join(mode_flags)} belong to capabilities that are not"
            " implemented on the Confucius4-TTS route (clone needs only"
            " ref_audio_base64 and language)",
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
            f"mode {mode!r} is not supported; this route implements"
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
        texts = list(text)
    else:
        raise _validation(
            "text is required: a non-empty string or a list of them")
    for i, item in enumerate(texts):
        if not isinstance(item, str) or not item.strip():
            raise _validation(f"text[{i}] must be a non-empty string")
        if len(item) > MAX_TEXT_CHARS:
            raise _validation(
                f"text[{i}] exceeds {MAX_TEXT_CHARS} characters"
                f" (got {len(item)})")

    language = body.get("language", "zh")
    if isinstance(language, str):
        languages = [language] * len(texts)
    elif isinstance(language, list):
        if len(language) != len(texts):
            raise _validation(
                f"language list must match text length {len(texts)},"
                f" got {len(language)}")
        languages = list(language)
    else:
        raise _validation("language must be a string or a list of strings")
    for i, item in enumerate(languages):
        if item not in SUPPORTED_LANGUAGES:
            raise _validation(
                f"language[{i}] must be one of"
                f" {', '.join(SUPPORTED_LANGUAGES)}; got {item!r}")

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
                f"unknown model {model!r}; this route accepts only"
                f" {registry.ROUTE_TARGETS['tts']}",
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
        header = payload_text[len(_DATA_URL_PREFIX):comma]
        params = [p.strip().lower() for p in header.split(";") if p.strip()]
        mime = params[0] if params else ""
        if not mime:
            raise _validation(
                "ref_audio_base64 data URL has no MIME type")
        if "base64" not in params[1:]:
            raise _validation(
                "ref_audio_base64 data URL must declare the base64"
                " parameter")
        payload_text = payload_text[comma + 1:]
        ref_suffix = _MIME_SUFFIXES.get(mime, ".bin")
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

    return TTSRequest(
        texts=tuple(texts), languages=tuple(languages),
        output_format=output_format, model=model,
        ref_audio_bytes=ref_bytes, ref_suffix=ref_suffix)


def preflight(settings: GatewaySettings, tts_client_factory) -> None:
    """Dependency check (spec section 7); runs before any generation POST."""
    missing_tools = [tool for tool in ("ffmpeg", "ffprobe")
                     if shutil.which(tool) is None]
    if missing_tools:
        raise _dependency(
            "voice clone needs ffmpeg and ffprobe on this gateway process's"
            f" PATH; missing: {', '.join(missing_tools)}")
    try:
        payload = tts_client_factory(settings.tts_worker_url).health()
    except ConfuciusError as e:
        raise _from_confucius(e) from e
    if (payload.get("status") != "ok"
            or payload.get("sample_rate") != 22050):
        raise TTSRouteError(
            "Confucius4-TTS worker is not ready: /health must report"
            " status='ok' and sample_rate=22050; got status="
            f"{payload.get('status')!r}, sample_rate="
            f"{payload.get('sample_rate')!r}", "tts_backend_unavailable")


def _require_remaining(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TTSRouteError(
            f"clone deadline reached at stage {stage}",
            "tts_backend_timeout")


def generate_clone(settings: GatewaySettings, tts_client_factory, body: dict,
                   lock) -> dict:
    """Validate, serialize on `lock`, and run the whole clone request."""
    started = time.monotonic()
    request = parse_request(body)  # 422-class errors raise before the lock
    deadline = started + deadline_seconds(len(request.texts))
    remaining = max(0.0, deadline - time.monotonic())
    if not lock.acquire(timeout=remaining):
        raise TTSRouteError(
            "timed out waiting for the gateway's TTS lock; the clone request"
            " was never submitted", "tts_backend_timeout")
    try:
        _require_remaining(deadline, "lock")
        return _generate_locked(settings, tts_client_factory, request,
                                deadline, started)
    finally:
        lock.release()


def _generate_locked(settings: GatewaySettings, tts_client_factory,
                     request: TTSRequest, deadline: float,
                     started: float) -> dict:
    _require_remaining(deadline, "preflight")
    preflight(settings, tts_client_factory)
    client = tts_client_factory(settings.tts_worker_url)
    ref_name = f"ccvt_ref_{uuid.uuid4().hex}{request.ref_suffix}"
    content_type = _REF_CONTENT_TYPES.get(request.ref_suffix,
                                          "application/octet-stream")
    budget = audio.ByteBudget()
    outputs: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="ccvt_tts_") as tmp:
        workdir = Path(tmp)
        for index, text in enumerate(request.texts):
            _require_remaining(deadline, f"item {index}")
            try:
                wav_bytes = client.generate(
                    text=text, lang=request.languages[index],
                    reference_bytes=request.ref_audio_bytes,
                    filename=ref_name, content_type=content_type,
                    deadline=deadline)
                audio_bytes, duration = audio.finalize_output(
                    wav_bytes, request.output_format, workdir,
                    f"item_{index}", budget, deadline)
            except ConfuciusError as e:
                raise _from_confucius(e) from e
            except audio.AudioError as e:
                raise TTSRouteError(e.message, e.code) from e
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

- [ ] **Step 15: 重写 server.py（完整新文件）**

用以下完整内容替换 `local-gateway/local_gateway/server.py`：

```python
"""FastAPI app factory, GET /health and POST /tts/generate.

The factory takes the settings plus two independent client factories (the
ComfyUI factory serves image/video model metadata in /health; the Confucius
factory serves the TTS route and the tts_backend health block) so tests
inject fakes. With no settings argument, local-gateway/.env is loaded lazily
inside the request, so importing the module or creating an app never reads
the developer's real .env and never opens a socket. The TTS route only
assembles dependencies and renders JSON; orchestration and the error
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
from .confucius_client import ConfuciusClient
from .config import GatewaySettings, load_settings
from .health import build_health_payload

ClientFactory = Callable[[str], ComfyUIClient]
TTSClientFactory = Callable[[str], ConfuciusClient]
logger = logging.getLogger(__name__)


def default_client_factory(base_url: str) -> ComfyUIClient:
    return ComfyUIClient(base_url)


def default_tts_client_factory(base_url: str) -> ConfuciusClient:
    return ConfuciusClient(base_url)


def create_app(settings: GatewaySettings | None = None,
               client_factory: ClientFactory | None = None,
               tts_client_factory: TTSClientFactory | None = None) -> FastAPI:
    app_settings = settings
    app_client_factory = (client_factory if client_factory is not None
                          else default_client_factory)
    app_tts_client_factory = (tts_client_factory if tts_client_factory
                              is not None else default_tts_client_factory)

    app = FastAPI(title="ccvt local gateway", version="0.1.0")
    app.state.tts_lock = threading.Lock()

    @app.get("/health")
    def health() -> JSONResponse:
        # Plain def on purpose: Starlette runs blocking requests calls in a
        # threadpool, so slow backend checks never block the event loop.
        active_settings = app_settings if app_settings is not None else load_settings()
        payload, status_code = build_health_payload(
            active_settings, app_client_factory, app_tts_client_factory)
        return JSONResponse(content=payload, status_code=status_code)

    @app.post("/tts/generate")
    def tts_generate(body: dict) -> JSONResponse:
        # Plain def: the clone request blocks (lock + worker POST) and must
        # not block the event loop; /health stays responsive without the lock.
        active_settings = app_settings if app_settings is not None else load_settings()
        try:
            payload = tts.generate_clone(active_settings,
                                         app_tts_client_factory, body,
                                         app.state.tts_lock)
        except tts.TTSRouteError as e:
            error = {"success": False, "error": e.message, "code": e.code}
            if e.details:
                error["details"] = e.details
            return JSONResponse(content=error, status_code=e.http_status)
        except Exception:
            logger.exception("Unexpected error while generating TTS clone")
            error = {"success": False, "error": "internal gateway error",
                     "code": "generation_failed"}
            return JSONResponse(content=error, status_code=500)
        return JSONResponse(content=payload, status_code=200)

    return app


app = create_app()
```

- [ ] **Step 16: 删除 ComfyUI TTS 遗留并跑全网关回归**

```bash
git rm local-gateway/local_gateway/tts_workflow.py \
  local-gateway/local_gateway/workflows/tts_voice_clone.json \
  local-gateway/tests/test_tts_workflow.py
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
```

Expected: 网关套件全绿（`test_comfyui.py`/`test_config.py` 不引用 tts_workflow；ruff 无 F 类错误）。

- [ ] **Step 17: 提交**

```bash
git add local-gateway/local_gateway/registry.py \
  local-gateway/local_gateway/config.py \
  local-gateway/local_gateway/audio.py \
  local-gateway/local_gateway/tts.py \
  local-gateway/local_gateway/health.py \
  local-gateway/local_gateway/server.py \
  local-gateway/tests/test_tts.py local-gateway/tests/test_audio.py \
  local-gateway/tests/test_health.py local-gateway/tests/test_config.py
git commit -m "FEAT: route the gateway TTS clone through the Confucius4 worker"
```

---

### Task 5: tools/confucius4_tts.py CLI

**Files:**
- Create: `tools/confucius4_tts.py`
- Test: `tests/python/test_confucius4_tts_local.py`（create）

**Interfaces:**
- Consumes: `cloud_gpu.call_cloud_endpoint(provider="local", ...)`（`_LOCAL_ENV_VARS["confucius4_tts"]`，Task 2 已注册）、`pacing.clamp_pace`/`pace_label`、`file_transfer`（完全不导入）。
- Produces（Task 6 依赖）: `confucius4_tts.generate_audio(text, output_path, lang="zh", ref_audio=None, output_format="mp3", timeout=300, model="confucius4-tts", cloud="local", verbose=True, max_wpm=None, progress=None) -> dict`；`confucius4_tts.MODEL == "confucius4-tts"`；`confucius4_tts.SUPPORTED_LANGUAGES`（14 码列表）；`confucius4_tts.parse_args()`。

- [ ] **Step 1: 写失败测试**

创建 `tests/python/test_confucius4_tts_local.py`：

```python
"""confucius4_tts: local clone-only CLI payload, guards and results.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_confucius4_tts_local.py -v

Every cloud call is faked. Nothing here contacts 127.0.0.1.
"""
from __future__ import annotations

import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import cloud_gpu  # noqa: E402
import confucius4_tts as c4  # noqa: E402


def run_generate(text, output, ref_path, cloud_output=None, **kwargs):
    if cloud_output is None:
        cloud_output = {"success": True, "outputs": [{
            "audio_base64": base64.b64encode(b"MP3DATA").decode("ascii"),
            "duration_seconds": 1.25,
        }]}
    captured = {}

    def fake_call(provider, payload, tool_name, timeout, **rest):
        captured["provider"] = provider
        captured["payload"] = payload
        captured["tool_name"] = tool_name
        captured["timeout"] = timeout
        return cloud_output, 0.5

    with mock.patch("cloud_gpu.call_cloud_endpoint",
                    side_effect=fake_call):
        result = c4.generate_audio(
            text=text, output_path=output, ref_audio=str(ref_path),
            verbose=False, **kwargs)
    return result, captured


class CliArgsTests(unittest.TestCase):
    def test_defaults_are_local_confucius4(self):
        with mock.patch.object(sys, "argv", [
                "confucius4_tts.py", "--text", "hi", "--ref-audio", "r.mp3",
                "--output", "o.mp3"]):
            args = c4.parse_args()
        self.assertEqual(args.cloud, "local")
        self.assertEqual(args.model, "confucius4-tts")
        self.assertEqual(args.lang, "zh")
        self.assertEqual(args.format, "mp3")
        self.assertEqual(args.timeout, 300)

    def test_cloud_has_no_non_local_choice(self):
        with mock.patch.object(sys, "argv", [
                "confucius4_tts.py", "--cloud", "modal", "--text", "hi",
                "--ref-audio", "r.mp3", "--output", "o.mp3"]):
            with self.assertRaises(SystemExit):
                c4.parse_args()

    def test_lang_only_accepts_the_14_codes(self):
        with mock.patch.object(sys, "argv", [
                "confucius4_tts.py", "--lang", "ZH", "--text", "hi",
                "--ref-audio", "r.mp3", "--output", "o.mp3"]):
            with self.assertRaises(SystemExit):
                c4.parse_args()


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_payload_is_clone_base64_without_qwen_fields(self):
        result, captured = run_generate(
            "你好", str(self.out), self.ref, model="confucius4-tts")
        self.assertTrue(result["success"], result)
        self.assertEqual(captured["provider"], "local")
        self.assertEqual(captured["tool_name"], "confucius4_tts")
        payload = captured["payload"]["input"]
        self.assertEqual(payload["mode"], "clone")
        self.assertEqual(payload["language"], "zh")
        self.assertEqual(payload["output_format"], "mp3")
        self.assertEqual(payload["model"], "confucius4-tts")
        self.assertEqual(payload["ref_audio_base64"],
                         base64.b64encode(b"REFERENCE-AUDIO").decode("ascii"))
        for forbidden in ("ref_text", "ref_audio_url", "r2", "speaker",
                          "instruct", "temperature", "top_p"):
            self.assertNotIn(forbidden, payload)

    def test_default_model_omits_model_field(self):
        _, captured = run_generate("你好", str(self.out), self.ref,
                                   model=None)
        self.assertNotIn("model", captured["payload"]["input"])

    def test_timeout_scales_with_batch(self):
        _, captured = run_generate(
            ["一", "二", "三", "四", "五"], [str(self.out)] * 5, self.ref,
            cloud_output={"success": True, "outputs": [
                {"audio_base64": base64.b64encode(b"M").decode("ascii"),
                 "duration_seconds": 1.0}] * 5})
        self.assertEqual(captured["timeout"], 300 * max(1, 5 // 3 + 1))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def reject(self, **kwargs):
        with mock.patch("cloud_gpu.call_cloud_endpoint",
                        side_effect=AssertionError("no HTTP allowed")):
            return c4.generate_audio(
                text="你好", output_path=str(self.out), verbose=False,
                **kwargs)

    def test_missing_ref_audio_rejected_before_http(self):
        result = self.reject()
        self.assertFalse(result["success"])
        self.assertIn("--ref-audio", result["error"])

    def test_unknown_model_rejected_before_http(self):
        result = self.reject(ref_audio=str(self.ref),
                             model="qwen3-tts-1.7B")
        self.assertFalse(result["success"])
        self.assertIn("confucius4-tts", result["error"])

    def test_non_local_cloud_rejected(self):
        result = self.reject(ref_audio=str(self.ref), cloud="modal")
        self.assertFalse(result["success"])
        self.assertIn("--cloud local", result["error"])

    def test_missing_ref_file_rejected(self):
        result = self.reject(ref_audio="does-not-exist.mp3")
        self.assertFalse(result["success"])


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.mp3"
        self.ref.write_bytes(b"REFERENCE-AUDIO")
        self.out = Path(self.tmp.name) / "out.mp3"

    def test_base64_result_writes_file_without_r2(self):
        result, _ = run_generate("你好", str(self.out), self.ref)
        self.assertTrue(result["success"])
        self.assertEqual(self.out.read_bytes(), b"MP3DATA")
        self.assertEqual(result["output"], str(self.out))
        self.assertIn("duration_seconds", result)

    def test_external_upload_and_download_functions_are_never_called(self):
        with mock.patch("file_transfer.upload_to_storage") as upload, \
                mock.patch("file_transfer.download_from_r2") as download_r2, \
                mock.patch("file_transfer.download_from_url") as download_url:
            result, _ = run_generate("你好", str(self.out), self.ref)
        self.assertTrue(result["success"])
        upload.assert_not_called()
        download_r2.assert_not_called()
        download_url.assert_not_called()

    def test_result_with_url_or_r2_key_fails_explicitly(self):
        for extra in ({"audio_url": "https://files.example/x.mp3"},
                      {"r2_key": "confucius/out.mp3"}):
            with self.subTest(extra=extra):
                result, _ = run_generate(
                    "你好", str(self.out), self.ref,
                    cloud_output={"success": True, "outputs": [
                        dict(extra, duration_seconds=1.0)]})
                self.assertFalse(result["success"])
                self.assertIn("base64", result["error"])

    def test_result_count_mismatch_fails(self):
        result, _ = run_generate(
            "你好", str(self.out), self.ref,
            cloud_output={"success": True, "outputs": []})
        self.assertFalse(result["success"])


class EnvMappingTests(unittest.TestCase):
    def test_tool_is_registered_for_local(self):
        self.assertEqual(cloud_gpu._LOCAL_ENV_VARS["confucius4_tts"],
                         "LOCAL_TTS_ENDPOINT_URL")


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["confucius4_tts.py"] + argv), \
                mock.patch("sys.stderr", err):
            try:
                c4.main()
                return 0, err.getvalue()
            except SystemExit as e:
                return e.code, err.getvalue()

    def test_missing_ref_audio_exits_1(self):
        code, text = self.run_main(["--text", "hi", "--output", "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("--ref-audio", text)

    def test_unknown_model_exits_1(self):
        code, text = self.run_main(["--text", "hi", "--ref-audio", "r.mp3",
                                    "--output", "o.mp3",
                                    "--model", "qwen3-tts-1.7B"])
        self.assertEqual(code, 1)
        self.assertIn("confucius4-tts", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_confucius4_tts_local.py -v
```

Expected: ERROR（`No module named 'confucius4_tts'`）。

- [ ] **Step 3: 实现 tools/confucius4_tts.py**

创建 `tools/confucius4_tts.py`：

```python
#!/usr/bin/env python3
"""
Generate speech using Confucius4-TTS on the local gateway (voice clone only).

The gateway route is clone-only: a reference clip plus a language code.
There is no ref_text, no built-in speaker, no instruct, no sampling knobs
and no R2 -- the reference is sent inline as base64 and the result comes
back inline as base64.

Usage:
    uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \
      --lang zh --text "这段声音由本地 Confucius4-TTS 克隆生成。" \
      --ref-audio C:/Users/kenyo/Downloads/Music/voice_02.mp3 \
      --format mp3 --output out.mp3

Requires the local stack: the Confucius worker (8765) and the gateway
(8123), plus LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate in
the root .env.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

MODEL = "confucius4-tts"
SUPPORTED_LANGUAGES = ["zh", "en", "ja", "ko", "de", "fr", "es", "id",
                       "it", "ms", "pt", "ru", "th", "vi"]
MAX_TEXT_CHARS = 1024
MAX_BATCH_ITEMS = 8


def get_audio_duration(file_path: str) -> float | None:
    """Audio duration in seconds via ffprobe, or None."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, shell=False)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, ValueError):
        pass
    return None


def generate_audio(
    text,                       # str OR list[str] for batch
    output_path,                # str OR list[str] (same shape as text)
    lang: str = "zh",
    ref_audio: str | None = None,
    output_format: str = "mp3",
    timeout: int = 300,
    model: str | None = MODEL,
    cloud: str = "local",
    verbose: bool = True,
    max_wpm: float | None = None,
    progress=None,
) -> dict:
    """Voice-clone one text (or a <=8 item batch) on the local gateway.

    Returns dict with:
      single call: {success, output, duration_seconds, duration_frames_30fps, script_chars, wpm, pacing}
      batch call:  {success, outputs: [{output, duration_seconds, ...}, ...], mode: clone}
    """
    is_batch = isinstance(text, list)
    texts = text if is_batch else [text]
    out_paths = output_path if is_batch else [output_path]
    if len(out_paths) != len(texts):
        return {"success": False, "error": "text and output_path must have matching lengths"}

    if cloud != "local":
        return {"success": False, "error": (
            "confucius4_tts runs on the local gateway only: pass"
            f" --cloud local (requested cloud: {cloud})")}
    if model is not None and model != MODEL:
        return {"success": False, "error": (
            f"unknown model {model!r}; this tool only accepts {MODEL}")}
    if not ref_audio:
        return {"success": False, "error": (
            "--ref-audio is required: the local route is voice clone only"
            " (no built-in speakers, no ref_text)")}
    ref_path = Path(ref_audio)
    if not ref_path.exists():
        return {"success": False, "error": f"Reference audio not found: {ref_audio}"}
    if lang not in SUPPORTED_LANGUAGES:
        return {"success": False, "error": (
            f"--lang must be one of {', '.join(SUPPORTED_LANGUAGES)}; got {lang!r}")}
    for i, item in enumerate(texts):
        if not isinstance(item, str) or not item.strip():
            return {"success": False, "error": f"text[{i}] must be a non-empty string"}
        if len(item) > MAX_TEXT_CHARS:
            return {"success": False, "error": (
                f"text[{i}] exceeds {MAX_TEXT_CHARS} characters (got {len(item)})")}
    if not 1 <= len(texts) <= MAX_BATCH_ITEMS:
        return {"success": False, "error": (
            f"batch must have 1..{MAX_BATCH_ITEMS} items, got {len(texts)}")}
    if output_format not in ("mp3", "wav"):
        return {"success": False, "error": (
            f"--format must be mp3 or wav, got {output_format!r}")}

    if verbose:
        print(f"Cloud provider: {cloud} (Confucius4-TTS voice clone,"
              f" {len(texts)} utterance{'s' if len(texts) > 1 else ''})",
              file=sys.stderr)

    payload = {
        "input": {
            "text": texts if is_batch else texts[0],
            "mode": "clone",
            "language": lang,
            "output_format": output_format,
            "ref_audio_base64": base64.b64encode(
                ref_path.read_bytes()).decode("ascii"),
        }
    }
    if model is not None:
        payload["input"]["model"] = model

    from cloud_gpu import call_cloud_endpoint

    effective_timeout = timeout * max(1, len(texts) // 3 + 1)
    output, _elapsed = call_cloud_endpoint(
        provider=cloud,
        payload=payload,
        tool_name="confucius4_tts",
        timeout=effective_timeout,
        poll_interval=3,
        progress_label="Cloning voice locally",
        verbose=verbose,
        progress=progress,
    )

    if isinstance(output, dict) and output.get("error"):
        return {"success": False, "error": output["error"]}

    outputs_list = None
    if isinstance(output, dict) and isinstance(output.get("outputs"), list):
        outputs_list = output["outputs"]
    if not outputs_list or len(outputs_list) != len(texts):
        return {"success": False, "error": (
            f"Expected {len(texts)} output(s), got"
            f" {len(outputs_list) if outputs_list else 0}:"
            f" {list(output.keys()) if isinstance(output, dict) else output}")}

    downloaded_items = []
    for i, (item, dest_path) in enumerate(zip(outputs_list, out_paths)):
        # Local results must be base64-only; any external reference is a
        # contract violation, not a download source.
        if item.get("r2_key") or item.get("audio_url"):
            return {"success": False, "error": (
                f"local gateway returned an external reference"
                f" (r2_key/audio_url) for output[{i}]; local results must"
                " be base64-only")}
        audio_base64 = item.get("audio_base64")
        if not audio_base64:
            return {"success": False, "error": (
                f"No audio_base64 in local output[{i}]:"
                f" {list(item.keys())}")}
        destination = Path(dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(audio_base64))
        if verbose:
            size_kb = destination.stat().st_size // 1024
            print(f"  Decoded from base64: {dest_path} ({size_kb}KB)",
                  file=sys.stderr)
        duration = get_audio_duration(dest_path)
        item_result = {
            "output": dest_path,
            "duration_seconds": round(duration, 2) if duration else None,
            "duration_frames_30fps": int(duration * 30) if duration else None,
            "script_chars": len(texts[i]),
        }
        from pacing import clamp_pace, pace_label
        if max_wpm:
            clamp = clamp_pace(dest_path, texts[i], max_wpm, verbose=verbose)
            if clamp.get("applied"):
                new_dur = clamp["duration_seconds"]
                item_result["duration_seconds"] = new_dur
                item_result["duration_frames_30fps"] = (
                    int(new_dur * 30) if new_dur else None)
                item_result["pace_adjusted"] = {
                    "original_wpm": clamp["original_wpm"],
                    "atempo": clamp["atempo"],
                    "floored": clamp["floored"],
                }
            elif clamp.get("error") and verbose:
                print(f"  Pace clamp skipped: {clamp['error']}", file=sys.stderr)
        wpm, label = pace_label(texts[i], item_result["duration_seconds"])
        item_result["wpm"] = wpm
        item_result["pacing"] = label
        downloaded_items.append(item_result)

    if is_batch:
        return {"success": True, "outputs": downloaded_items, "mode": "clone"}
    return {"success": True, **downloaded_items[0], "mode": "clone"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate speech with Confucius4-TTS on the local"
                    " gateway (voice clone only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \\
    --lang zh --text "你好，世界" --ref-audio ref.mp3 --output out.mp3
  uv run tools/confucius4_tts.py --lang en --format wav --ref-audio ref.mp3 \\
    --text "Hello" --output out.wav --timeout 420
        """,
    )
    parser.add_argument("--text", "-t", type=str, required=True,
                        help="Text to synthesize")
    parser.add_argument("--output", "-o", type=str, required=True,
                        help="Output audio file path (.mp3 or .wav)")
    parser.add_argument("--cloud", type=str, default="local",
                        choices=["local"],
                        help="Local backend (default and only: local)")
    parser.add_argument("--model", type=str, default=MODEL,
                        help=f"Model name (only {MODEL} is accepted)")
    parser.add_argument("--lang", type=str, default="zh",
                        choices=SUPPORTED_LANGUAGES,
                        help="Language code (default: zh)")
    parser.add_argument("--ref-audio", type=str,
                        help="Reference audio file for voice cloning"
                             " (required; no ref_text needed)")
    parser.add_argument("--format", type=str, default="mp3",
                        choices=["mp3", "wav"],
                        help="Output format (default: mp3)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Job timeout in seconds (default: 300;"
                             " batches scale it by count)")
    parser.add_argument("--max-wpm", type=float, default=None,
                        help="Pace clamp: slow takes exceeding this"
                             " words-per-minute (see tools/pacing.py)")
    parser.add_argument("--min-tempo", type=float, default=None,
                        help="Stretch floor for --max-wpm (default 0.85)")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON")
    return parser.parse_args()


def main():
    args = parse_args()
    verbose = not args.json
    if args.min_tempo is not None:
        import pacing
        try:
            pacing.set_min_tempo(args.min_tempo)
        except ValueError as e:
            print(f"Error: --min-tempo: {e}", file=sys.stderr)
            sys.exit(1)

    if args.model != MODEL:
        print(f"Error: unknown model {args.model!r}; this tool only"
              f" accepts {MODEL}", file=sys.stderr)
        sys.exit(1)
    if not args.ref_audio:
        print("Error: --ref-audio is required (voice clone only)",
              file=sys.stderr)
        sys.exit(1)
    if not Path(args.ref_audio).exists():
        print(f"Error: Reference audio not found: {args.ref_audio}",
              file=sys.stderr)
        sys.exit(1)

    progress = None
    if not args.json:
        from cloud_gpu import ProgressReporter
        progress = ProgressReporter(mode="human")

    result = generate_audio(
        text=args.text,
        output_path=args.output,
        lang=args.lang,
        ref_audio=args.ref_audio,
        output_format=args.format,
        timeout=args.timeout,
        model=args.model,
        cloud=args.cloud,
        verbose=verbose,
        max_wpm=args.max_wpm,
        progress=progress,
    )

    if not result.get("success"):
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Error: {result.get('error', 'Unknown error')}",
                  file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        duration = result.get("duration_seconds", 0)
        print(f"Generated: {result['output']}")
        if duration:
            print(f"  Duration: {duration:.1f}s ({int(duration * 30)}"
                  " frames @ 30fps)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行通过、lint、提交**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_confucius4_tts_local.py -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
git add tools/confucius4_tts.py tests/python/test_confucius4_tts_local.py
git commit -m "FEAT: add the confucius4_tts local clone CLI"
```

Expected: PASS。

---

### Task 6: voiceover confucius4 provider and --tts-model

**Files:**
- Modify: `tools/voiceover.py`
- Test: `tests/python/test_voiceover_confucius4.py`（create）

**Interfaces:**
- Consumes: Task 5 的 `confucius4_tts.generate_audio`/`SUPPORTED_LANGUAGES`；Task 2 的 `local_clone_error(provider, cloud, ref_audio, brand_design=False)`。
- Produces: `voiceover.generate_single_audio_confucius4(script, output_path, lang="zh", ref_audio=None, cloud="local", model=None, timeout=300, max_wpm=None, progress=None) -> dict`；`voiceover.generate_batch_audio_confucius4(scripts, output_paths, lang="zh", ...) -> list[dict]`；`process_scene_directory(..., provider="confucius4", lang, ref_audio, tts_model, timeout, ...)`；CLI `--tts-model`（`--qwen-model` 移除）、`--provider confucius4`。

- [ ] **Step 1: 写失败测试**

创建 `tests/python/test_voiceover_confucius4.py`：

```python
"""voiceover confucius4: local clone guards, --tts-model passthrough.

    TEMP='E:\\GitHub' TMP='E:\\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_confucius4.py -v

The confucius4_tts generator is faked; no network, no ElevenLabs SDK.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import confucius4_tts  # noqa: E402
import voiceover  # noqa: E402


class LocalCloneErrorTests(unittest.TestCase):
    def test_confucius4_local_with_ref_audio_is_fine(self):
        self.assertIsNone(voiceover.local_clone_error(
            "confucius4", "local", "ref.mp3"))

    def test_confucius4_without_ref_audio_requires_it(self):
        message = voiceover.local_clone_error("confucius4", "local", None)
        self.assertIsNotNone(message)
        self.assertIn("--ref-audio", message)
        self.assertNotIn("--ref-text", message)

    def test_confucius4_non_local_cloud_is_rejected(self):
        message = voiceover.local_clone_error("confucius4", "modal", "r.mp3")
        self.assertIsNotNone(message)
        self.assertIn("--cloud local", message)

    def test_confucius4_brand_design_is_rejected(self):
        message = voiceover.local_clone_error(
            "confucius4", "local", None, brand_design=True)
        self.assertIsNotNone(message)
        self.assertIn("clone only", message)

    def test_qwen3_local_is_rejected(self):
        message = voiceover.local_clone_error(
            "qwen3", "local", "ref.mp3")
        self.assertIsNotNone(message)
        self.assertIn("confucius4", message)

    def test_cloud_providers_are_unchanged(self):
        self.assertIsNone(voiceover.local_clone_error("qwen3", "modal", None))
        self.assertIsNone(voiceover.local_clone_error(
            "elevenlabs", "local", None))


def capture_generate(result=None):
    captured = {}
    default = {"success": True, "output": "out.mp3",
               "duration_seconds": 1.5}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return result if result is not None else default

    return captured, mock.patch.object(confucius4_tts, "generate_audio",
                                       side_effect=fake_generate)


class PassthroughTests(unittest.TestCase):
    def test_single_passes_model_lang_and_timeout(self):
        captured, patcher = capture_generate()
        with patcher:
            result = voiceover.generate_single_audio_confucius4(
                script="hi", output_path=Path("out.mp3"), lang="zh",
                ref_audio="ref.mp3", cloud="local",
                model="confucius4-tts", timeout=420)
        self.assertTrue(result["success"])
        self.assertEqual(captured["model"], "confucius4-tts")
        self.assertEqual(captured["lang"], "zh")
        self.assertEqual(captured["timeout"], 420)
        self.assertEqual(captured["cloud"], "local")
        self.assertEqual(captured["ref_audio"], "ref.mp3")

    def test_single_rejects_missing_ref_before_generator(self):
        captured, patcher = capture_generate()
        with patcher as generator:
            result = voiceover.generate_single_audio_confucius4(
                script="hi", output_path=Path("out.mp3"), cloud="local")
        self.assertFalse(result["success"])
        generator.assert_not_called()

    def test_single_rejects_non_local_cloud(self):
        captured, patcher = capture_generate()
        with patcher as generator:
            result = voiceover.generate_single_audio_confucius4(
                script="hi", output_path=Path("out.mp3"), cloud="modal",
                ref_audio="r.mp3")
        self.assertFalse(result["success"])
        generator.assert_not_called()

    def test_batch_passes_texts_and_model(self):
        captured, patcher = capture_generate(result={
            "success": True,
            "outputs": [{"output": "a.mp3"}, {"output": "b.mp3"}],
        })
        with patcher:
            results = voiceover.generate_batch_audio_confucius4(
                scripts=["a", "b"],
                output_paths=[Path("a.mp3"), Path("b.mp3")],
                lang="zh", ref_audio="ref.mp3", cloud="local",
                model="confucius4-tts", timeout=300)
        self.assertEqual(len(results), 2)
        self.assertEqual(captured["text"], ["a", "b"])
        self.assertEqual(captured["model"], "confucius4-tts")
        self.assertEqual(captured["lang"], "zh")

    def test_scene_directory_rejects_missing_ref_before_io(self):
        captured, patcher = capture_generate()
        with patcher as generator:
            results, _, _ = voiceover.process_scene_directory(
                Path("directory-that-must-not-be-read"),
                provider="confucius4", cloud="local")
        self.assertFalse(results[0]["success"])
        generator.assert_not_called()


class ArgsTests(unittest.TestCase):
    def parse(self, argv):
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv):
            return voiceover.parse_args()

    def test_tts_model_exists_and_qwen_model_is_gone(self):
        args = self.parse(["--provider", "confucius4", "--cloud", "local",
                           "--tts-model", "confucius4-tts", "--timeout",
                           "420", "--script", "s.txt", "--output", "o.mp3",
                           "--ref-audio", "r.mp3"])
        self.assertEqual(args.tts_model, "confucius4-tts")
        self.assertEqual(args.timeout, 420)
        with self.assertRaises(SystemExit):
            self.parse(["--provider", "qwen3", "--qwen-model", "x",
                        "--script", "s.txt", "--output", "o.mp3"])

    def test_provider_accepts_confucius4(self):
        args = self.parse(["--provider", "confucius4", "--cloud", "local",
                           "--script", "s.txt", "--output", "o.mp3",
                           "--ref-audio", "r.mp3"])
        self.assertEqual(args.provider, "confucius4")

    def test_elevenlabs_model_argument_is_unchanged(self):
        args = self.parse(["--model", "eleven_v4", "--script", "s.txt",
                           "--output", "o.mp3"])
        self.assertEqual(args.model, "eleven_v4")


class MainGuardTests(unittest.TestCase):
    def run_main(self, argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["voiceover.py"] + argv), \
                mock.patch("voiceover.load_dotenv"), \
                mock.patch("sys.stderr", err):
            try:
                voiceover.main()
                return 0, err.getvalue()
            except SystemExit as e:
                return e.code, err.getvalue()

    def test_confucius4_without_ref_audio_exits_1(self):
        code, text = self.run_main([
            "--provider", "confucius4", "--cloud", "local", "--script",
            "s.txt", "--output", "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("--ref-audio", text)

    def test_confucius4_explicit_speaker_exits_1(self):
        code, _ = self.run_main([
            "--provider", "confucius4", "--cloud", "local", "--speaker",
            "Gloria", "--ref-audio", "r.mp3", "--script", "s.txt",
            "--output", "o.mp3"])
        self.assertEqual(code, 1)

    def test_confucius4_ref_text_and_sampling_exit_1(self):
        for extra in (["--ref-text", "transcript"],
                      ["--temperature", "0.7"], ["--top-p", "0.8"]):
            with self.subTest(extra=extra):
                code, _ = self.run_main([
                    "--provider", "confucius4", "--cloud", "local",
                    "--ref-audio", "r.mp3", "--script", "s.txt",
                    "--output", "o.mp3", *extra])
                self.assertEqual(code, 1)

    def test_confucius4_clone_does_not_require_ref_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "script.txt"
            reference = root / "ref.mp3"
            output = root / "out.mp3"
            script.write_text("hello", encoding="utf-8")
            reference.write_bytes(b"reference")
            with mock.patch(
                    "voiceover.generate_single_audio_confucius4",
                    return_value={"success": True, "output": str(output),
                                  "duration_seconds": 1.0}) as generator:
                code, text = self.run_main([
                    "--provider", "confucius4", "--cloud", "local",
                    "--tts-model", "confucius4-tts", "--language", "zh",
                    "--ref-audio", str(reference), "--script", str(script),
                    "--output", str(output)])
        self.assertEqual(code, 0, text)
        generator.assert_called_once()
        self.assertEqual(generator.call_args.kwargs["lang"], "zh")
        self.assertEqual(generator.call_args.kwargs["model"],
                         "confucius4-tts")
        self.assertNotIn("ref_text", generator.call_args.kwargs)

    def test_qwen3_local_exits_1_pointing_at_confucius4(self):
        code, text = self.run_main([
            "--provider", "qwen3", "--cloud", "local", "--ref-audio",
            "r.mp3", "--ref-text", "t", "--script", "s.txt", "--output",
            "o.mp3"])
        self.assertEqual(code, 1)
        self.assertIn("confucius4", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_confucius4.py -v
```

Expected: FAIL/ERROR（provider 无 confucius4、无 `--tts-model`、无 confucius4 helper）。

- [ ] **Step 3: 实现 voiceover.py 修改（精确编辑）**

编辑一，`--provider` choices：

```python
        choices=["elevenlabs", "qwen3", "60db"],
```

替换为：

```python
        choices=["elevenlabs", "qwen3", "confucius4", "60db"],
```

编辑二，`--qwen-model` 参数整个替换为：

```python
    parser.add_argument(
        "--tts-model",
        type=str,
        default=None,
        help="Model name passed through on every qwen3/confucius4 path"
             " (e.g. confucius4-tts). The gateway validates it; default"
             " omits the field. Not the ElevenLabs --model.",
    )
```

同时把 `--ref-audio` help 改为 `"Reference audio for Qwen3 or Confucius4 voice cloning"`，把 `--ref-text` help 改为 `"Qwen3 cloud clone transcript (not accepted by confucius4)"`，避免 CLI 文档继续暗示 Confucius 需要逐字稿。

编辑三，在 `generate_batch_audio_qwen3` 之后新增两个 helper：

```python
def generate_single_audio_confucius4(
    script: str,
    output_path: Path,
    lang: str = "zh",
    ref_audio: str | None = None,
    cloud: str = "local",
    model: str | None = None,
    timeout: int = 300,
    max_wpm: float | None = None,
    progress=None,
) -> dict:
    """Generate a single audio file with Confucius4-TTS on the local gateway."""
    error = local_clone_error("confucius4", cloud, ref_audio)
    if error:
        return {"success": False, "error": error}

    from confucius4_tts import generate_audio

    output_path.parent.mkdir(parents=True, exist_ok=True)
    return generate_audio(
        text=script,
        output_path=str(output_path),
        lang=lang,
        ref_audio=ref_audio,
        output_format="mp3",
        timeout=timeout,
        model=model,
        cloud=cloud,
        verbose=False,
        max_wpm=max_wpm,
        progress=progress,
    )


def generate_batch_audio_confucius4(
    scripts: list[str],
    output_paths: list[Path],
    lang: str = "zh",
    ref_audio: str | None = None,
    cloud: str = "local",
    model: str | None = None,
    timeout: int = 300,
    max_wpm: float | None = None,
    progress=None,
) -> list[dict]:
    """Generate up to 8 audio files in one Confucius4-TTS gateway request."""
    error = local_clone_error("confucius4", cloud, ref_audio)
    if error:
        return [{"success": False, "error": error}]

    from confucius4_tts import generate_audio

    for p in output_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
    result = generate_audio(
        text=scripts,
        output_path=[str(p) for p in output_paths],
        lang=lang,
        ref_audio=ref_audio,
        output_format="mp3",
        timeout=timeout,
        model=model,
        cloud=cloud,
        verbose=False,
        max_wpm=max_wpm,
        progress=progress,
    )
    if not result.get("success"):
        err = result.get("error", "batch generation failed")
        return [{"success": False, "error": err, "output": str(p)}
                for p in output_paths]
    return [{"success": True, **item}
            for item in result.get("outputs", [])]
```

编辑四，`process_scene_directory` 签名中 `qwen_model: str | None = None,` 替换为 `tts_model: str | None = None,`；该函数既有 qwen3 batch 与 serial 调用中的 `model=qwen_model` 都改为 `model=tts_model`。函数内 `local_clone_error("qwen3", cloud, ref_audio)` 调用替换为：

```python
    error = local_clone_error(provider, cloud, ref_audio)
```

在既有 `if can_batch_qwen3:` 整个块（以 `return results, total_duration, total_chars` 结束）之后、`# Fallback: serial per-scene generation` 注释之前加：

```python
    can_batch_confucius4 = (
        provider == "confucius4"
        and 1 < len(scenes) <= 8  # the gateway accepts at most 8 items
    )

    if can_batch_confucius4:
        if not json_output:
            print(f"Batching {len(scenes)} scenes in one Confucius4 call — mode: clone (shared reference)", file=sys.stderr)
        batch_results = generate_batch_audio_confucius4(
            scripts=[s["script"] for s in scenes],
            output_paths=[s["mp3_file"] for s in scenes],
            lang=language,
            ref_audio=ref_audio,
            cloud=cloud,
            model=tts_model,
            timeout=timeout,
            max_wpm=max_wpm,
            progress=progress,
        )
        for s, r in zip(scenes, batch_results):
            r["script"] = str(s["txt_file"])
            results.append(r)
            if r.get("duration_seconds"):
                total_duration += r["duration_seconds"]
            if not json_output:
                if r.get("success"):
                    print(f"  {s['mp3_file'].name} ({r.get('duration_seconds', '?')}s{_pace_note(r)})", file=sys.stderr)
                else:
                    print(f"  {s['mp3_file'].name}  [FAILED: {r.get('error')}]", file=sys.stderr)
        return results, total_duration, total_chars
```

（`language` 形参对 confucius4 传入的是已归一的 14 码小写；`scene_instruct` 逻辑只对 `provider == "qwen3"` 生效，不用改。）串行 fallback 的 provider 分支中、`if provider == "qwen3":` 之前加：

```python
        if provider == "confucius4":
            result = generate_single_audio_confucius4(
                script=s["script"],
                output_path=s["mp3_file"],
                lang=language,
                ref_audio=ref_audio,
                cloud=cloud,
                model=tts_model,
                timeout=timeout,
                max_wpm=max_wpm,
                progress=progress,
            )
        elif provider == "qwen3":
```

（即原 `if provider == "qwen3":` 改为 `elif`。）

编辑五，`main()`：先把通用的 Qwen 参考逐字稿校验改为不拦截 Confucius：

```python
    if provider != "confucius4" and args.ref_audio and not args.ref_text:
        print("Error: --ref-text is required with --ref-audio", file=sys.stderr)
        sys.exit(1)
```

并把 `# Local gateway is clone-only (P2)` 下方的 `if provider == "qwen3":` 改为 `if provider in ("qwen3", "confucius4"):`，让缺 `--ref-audio` 和非 local cloud 在读取 script/scene 目录之前退出。

再在 `provider = args.provider` 之后加：

```python
    if provider == "confucius4":
        if args.language == "Auto":
            args.language = "zh"
        from confucius4_tts import SUPPORTED_LANGUAGES
        if args.language not in SUPPORTED_LANGUAGES:
            print(f"Error: --language must be one of"
                  f" {', '.join(SUPPORTED_LANGUAGES)} for confucius4"
                  f" (got {args.language!r})", file=sys.stderr)
            sys.exit(1)
        if (args.speaker != "Ryan" or args.instruct or args.tone
                or args.ref_text or args.temperature is not None
                or args.top_p is not None):
            print(
                "Error: --provider confucius4 supports voice clone only:"
                " --ref-text, built-in speakers, instruct/tone and sampling"
                " parameters are not available locally. Pass only"
                " --ref-audio plus the text/language options.",
                file=sys.stderr,
            )
            sys.exit(1)
```

brand 分支中 `if provider == "qwen3":` 块之前加：

```python
        if provider == "confucius4":
            confucius4_cfg = voice_config.get("confucius4", {})
            clone_cfg = confucius4_cfg.get("clone", {})
            if clone_cfg and not args.ref_audio:
                if "design" in clone_cfg:
                    design_guard = local_clone_error(
                        provider, args.cloud, None, brand_design=True)
                    if design_guard:
                        print(f"Error: {design_guard}", file=sys.stderr)
                        sys.exit(1)
                elif "refAudio" in clone_cfg:
                    ref_audio_path = get_brand_dir(args.brand) / clone_cfg["refAudio"]
                    if ref_audio_path.exists():
                        args.ref_audio = str(ref_audio_path)
                    else:
                        print(f"Warning: Clone ref audio not found: {ref_audio_path}", file=sys.stderr)
```

编辑六，`main()` 的 local guard 调用与 qwen3 单文件/scene 调用点：`guard = local_clone_error(provider, args.cloud, args.ref_audio)` 保持（Task 2 已改签名），guard 外层 provider 条件按编辑五扩展。所有 `qwen_model=args.qwen_model` 替换为 `tts_model=args.tts_model`（process_scene_directory 两处调用），既有 qwen3 单文件调用的 `model=args.qwen_model` 改为 `model=args.tts_model`。单文件生成分支 `if provider == "qwen3":` 之前加：

```python
    if provider == "confucius4":
        result = generate_single_audio_confucius4(
            script=script,
            output_path=output_path,
            lang=args.language,
            ref_audio=args.ref_audio,
            cloud=args.cloud,
            model=args.tts_model,
            timeout=args.timeout,
            max_wpm=args.max_wpm,
            progress=_progress_reporter(args),
        )
    elif provider == "qwen3":
```

（原 `if provider == "qwen3":` 改为 `elif`。）`provider_label` 字典 `{"qwen3": "Qwen3-TTS", "60db": "60db"}` 替换为 `{"qwen3": "Qwen3-TTS", "confucius4": "Confucius4-TTS", "60db": "60db"}`（两处）。模块 docstring 末尾示例后追加：

```text
    # Local Confucius4 voice clone
    uv run tools/voiceover.py --provider confucius4 --cloud local --tts-model confucius4-tts \
      --language zh --script script.txt --ref-audio ref.mp3 --output out.mp3
```

- [ ] **Step 4: 运行通过、根域回归、lint、提交**

```bash
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest tests/python/test_voiceover_confucius4.py -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
git add tools/voiceover.py tests/python/test_voiceover_confucius4.py
git commit -m "FEAT: add the confucius4 provider and --tts-model to voiceover"
```

Expected: 全绿。

---

### Task 7: Docs sync (roadmap, plan, README, env example)

**Files:**
- Modify: `docs/local-comfyui-provider-roadmap.md`
- Modify: `local-gateway/README.md`
- Modify: `local-gateway/.env.example`
- Modify: `docs/local-comfyui-provider-plan.md`（补充说明已在 Task 1；本任务只核对）

**Interfaces:**
- Consumes: Task 3-6 的真实接口（README 示例必须与之一致）。
- Produces: 文档描述 Confucius 路线；roadmap P2 checkbox 保持未勾（Task 8 勾）。

- [ ] **Step 1: 重写 gateway README 的相关章节**

`local-gateway/README.md` 全文替换为：

```markdown
# local-gateway

A loopback FastAPI gateway in front of two local backends: the Confucius4-TTS
worker for speech (P2) and, in later phases, a local ComfyUI install for
image/video. P2 provides `GET /health` and `POST /tts/generate`, which
supports voice clone only (mode `clone`, `mp3` or `wav`, reference audio
inline as `ref_audio_base64`, results inline as `audio_base64`). Custom
voice, voice design, built-in speakers and `ref_text` are not implemented on
this gateway. The image and video routes stay `implemented: false` until
later phases.

## Start order (P2)

1. Start the Confucius4-TTS worker first. It must answer on
   `127.0.0.1:8765`:

   ```bat
   set "CONFUCIUS4_TTS_ROOT=E:\path\to\Confucius4-TTS"
   local-gateway\start_confucius_worker.bat
   ```

   `CONFUCIUS4_TTS_ROOT` is required process-local configuration; the worker
   has no machine-specific checkout default. The worker uses an isolated
   environment (`E:\GitHub\_confucius4_tts_venv`) cloned from the validated
   `confuciustts` conda env, with basic Transformers and the repository-local
   offline Hugging Face cache at `models/hf`; the gateway
   process itself never imports torch. P2 does not need ComfyUI running.

2. Then start the gateway:

   ```bat
   local-gateway\start_gateway.bat
   ```

   The script uses its own directory, so you can run it from anywhere. The
   gateway binds `127.0.0.1:8123`.

## Configure

Copy `.env.example` to `.env` in this directory and edit it:

```
COMFYUI_BASE_URL=http://127.0.0.1:9188
COMFYUI_MODELS_ROOT=E:/path/to/ComfyUI/models
CONFUCIUS4_TTS_BASE_URL=http://127.0.0.1:8765
```

`CONFUCIUS4_TTS_BASE_URL` points at the Confucius worker (loopback http
only). `COMFYUI_*` stays for the image/video models of later phases.
Process environment variables win over `.env` values.

## Binding and security

- The gateway binds `127.0.0.1:8123` only and does not enable CORS. The
  worker binds `127.0.0.1:8765` only.
- The transport code sets `trust_env=False` on every requests session, which
  disables environment proxies explicitly. `NO_PROXY` is not a substitute
  for this.
- There is no token auth because both services are loopback only. Any future
  non-loopback exposure needs a new auth and access-control design.

## When /health fails

Read `errors[].action` in the response body and follow it. The gateway
returns HTTP 503 while configuration is invalid, any declared image/video
model files are missing, the TTS worker is not ok at 22050 Hz, or
ffmpeg/ffprobe are unavailable. ComfyUI process reachability and image/video
node metadata are diagnostic for P2 and do not block `ready`; declared
image/video files still feed `models_ready`. TTS generation preflight itself
needs only the worker and ffmpeg/ffprobe. `ready` never means every route
works: check each route's `implemented` flag.

## TTS voice clone

Point the toolkit at the route (root `.env`):

```
LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate
```

Then, for example:

```bash
uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \
  --lang zh --text "Hello" --ref-audio ref.mp3 --output out.mp3
```

Requirements:

- `ffmpeg` and `ffprobe` must be on the gateway process's PATH; `/health`
  reports both under `dependencies` and the gateway is not `ready` without
  them.
- The worker must report `status=ok` on `127.0.0.1:8765/health`
  (`tts_backend.status` in `/health`).
- One clone request runs at a time per gateway process (serialized on
  purpose); `/health` stays responsive. The worker serializes generation
  with its own lock.
```

- [ ] **Step 2: 更新 local-gateway/.env.example**

替换为：

```
COMFYUI_BASE_URL=http://127.0.0.1:9188
COMFYUI_MODELS_ROOT=E:/path/to/ComfyUI/models
CONFUCIUS4_TTS_BASE_URL=http://127.0.0.1:8765
```

- [ ] **Step 3: 重写 roadmap P2 章节**

`docs/local-comfyui-provider-roadmap.md`：

0. P1 当前映射行 `` `qwen3_tts` → `LOCAL_TTS_ENDPOINT_URL` `` 改为 `` `confucius4_tts` → `LOCAL_TTS_ENDPOINT_URL` ``；同节“网关不可达时提示先启动 ComfyUI 与 `start_gateway.bat`”改为“按能力先启动 Confucius worker 或 ComfyUI，再启动 `start_gateway.bat`”。P0/P1 规格中的历史事实不改，这里只同步 living roadmap 的当前行为。

1. 阶段总览表 P2 行替换为：

```markdown
| **P2** TTS 路由 | 独立 Confucius4-TTS worker（基础 Transformers）+ `/tts/generate` clone、`confucius4_tts.py` 与 `voiceover.py` 接 local | qwen3 已移除 local：`confucius4_tts` clone MP3、clone WAV、voiceover clone 三项真实生成通过；local clone 不触发外部上传 | M |
```

2. `### P2 TTS 路由（clone-only 修订）` 整节（含旧 checklist 与旧验收命令块，直到 `### P3` 之前）替换为：

```markdown
### P2 TTS 路由（Confucius4-TTS 修订）

依据 2026-11-18 Confucius4 设计规格（docs/superpowers/specs/2026-11-18-confucius4-tts-design.md）执行。旧 Qwen ComfyUI 路线（AILab 节点、ref_text、SaveAudioAdvanced）已废止；`qwen3_tts` 只保留 Modal/RunPod。

- [ ] 独立 worker（`local-gateway/local_gateway/confucius_worker.py`，端口 8765，隔离环境 `E:/GitHub/_confucius4_tts_venv`，由已验证 `confuciustts` 克隆）：加载一次 `ConfuciusTTS`，`/health` 三态，multipart `/api/tts` 返回 PCM16 mono 22050 Hz WAV；`HF_HUB_CACHE=<Confucius根>/models/hf` + 两个离线变量，不在生成时下载。
- [ ] 网关 `ConfuciusClient` + `/tts/generate`：仅 `mode=clone`，注册表 `confucius4-tts`，14 语言，`max_batch_items=8`，deadline `270 * max(1, item_count // 3 + 1)`，逐项 multipart、任一失败无部分结果；网关进程无 torch。
- [ ] 音频：WAV 直通（PCM16/mono/22050 校验）、MP3 用 FFmpeg libmp3lame 真转码，ffprobe 时长 > 0，临时文件 + 原子替换，64/128 MiB 界限。
- [ ] `tools/confucius4_tts.py`：`--cloud local`、`--model confucius4-tts`、`--lang zh`、`--ref-audio`、`--output`、`--format mp3|wav`、`--timeout 300`；无 ref_text/speaker/instruct/sampling；`cloud_gpu` 的 TTS local 映射只属 `confucius4_tts`。
- [ ] `tools/voiceover.py`：provider `confucius4` + `--tts-model`（取代 `--qwen-model`）；local 所有 single/batch/scene/brand clone 必须 `--ref-audio`、不要求 `--ref-text`；内置 speaker 与 brand VoiceDesign 在 local 下拒绝；`qwen3 + local` 不可用。
- [ ] `/health` 新增 `tts_backend: {base_url, status, sample_rate}`；TTS 就绪 = worker ok + 22050 Hz + ffmpeg/ffprobe，独立于 ComfyUI 模型根目录；ComfyUI 可达性和图像/视频节点缺项不阻塞 P2，图像/视频声明文件仍计入 `models_ready`。
- [ ] 单测：worker/client 契约、请求校验矩阵、批量顺序与上限、deadline/lock、音频管线、CLI/voiceover 透传与提前拒绝、health 聚合。全部 fake 网络。

验收（三项真实 clone，素材见规格第 14 节；先启动 8765 worker 再启动 8123 gateway，P2 不需要 ComfyUI）：按规格 14.2 执行 `tools/confucius4_tts.py` MP3/WAV 与 `tools/voiceover.py` clone，按 14.3 断言 ffprobe 编码/采样率/声道/时长与 `/health` JSON，并跑双测试域、ruff 与 copy lint。checkbox 只能在三项生成、health 与完整回归全部通过后勾选。
```

3. 附录 A 的 TTS 三行（`| TTS custom_voice |`、`| TTS clone |`、`| TTS voice_design |`）与 `| TTS 格式 |` 行替换为：

```markdown
| TTS clone（Confucius4） | `text` + `ref_audio_base64` + `language`（14 小写码）+ 可选 `model=confucius4-tts` | 网关逐项 multipart 转发 worker `/api/tts`；custom_voice、voice_design、ref_text、speaker、instruct 与 sampling 参数在本地不可用（`mode_unsupported`） |
| TTS 格式 | `output_format=mp3\|wav` | worker 固定输出 PCM16 mono 22050 Hz WAV；MP3 由网关用 libmp3lame 真转码，不改名伪装 |
```

附录 B 的 TTS 行 `- TTS：\`TTS/Qwen3-TTS/\` 目录及三种 AILab 节点。` 替换为：

```markdown
- TTS：Confucius4-TTS worker `127.0.0.1:8765`（仓库内 `models/hf` 缓存 T2S/S2A、w2v-bert-2.0、CAMPPlus、BigVGAN；`HF_HUB_CACHE` 指向该目录，不依赖 COMFYUI_MODELS_ROOT 或用户 home cache）。
```

P5 清单中 `voice.model` 经 `voiceover.py --qwen-model` 透传` 的 `--qwen-model` 改为 `--tts-model`。附录 A 表下方那句 "H3 网格、24 fps 与 `CLIPLoader.type` 已于 2026-11-17 通过本机源码和 `/object_info` 复核；TTS 三模式、`target_text`、保存节点、Qwen resolution 语义及 loader 权重清单也已通过运行时元数据复核。" 替换为 "H3 网格、24 fps 与 `CLIPLoader.type` 已于 2026-11-17 通过本机源码和 `/object_info` 复核；Qwen resolution 语义及 loader 权重清单也已通过运行时元数据复核。旧 TTS 节点复核结论随 Qwen 路线废止。"

- [ ] **Step 4: copy lint 与提交**

```bash
uv run tools/copy_lint.py docs/local-comfyui-provider-roadmap.md local-gateway/README.md local-gateway/.env.example
git add docs/local-comfyui-provider-roadmap.md local-gateway/README.md local-gateway/.env.example
git commit -m "DOCS: sync the roadmap and gateway README with the Confucius4 TTS route"
```

---

### Task 8: Real acceptance, IMPLEMENTED_ROUTES flip, final regression

**Files:**
- Modify: `local-gateway/local_gateway/registry.py`（`IMPLEMENTED_ROUTES`）
- Modify: `local-gateway/tests/test_health.py`（对应断言）
- Modify: `docs/local-comfyui-provider-roadmap.md`（勾选 P2 checkbox）

**Interfaces:**
- Consumes: Task 3-7 的全部产出；真实 worker/网关进程；素材 `C:/Users/kenyo/Downloads/Music/voice_02.mp3`。
- Produces: `IMPLEMENTED_ROUTES = frozenset({"tts"})`；P2 勾选；`scratch/local-comfyui/p2/` 下的三项真实产物、`health-pre-flip.json` 与 `health.json`。

- [ ] **Step 1: 核实离线前提（HF 缓存非空）**

```bash
set -euo pipefail
ROOT='E:/GitHub/Confucius4-TTS'
CACHE="$ROOT/models/hf"
for rel in \
  "models--netease-youdao--Confucius4-TTS/snapshots" \
  "models--facebook--w2v-bert-2.0/snapshots" \
  "models--funasr--campplus/snapshots" \
  "models--nvidia--bigvgan_v2_22khz_80band_256x/snapshots"; do
  test -d "$CACHE/$rel" && test -n "$(ls -A "$CACHE/$rel" 2>/dev/null)"
done
find "$CACHE" \( -name "t2s_model.safetensors" -o -name "s2a_model.pt" \
  -o -name "campplus_cn_common.bin" -o -name "bigvgan_generator.pt" \
  -o -name "model.safetensors" \) -type f | while read -r f; do test -s "$f"; done
for rel in tokenizer.json tokenizer.model tokenizer_config.json special_tokens_map.json wav2vec2bert_stats.pt; do
  test -s "$ROOT/checkpoints/$rel"
done
printf 'Repository-local HF cache and checkpoints OK\n'
```

Expected: 输出 `Repository-local HF cache and checkpoints OK`（若失败，停止并修复仓库内模型，禁止回落 home cache 或生成时下载）。

- [ ] **Step 2: 克隆已验证环境到 worker 隔离前缀（一次性，不入库）**

不从 PyPI 重装 requirements/torch；源 `confuciustts` 与 miniconda base 均保持只读，仅向克隆前缀补 HTTP 依赖。

```bash
set -euo pipefail
if ! test -f /e/GitHub/_confucius4_tts_venv/Scripts/python.exe; then
  "C:/Users/kenyo/miniconda3/Scripts/conda.exe" create \
    -p "E:/GitHub/_confucius4_tts_venv" \
    --clone "C:/Users/kenyo/miniconda3/envs/confuciustts" -y
fi
uv pip install --python "E:/GitHub/_confucius4_tts_venv/Scripts/python.exe" \
  "fastapi>=0.110" "uvicorn>=0.29" "python-multipart>=0.0.9"
"E:/GitHub/_confucius4_tts_venv/Scripts/python.exe" -I -c \
  'import torch, transformers, fastapi, uvicorn, multipart; assert torch.__version__ == "2.7.0+cu128", torch.__version__; assert torch.cuda.is_available(); print(torch.__version__, transformers.__version__, fastapi.__version__)'
```

Expected: 末行打印 `2.7.0+cu128`、transformers/fastapi 版本；不得出现 cu126、torch 重装或源环境修改。

- [ ] **Step 3: 启动 worker 并等待 ok**

新终端（保留运行）：

```bat
set "CONFUCIUS4_TTS_ROOT=E:\GitHub\Confucius4-TTS"
local-gateway\start_confucius_worker.bat
```

等待就绪（Git Bash，最长 15 分钟；期间 status=starting 是正常的）：

```bash
set -euo pipefail
for i in $(seq 1 90); do
  body="$(curl --noproxy '*' -s http://127.0.0.1:8765/health || true)"
  if echo "$body" | grep -q '"status":"ok"'; then echo "$body"; exit 0; fi
  sleep 10
done
echo "worker never became ok"; exit 1
```

Expected: `{"status":"ok","sample_rate":22050}`。

- [ ] **Step 4: 启动 gateway**

另开新终端（保留运行）：

```bat
local-gateway\start_gateway.bat
```

P2 不需要启动 ComfyUI；已有 9188 进程保持原样。

- [ ] **Step 5: 三项真实 clone（不传 ref_text）**

当前 `scratch/local-comfyui/p2/` 已核实存在旧 Qwen 9189 路线的同名产物。先留档隔离，禁止用旧文件满足后续 ffprobe；归档目标若已存在则停止，不覆盖。若是失败后的重跑，先把该次 Confucius attempt 单独归档，不得混入旧 Qwen 目录。

```bash
set -euo pipefail
cd /e/GitHub/claude-code-video-toolkit
mkdir -p scratch/local-comfyui/p2
archive='scratch/local-comfyui/p2/superseded-qwen-e09ce2c'
mkdir -p "$archive"
for name in clone.mp3 clone.wav voiceover.mp3 voiceover.txt health-pre.json health.json; do
  src="scratch/local-comfyui/p2/$name"
  if [[ -f "$src" ]]; then
    if [[ -e "$archive/$name" ]]; then
      printf 'Refusing to overwrite archived evidence: %s\n' "$archive/$name" >&2
      exit 1
    fi
    mv -- "$src" "$archive/$name"
  fi
done
for name in clone.mp3 clone.wav voiceover.mp3 voiceover.txt health-pre-flip.json health.json; do
  test ! -e "scratch/local-comfyui/p2/$name"
done

REF_AUDIO='C:/Users/kenyo/Downloads/Music/voice_02.mp3'

uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \
  --lang zh --text "这段声音由本地 Confucius4-TTS 克隆生成。" \
  --ref-audio "$REF_AUDIO" --format mp3 \
  --output scratch/local-comfyui/p2/clone.mp3

uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \
  --lang zh --text "这是由本地 Confucius4-TTS 生成的 WAV 文件。" \
  --ref-audio "$REF_AUDIO" --format wav \
  --output scratch/local-comfyui/p2/clone.wav

printf '%s\n' '测试 voiceover 的 Confucius4 本地克隆' > scratch/local-comfyui/p2/voiceover.txt
uv run tools/voiceover.py --provider confucius4 --cloud local \
  --tts-model confucius4-tts --language zh \
  --script scratch/local-comfyui/p2/voiceover.txt \
  --ref-audio "$REF_AUDIO" \
  --output scratch/local-comfyui/p2/voiceover.mp3
```

Expected: 三条命令 exit 0，各打印 `Generated:` / `Voiceover saved to:`。

- [ ] **Step 6: ffprobe 断言（codec/duration/sample_rate/channels）**

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
test "$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of csv=p=0 "$file")" = "22050"
test "$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of csv=p=0 "$file")" = "1"
duration="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file")"
uv run python -I -c 'import sys; assert float(sys.argv[1]) > 0' "$duration"
```

Expected: 全部通过（无输出即成功）。

- [ ] **Step 7: pre-flip 真机 /health 与完整回归（flip 门槛）**

此时 `IMPLEMENTED_ROUTES` 仍为空。先证明三项真实生成后的 worker、依赖和聚合健康，再跑完整回归；两步都通过后才允许 Step 8 flip。

```bash
set -euo pipefail
curl --noproxy '*' --fail-with-body \
  http://127.0.0.1:8123/health > scratch/local-comfyui/p2/health-pre-flip.json

uv run python -I -c '
import json
p = "scratch/local-comfyui/p2/health-pre-flip.json"
d = json.load(open(p, encoding="utf-8"))
assert d["ready"] and d["models_ready"]
assert d["dependencies"] == {"ffmpeg": True, "ffprobe": True}
assert d["tts_backend"]["status"] == "ok"
assert d["tts_backend"]["sample_rate"] == 22050
assert d["routes"]["tts"] == {"implemented": False, "available": False}
print("pre-flip health OK")
'

TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py \
  docs/superpowers/specs/2026-11-18-confucius4-tts-design.md \
  docs/superpowers/plans/2026-11-18-confucius4-tts.md \
  docs/local-comfyui-provider-roadmap.md local-gateway/README.md
```

Expected: `pre-flip health OK`，双测试域、ruff、copy lint 全绿。任何一步失败都保持 `IMPLEMENTED_ROUTES` 为空并停止验收。

- [ ] **Step 8: flip IMPLEMENTED_ROUTES 并验证 post-flip /health**

只有 Step 7 的三项生成、pre-flip health 和完整回归全绿后才执行本步。

`registry.py` 中：

```python
IMPLEMENTED_ROUTES: frozenset[str] = frozenset()
```

替换为：

```python
# Flipped after the three real clone generations, /health and the full
# regression of spec section 14 all passed (see the P2 acceptance notes).
IMPLEMENTED_ROUTES: frozenset[str] = frozenset({"tts"})
```

`test_health.py` 的 `test_route_targets_and_empty_implemented_set` 替换为：

```python
    def test_route_targets_and_implemented_tts(self):
        self.assertEqual(registry.ROUTE_TARGETS, {
            "tts": "confucius4-tts",
            "image_generate": "qwen-image-2.1",
            "image_edit": "qwen-image-2.1",
            "video": "minimax-h3-fl2va",
        })
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset({"tts"}))
```

把 `test_full_ready_with_comfyui_down_returns_200` 中 routes 的 TTS 期望改为 `{"implemented": True, "available": True}`，其余三项仍为 false。再在 `HealthAggregationTests` 加一个显式 available 测试：

```python
    def test_tts_route_available_when_implemented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, _ = self.payload_for(
                root, FakeClient(object_info=object_info_fixture()))
        self.assertEqual(payload["routes"]["tts"],
                         {"implemented": True, "available": True})
        for name in ("image_generate", "image_edit", "video"):
            self.assertEqual(payload["routes"][name],
                             {"implemented": False, "available": False})
```

`registry.py` 已修改而 uvicorn 未开 `--reload`：只停掉 Step 4 启动的 gateway（Ctrl+C 其终端），重新运行 `local-gateway\start_gateway.bat`，不要动 worker 或已有 9188。然后：

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
assert d["tts_backend"]["status"] == "ok"
assert d["tts_backend"]["sample_rate"] == 22050
assert d["routes"]["tts"] == {"implemented": True, "available": True}
for name in ("image_generate", "image_edit", "video"):
    assert d["routes"][name] == {"implemented": False, "available": False}
print("health OK")
'
```

Expected: `health OK`。

- [ ] **Step 9: post-flip 最终完整回归**

```bash
set -euo pipefail
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py \
  docs/superpowers/specs/2026-11-18-confucius4-tts-design.md \
  docs/superpowers/plans/2026-11-18-confucius4-tts.md \
  docs/local-comfyui-provider-roadmap.md local-gateway/README.md
```

Expected: 全部通过。若失败，将 `IMPLEMENTED_ROUTES` 恢复为空、保持 P2 未勾，修复后从 Step 7 重跑；不得提交或宣称验收完成。

- [ ] **Step 10: 勾选 roadmap P2 checkbox**

只有 Step 8 post-flip health 与 Step 9 最终回归均通过后，才把 `docs/local-comfyui-provider-roadmap.md` P2 节（Task 7 Step 3 写入的清单）7 个 `- [ ]` 全部改为 `- [x]`。

- [ ] **Step 11: 收尾提交并只停本次进程**

```bash
git status --short   # 确认只有 registry.py、test_health.py、roadmap 与 scratch 外产物
git add local-gateway/local_gateway/registry.py local-gateway/tests/test_health.py \
  docs/local-comfyui-provider-roadmap.md
git commit -m "FEAT: mark the Confucius4 TTS route implemented after real acceptance"
```

停止 Step 3/4 本次启动的 worker 与 gateway 进程（Ctrl+C 各终端）；不停止任何已有 9188 ComfyUI 进程。验收产物留在 `scratch/local-comfyui/p2/`（gitignore 已覆盖）。

---

## Notes for reviewers

- 每个任务的测试都是"完整文件替换"或"精确编辑块"，执行者不需要自行设计断言；若某步的预期失败信息不同，先核对锚点文本再继续。
- Task 4 是最大的一个任务（网关核心交换）；它内部按 registry→config→health→audio→tts→server→删除 的顺序推进，每步有独立测试循环，建议在执行时逐 commit-review 或至少逐文件 review。
- 真实验收（Task 8）明确允许单步超过 5 分钟（模型加载、三项生成）；其余步骤保持在 2-5 分钟粒度。
