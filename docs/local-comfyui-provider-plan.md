# 本地化改造计划：ComfyUI 后端 + local provider

> 状态：**已定稿，未实施**。定稿日期 2026-11-14。
> 范围决议：配音走本地 Qwen3-TTS；音乐不变（保持默认 acemusic 路线）；出图用 Qwen Image 2.1（本地权重）；视频用 MiniMax H3（本地权重）。改造风格：正规 provider（可上游 PR 质量）。
> 前置事实已核实：三套模型权重全部在 `E:\ugit\comfyui`，ComfyUI 可用（端口 9188）。

## Context

ccvt 的 AI 生成环节（配音/出图/视频）设计上只支持 Modal/RunPod 云。本机 RTX 5090（32GB，sm_120）+ `E:\ugit\comfyui` 已有全部所需模型权重：

| 环节 | 模型 | 本地权重位置（`E:\ugit\comfyui\models\`） |
|---|---|---|
| 配音 | Qwen3-TTS 1.7B（Base / CustomVoice / VoiceDesign + 12Hz tokenizer） | `TTS/Qwen3-TTS/` |
| 出图 | Qwen Image 2.1（int8 / turbo int8 DiT + qwen3.5-9B 专用文本编码器 + 2.1 VAE） | `diffusion_models/` + `text_encoders/` + `vae/` |
| 视频 | MiniMax H3（fl2va / ref2va int8+nvfp4 + qwen3vl-32b 专用文本编码器） | `diffusion_models/` + `text_encoders/` |
| 音乐 | 不变（默认 acemusic） | 不适用 |

目标：给 ccvt 加一个正规的 `local` provider，AI 生成全部走本地 ComfyUI，`video_job` 流水线可按场景选用。改动走 `feat/local-comfyui-provider` 分支；保持 MIT 兼容（**不从 AGPL 的 OpenMontage 复制代码**，只借鉴协议模式）。

## 架构

```
ccvt 工具 (qwen3_tts.py / qwen_image.py / h3_video.py)
   │  call_cloud_endpoint(provider="local")   ← 复用现有 Modal 同步 POST 流程
   ▼
local-gateway（新子项目，FastAPI，无 torch 依赖，端口 8123）
   │  契约翻译：modal 风格 JSON ⇄ ComfyUI API
   │  （POST /prompt → poll /history → GET /view；/upload/image 传参考图）
   ▼
ComfyUI（127.0.0.1:9188，权重与节点包已装，自带显存管理）
```

## 已核实的契约要点（实施依据）

- `tools/cloud_gpu.py:293-316` 是 if/elif 分发缝；`get_provider_config()` 在 :251 对未知 provider 抛错，两处是 `local` 的入口
- `_call_modal`（:484-571）为同步 POST（`payload.get("input", payload)` 解包、超时+30、错误一律返回 `{"error": str}` 不抛异常），`_call_local` 直接克隆此流程，去掉 token 认证与 Modal 报错提示，加「网关未启动」友好错误
- 服务端契约（modal-* 容器，字段级已摸清）：
  - **qwen3-tts**：请求 `text`(str|list)/`mode`(custom_voice|clone|voice_design)/`speaker`(默认 Ryan)/`instruct`/`language`/`output_format`(mp3|wav)/`ref_audio_base64`+`ref_text`；响应 `{"success", "outputs":[{"duration_seconds","audio_base64"}]}`
  - **flux2**（qwen-image 沿用同构字段）：请求 `operation`(generate|edit)/`prompt`/`width`/`height`/`seed`/`num_inference_steps`/`image_base64`(+`images_base64`≤2)；响应 `{"success","image_base64"(PNG),"seed","inference_time_ms","model"}`
  - **ltx2**（h3_video 沿用同构字段）：请求 `prompt`/`width`/`height`/`num_frames`/`fps`/`quality`/`seed`/`image_base64`(i2v)；响应 `{"success","seed","duration","video_base64"(mp4)}`
- 需要扩展 choices 的 argparse 位置：`qwen3_tts.py:864-869`、`voiceover.py:298-304`（新工具无需，仅 local）
- `video_job.py`：`VISUAL_KINDS`(:118)、`_visual_args`(:1741)、`_NEEDS`(:2650)、`host_capabilities()`(:2694，三处枚举)
- `verify_setup.py:229` 的 `https://` 前缀谓词会误报本地 URL，local 检查块要自写
- ComfyUI 启动：`Start ComfyUI.bat` → `main.py --port 9188`，输出目录重定向至 `E:\UGit\ComfyUI_Workflow\History`，已设代理但 `NO_PROXY=localhost,127.0.0.1`
- TTS 节点包：用 `custom_nodes/ComfyUI-QwenTTS`（AILab_QwenTTS 节点，自带 example_workflows 可作模板）；`qwen3-tts-comfyui` 只有 README 是空壳

## 改动清单

### A. ccvt 侧（`tools/`，约 8 个文件）

1. **`tools/cloud_gpu.py`**：`_LOCAL_ENV_VARS` 表；`get_provider_config()` 加 `"local"` 分支；`call_cloud_endpoint()` 分发链加 `_call_local`
2. **`tools/qwen3_tts.py`**：argparse choices 与 `generate_audio` 签名默认加 `"local"`
3. **`tools/voiceover.py`**：argparse `--cloud` 加 `"local"`
4. **新 `tools/qwen_image.py`**：CLI 仿 `flux2.py`（--prompt/--width/--height/--seed/--num-inference-steps/--out；generate/edit），契约同构，`tool_name="qwen_image"`
5. **新 `tools/h3_video.py`**：CLI 仿 `ltx2.py`（--prompt/--width/--height/--num-frames/--fps/--quality/--image/--out），`tool_name="h3_video"`，timeout 默认 1800s
6. **`tools/video_job.py`**：`VISUAL_KINDS` 加 `"qwen"`、`"h3"`；`_visual_args` 加映射；`_NEEDS` 加三组 local 条目；`host_capabilities()` 补枚举
7. **`tools/verify_setup.py`**：Local Gateway 检查块（`http://` 谓词 + `/health` 可达性）
8. **`.env.example` / `README` / `skills/video-toolkit/SKILL.md`**：登记三个 `LOCAL_*_ENDPOINT_URL`、网关启动说明、路由表新行

### B. 网关（新目录 `local-gateway/`，独立 uv 子项目）

- `pyproject.toml`：仅 `fastapi`、`uvicorn`、`requests`（无 torch）
- `server.py`：
  - `GET /health`：代理 ComfyUI `/system_stats` + 经 `/object_info` 检查所需模型存在（缺模型给 actionable 错误）
  - `POST /qwen3-tts/generate`：modal-qwen3-tts 契约 → ComfyUI-QwenTTS workflow（按 node id 打补丁）
  - `POST /qwen-image/generate` / `/edit`：UNETLoader(`qwen_image_2.1_turbo_int8_convrot`) + CLIPLoader(qwen3.5-9B pe t2i/i2i) + VAELoader(`qwen_image_2.1_vae_bf16`) + KSampler + SaveImage；edit 经 `/upload/image`
  - `POST /h3/video`：fl2va t2v/i2v（text encoder `qwen3vl_32b_minimax_h3_int8_convrot`），VHS_VideoCombine 出 mp4
  - ComfyUI 客户端（submit/poll/history/view/upload，约 200 行）**原创实现**，不复制 OpenMontage（AGPL）
- `workflows/*.json`：4 类模板（从 ComfyUI-QwenTTS example_workflows 与现有可用 GUI 设置导出适配；实施第一步先起 ComfyUI 用 `/object_info` 校验节点名）
- `start_gateway.bat` + `README.md`（启动顺序：先 ComfyUI(9188) 再网关(8123)）

### C. 用户 `.env` 配置（ccvt 根）

```ini
LOCAL_QWEN3_TTS_ENDPOINT_URL=http://127.0.0.1:8123/qwen3-tts/generate
LOCAL_QWEN_IMAGE_ENDPOINT_URL=http://127.0.0.1:8123/qwen-image/generate
LOCAL_H3_VIDEO_ENDPOINT_URL=http://127.0.0.1:8123/h3/video
```

## 实施顺序

1. `cloud_gpu.py` local provider + 网关骨架与 ComfyUI 客户端 + `/health`，打通链路
2. Qwen3-TTS 路由（契约最成熟，先端到端：`uv run tools/qwen3_tts.py --cloud local --text "..." --out test.mp3`）
3. Qwen Image 路由 + `qwen_image.py`
4. H3 视频路由 + `h3_video.py`（生成慢，最后单独验）
5. `video_job.py` / `verify_setup.py` / 文档收尾

## 验证

- **回归**：`uv run python -m unittest discover tests/python` 全过；runpod/modal 路径不受影响
- **网关**：health 返回 GPU/模型清单；qwen3-tts 出 mp3（ffprobe 验时长）；qwen-image 出 png（验尺寸）；h3 出短 mp4（121 帧）
- **CLI 集成**：三个工具各跑一次 `--cloud local`
- **任务级 E2E**：最小 spec（voice=qwen3/local + 一幕 qwen 图 + 一幕 text）→ `video_job.py submit --max-usd 1` → MP4，成本 $0.00
- **verify_setup**：Local GPU 区块全绿

## 风险与对策

- ComfyUI 节点名/workflow 细节以运行时 `/object_info` 为准，模板先校验再用
- H3 本地视频生成可能数分钟/段：timeout 1800s，job 文档标注
- 上游同步：改动集中且加法式，`git merge upstream` 冲突面小

## 备注：仓库准备（尚未做）

- fork 到 `kenyonxu/claude-code-video-toolkit`；本地 origin 指向 fork、upstream 指向 digitalsamba（等指示后执行）
- 工作分支：`feat/local-comfyui-provider`
