# Confucius4-TTS 本地语音克隆设计规格

> 状态：**分节设计已于 2026-11-18 获确认；2026-11-22 按上游新整合指南完成执行前同步，进入实施**。
> 本文取代 `docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md` 中的 Qwen ComfyUI 路线及其实施计划。旧 Task 9 不再恢复，旧提交不通过 reset 或混合 revert 删除。
> 前置事实：基础 Transformers 后端已获选择；`E:/GitHub/Confucius4-TTS` 的基础 `ConfuciusTTS` 可复用。按上游 `INTEGRATION.zh-CN.md` 与提交 `366fdc0`，模型已从用户 HF cache 迁至仓库内 `models/hf/`，所需文件已重新逐项确认存在且非空。
> 本文中的「必须」是验收要求，「不得」是边界。

## 1. 目标与范围

建立完全独立于 Qwen ComfyUI 节点的本地 Voice Clone 路线：ccvt 新 CLI/provider 经现有 local JSON transport 调用轻量网关，网关经 multipart 调用独立 Transformers worker，获得真实 PCM16 WAV，再生成 base64 的 WAV 或 MP3。

完成时必须同时满足：

1. `confucius4_tts.py` 与 `voiceover.py --provider confucius4` 的 local clone 全链路可用。
2. `POST /tts/generate` 只接受 clone，不要求也不接受 `ref_text`、Qwen 节点、Qwen sampling 或 Qwen model_size。
3. 独立 worker 只加载一次 `ConfuciusTTS`，使用基础 Transformers，不使用 vLLM、ComfyUI 或 AILab 节点。
4. 网关保持无 torch；worker 在独立隔离环境运行，使用 Confucius 仓库内的离线 `models/hf/`，不在生成时下载权重。
5. `confucius4_tts` 的 clone MP3、clone WAV 和 `voiceover` clone 三项真实生成全部通过，全部有正确的编码、采样率、声道和时长。
6. 只有真实验收、health 和完整回归通过后，才允许 `IMPLEMENTED_ROUTES` 加入 `tts` 并勾选 P2。

## 2. 非目标

- 不实现 CustomVoice、VoiceDesign、内置 speaker、跨参考混音、持久 voice ID 或文字描述音色。
- 不使用 `AILab_Qwen3TTS*`、`ComfyUI`、`ComfyUIClient`、ComfyUI workflow JSON 或 ComfyUI 节点健康检查作为 TTS 路线。
- 不使用 vLLM、vLLM 0.16 内部补丁、GPUModelRunner、vLLM 模型目录或 vLLM 启动写文件/symlink。
- 不引入 torch、transformers、torchaudio 到轻量 `local-gateway` 进程；这些依赖只允许在独立 worker 进程内。
- 不要求 `ref_text`、语音逐字稿、Qwen `Auto` 语言枚举、Qwen `temperature/top_p` 或 `speaker`。
- 不修改 Modal/RunPod 的 Qwen 请求、R2、下载、价格或云文档，不把已有 Qwen 云端支持删除。
- 不提前执行 P3/P4/P5，不修改 `video_job.py`、`verify_setup.py`、根 `.env.example` 或 toolkit registry。
- 不自动 `/interrupt`，不重试生成 POST，不声称客户端超时取消后台生成。
- 不把参考音频、模型权重或真实用户路径写入 tracked 配置或提交到 git。

## 3. 已确认的决策

1. **后端为基础 Transformers**，通过薄 HTTP wrapper 复用 `confuciustts.cli.inference.ConfuciusTTS`。
2. **使用 Confucius 仓库内的 HF 缓存**，不重新下载。运行时必须设置 `HF_HUB_CACHE=<Confucius根>/models/hf`；以下文件已在该目录重新核实存在且非空：
   - `netease-youdao/Confucius4-TTS` 的 `t2s_model.safetensors`、`s2a_model.pt`
   - `facebook/w2v-bert-2.0` 的 `model.safetensors` 与配置
   - `funasr/campplus` 的 `campplus_cn_common.bin`
   - `nvidia/bigvgan_v2_22khz_80band_256x` 的 `bigvgan_generator.pt` 与配置
3. **独立 worker 进程**：worker 在 `E:/GitHub\_confucius4_tts_venv` 运行；该前缀由已验证的 `confuciustts` conda 环境克隆而来，保留 `torch 2.7.0+cu128`，不从 PyPI 重装 torch，轻量网关继续无 torch。
4. **不保留 Qwen local 入口**：`qwen3_tts` 只保留 Modal/RunPod；本地只使用新的 Confucius CLI/provider，避免并存两套本地语音后端。
5. **不需要 `ref_text`**：只传参考音频和语言代码，参考音频就是用户指定的 `voice_02.mp3`。
6. **批量只解码一次，但逐项 multipart**：不为“只上传一次”改变 Confucius 服务端，也不伪造 reference session。
7. **旧 P2 spec/plan 标记 superseded**，继续在当前 P2 分支上以新提交替换，不重置历史。

## 4. 总体架构与文件边界

```text
confucius4_tts.py / voiceover.py
        │  复用 cloud_gpu local JSON + base64 transport
        ▼
local-gateway :8123（无 torch）
        │  multipart: text/lang/reference
        ▼
Confucius worker :8765（独立 Python 进程，加载 ConfuciusTTS 一次）
        │  PCM16 WAV bytes
        ▼
网关有界读取 → WAV直通或 WAV→MP3 → ffprobe → base64
```

新增或替换：

- `local-gateway/local_gateway/confucius_worker.py`：worker 的 `/health` 与 `/api/tts`，仅加载和调用，不含 ccvt 业务规则。
- `local-gateway/local_gateway/confucius_client.py`：独立 HTTP 客户端，负责 multipart、健康检查、有界二进制响应、错误映射。
- `local-gateway/local_gateway/tts.py`：完全替换为 Confucius 请求校验、批量编排、deadline 和 lock。
- `local-gateway/local_gateway/audio.py`：改为 WAV bytes → WAV/MP3，不再读取 ComfyUI history。
- `local-gateway/local_gateway/registry.py`：TTS 条目改为 `confucius4-tts`，图像/视频条目不变。
- `local-gateway/local_gateway/server.py`：分别装配 ComfyUI factory 与 Confucius factory。
- `tools/confucius4_tts.py`：新的本地 CLI。
- `tools/voiceover.py`：新增 `confucius4` provider 与 `--tts-model`，不覆盖 ElevenLabs `model`。
- 新增 `start_confucius_worker.bat` 和本地配置示例。

删除或彻底替换：

- `local-gateway/local_gateway/tts_workflow.py`
- `local-gateway/local_gateway/workflows/tts_voice_clone.json`
- `local-gateway/tests/test_tts_workflow.py`
- `tests/python/test_qwen3_tts_local.py`
- `tests/python/test_voiceover_local.py`
- Qwen local argparse/setup/base64/R2 分支和 local registry/health 断言

`tts_workflow` 的 AILab node id、`target_text/reference_text`、`SaveAudioAdvanced`、Qwen model_size、Qwen language enum、Qwen Advanced 节点及其测试不得迁移到新路线。

## 5. 请求契约

### 5.1 ccvt 到网关

`POST /tts/generate` 的 JSON 顶层只允许：

| 字段 | 规则 |
|---|---|
| `model` | 可选字符串，缺省 `confucius4-tts`，其他值返回 `unknown_model` |
| `text` | 必填，非空字符串或最多 8 项非空字符串数组，每项最多 1024 字符 |
| `mode` | 可选，缺省 `clone`；其他值返回 `mode_unsupported` |
| `language` | 默认 `zh`，限 14 个小写代码；标量广播，数组必须与 text 等长 |
| `output_format` | `mp3`（默认）或 `wav`，其他值拒绝 |
| `ref_audio_base64` | 必填，可带 data-URL，解码后最大 16 MiB |
| `ref_audio_url` | 不允许，返回 `external_url_unsupported` |
| `r2` | 不允许，返回 `external_url_unsupported` |

允许的 14 个语言代码：`zh/en/ja/ko/de/fr/es/id/it/ms/pt/ru/th/vi`。

不支持 `ref_text`、`speaker`、`instruct`、`temperature`、`top_p`、CustomVoice 或 VoiceDesign。出现这些字段时，按字段类型返回 `mode_unsupported`；其他未知字段返回 `validation_error`。`mode` 非 clone 也返回 `mode_unsupported`。

`ref_audio_base64` 必须使用严格 base64 解码，data-URL 的 MIME 与 `;base64` 参数必须分离。空音频、超限、非法 base64、非法文本、非法语言或非法模型均在发起后端请求前拒绝。

### 5.2 网关到 worker

worker 的 `POST /api/tts` 只接受 multipart：

- `text`：一个 item 的文本
- `lang`：对应的小写语言代码
- `reference`：当前 item 的参考音频 bytes 和原文件名/内容类型

每个 item 都发送一次 multipart reference，但保持原始输入顺序。worker 不接收 base64 JSON、ref_text、speaker、temperature/top_p 或输出格式选择；它只返回 PCM16 WAV bytes。

## 6. Worker 设计

`confucius_worker.py` 只包含以下职责：

- 启动时从 `config/inference_config.yaml` 的临时绝对路径副本创建一次 `ConfuciusTTS`，`device="cuda"`（副本见本节下文）。
- `GET /health` 返回 `status` 和 `sample_rate`。
- `POST /api/tts` 以 lock 串行执行基础 `generate()`，返回 PCM16、mono、22050 Hz WAV bytes。
- 上传的参考音频写入平台临时目录，请求完成后删除，不写 Confucius 根目录。
- 输出明确编码为 PCM16，避免依赖 `torchaudio.save()` 默认 float WAV 的行为。

健康状态：

- `starting`：模型正在加载，HTTP 503。
- `ok`：模型加载完成，HTTP 200，`sample_rate=22050`。
- `error`：加载或初始化失败，HTTP 503，只返回脱敏错误，不回传 traceback。

worker 使用独立的 `E:/GitHub\_confucius4_tts_venv`。该隔离前缀通过 `conda create --clone confuciustts` 创建，只向克隆环境补充 FastAPI/uvicorn/python-multipart；不修改 miniconda base 或源 `confuciustts` 环境，不安装其他 CUDA 构建。启动时在任何 Hugging Face/Transformers 导入之前设置：

```text
HF_HUB_CACHE=<CONFUCIUS4_TTS_ROOT>/models/hf
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

`models/hf/` 与仓库 `checkpoints/` 必须同时齐全；不回落到用户 home cache，不允许生成时下载。

上游 `config/inference_config.yaml` 用 `./checkpoints` 相对路径定位 tokenizer 与 w2v-bert 统计文件。worker 读取该配置，只把 `paths.tokenizer_path` 与 `paths.w2v_stat` 两行改写为 `<CONFUCIUS4_TTS_ROOT>/checkpoints/...` 的绝对路径（POSIX 分隔符），写入进程临时目录中的副本并交给 `ConfuciusTTS`；副本保留到 uvicorn 退出后清理。两键各须恰好出现一次且取值须为 `./` 相对路径，否则启动即失败，不猜测 tokenizer 位置。（2026-11-22 修订：原设计在 ccvt 根目录创建指向 `E:/GitHub/Confucius4-TTS/checkpoints` 的目录联接，真实验收时该联接在执行环境中被拒绝，且会形成指向模型目录的可写别名，故废止。）上游配置与 Confucius 根目录全程只读：不创建目录联接，不切换解释器工作目录，启动器以 `-B` 运行以免导入向 checkout 写入 `__pycache__`。`CONFUCIUS4_TTS_ROOT` 仅用于解析代码与配置路径，不作为公开提交值。

## 7. ConfuciusClient 与错误映射

`ConfuciusClient` 不复用或伪装 `ComfyUIClient`，提供：

- `health() -> dict`：读取 `/health`，要求 `status=ok`。
- `generate(text, lang, reference_bytes, filename, content_type, deadline) -> bytes`：单次 multipart `/api/tts`，返回有界 WAV bytes。

约束：

- `requests.Session.trust_env=False`，每次请求独立 Session 并关闭。
- 短连接 timeout，read timeout 使用当前阶段剩余预算；POST 不重试。
- 响应采用有界读取，单文件最多 64 MiB，所有 item 合计最多 128 MiB。
- 非 200、非 WAV、超限、连接失败、timeout 均转换为结构化错误，不把后端 traceback 或绝对路径透传给 ccvt。

错误映射：

| 情况 | HTTP | code |
|---|---:|---|
| 非 clone 模式或不支持字段 | 422 | `mode_unsupported` |
| 请求字段、语言、base64、批量或格式错误 | 422 | `validation_error` |
| 未知模型 | 422 | `unknown_model` |
| ref_audio_url、r2 或外部存储 | 422 | `external_url_unsupported` |
| 权重、ffmpeg/ffprobe 或启动依赖失败 | 503 | `dependency_missing` |
| worker 不可达或未 ready | 503 | `tts_backend_unavailable` |
| HTTP 请求或总 deadline 到期 | 504 | `tts_backend_timeout` |
| worker HTTP/协议错误 | 502 | `tts_backend_error` |
| 生成、响应解析或音频处理失败 | 500 | `generation_failed` |

ComfyUI 专属 `comfyui_*` 错误码继续留给 P3/P4，不用于 Confucius TTS。

## 8. 批量、deadline 与并发

每个 item 独立执行 multipart generate、下载和最终编码，保持顺序。任一 item 失败时整个请求失败，不返回已生成 item 的部分 base64，临时目录按请求清理。

总 deadline 从等待 lock 前开始，覆盖 preflight、multipart、生成、下载、ffprobe 和 FFmpeg：

```text
deadline_seconds = 270 * max(1, item_count // 3 + 1)
```

该公式与现有 `qwen3_tts` 客户端公式一致。每个阶段开始前检查剩余时间；poll sleep 按剩余时间截断；FFmpeg subprocess timeout 使用剩余时间。lock 等待自身也必须有界，不能使用无限等待的 `with lock`。

`/health` 不获取 TTS lock，保持响应。worker 自身另有生成 lock，gateway 不把两个 lock 混为一个跨进程全局锁。

## 9. 音频管线

`audio.py` 不再读取 ComfyUI history，也不再使用 FLAC 中间文件：

- WAV：直接验证并返回，要求 PCM16、mono、22050 Hz。
- MP3：用 FFmpeg `libmp3lame` 将 WAV 转为真实 MP3，不改扩展名伪装。
- 最终文件执行 ffprobe，要求 `duration_seconds > 0`。
- 最终写出采用临时文件 + 原子替换，失败时不得留下可误认为成功的半成品。
- 单文件 64 MiB、单请求 128 MiB；临时目录在成功和失败时均清理。

成功响应：

```text
success=true
mode=clone
model=confucius4-tts
processing_time_seconds=<路由总耗时>
outputs=[
  { audio_base64=<最终字节>, duration_seconds=<ffprobe 时长> }
]
```

单文本额外把 `audio_base64` 和 `duration_seconds` 镜像到顶层。响应不包含 audio_url、r2_key、ref_audio_url 或外部路径。

## 10. CLI 与 voiceover

### 10.1 `confucius4_tts.py`

提供：

```text
--cloud local（显式本地后端）
--model confucius4-tts
--lang zh
--ref-audio PATH
--output PATH
--format mp3|wav
--timeout 300
```

`tool_name="confucius4_tts"`，并加入 `_LOCAL_ENV_VARS` 映射到 `LOCAL_TTS_ENDPOINT_URL`。`--model` 接受字符串，但当前只接受 `confucius4-tts`；其他模型返回 `unknown_model`。不要求 `--ref-text`，不支持 DesignInstruct、内置 speaker 或 R2。

### 10.2 `voiceover.py`

- 新增 `provider=confucius4` 和 `--cloud local`。
- 新增 `--tts-model`，取代未验收的 `--qwen-model`，保留 ElevenLabs `--model`。
- 所有 local single/batch/scene/brand clone 路径都必须有 `--ref-audio`，但不要求 `--ref-text`。
- 内置 speaker、brand VoiceDesign 和其他非 clone 路径在 local 下明确拒绝。
- `qwen3` 的云端分支保持原行为；`qwen3 + local` 不得再作为可用路径。

`qwen3_tts.py` 的 `--cloud local` 选择及其 local setup 分支移除，避免把 Confucius 请求误发给旧 Qwen 端点。`cloud_gpu` 只为 `confucius4_tts` 保留 TTS URL 映射。

## 11. Health 与注册表

TTS 注册条目改为 `confucius4-tts`，包含：

- `modes=(clone,)`
- `output_formats=(mp3, wav)`
- 14 个语言代码
- `max_batch_items=8`
- `deadline_chunk_seconds=270`
- worker `/health` 的必要状态与 sample_rate

不再包含 AILab 节点、Qwen 权重目录、model_size 或 Qwen sampling 能力。图像/视频 registry 不变。

`/health` 新增：

```text
tts_backend: { base_url, status, sample_rate }
```

`models_ready` 继续综合 TTS、图像、视频；TTS 不再读取 ComfyUI 节点。`routes.tts.available` 要求 implemented、`tts_backend.status=ok`、ffmpeg/ffprobe 可用。全局 `ready` 也要求这些依赖，图像/视频缺项只在各自 preflight 处理。

只有本规格第 14 节全部通过后，`IMPLEMENTED_ROUTES` 才加入 `tts`，其他三项仍为 false。

## 12. 测试设计

### 12.1 Worker 与 ConfuciusClient

覆盖：

- 启动状态、ok/error 健康检查、模型加载失败脱敏
- multipart 文本、语言、参考音频、输出 PCM16/mono/22050 Hz
- 语言/大小限制、离线环境变量、lock
- 临时 inference 配置：只改写两行 checkpoint 路径、上游配置文件保持不变、上游布局变化即失败且不落盘、副本保留到 uvicorn 退出后清理
- 有界二进制响应、timeout/HTTP/非 WAV 错误、无代理、无重试、无凭据泄漏

### 12.2 网关编排与音频

覆盖：

- 无 ref_text 的请求校验、逐项 multipart、批量顺序和最大 8 项
- deadline 公式、lock 等待、唯一输出前缀、失败无部分结果
- WAV 直通、WAV→MP3、22050 Hz/mono、时长和原子落盘
- history 不再被使用，旧 AILab JSON/节点断言全部移除

### 12.3 CLI 与 health

覆盖：

- model/timeout 在 confucius4_tts 和 voiceover 的所有 clone 分支透传
- 缺 ref-audio、非 clone、DesignInstruct、内置 speaker 的提前拒绝
- local base64、无 r2、外部上传下载函数零调用
- local 响应出现 URL/r2_key 时明确失败，云路径行为不变
- `tts_backend`、依赖失败、最终 route 状态和独立 fallback 列表

除第 14 节真实生成和 `/health` 外，所有网络、模型加载、模型目录和 worker 均使用 fake。所有 uv-run 测试在当前沙箱使用进程级 `TEMP=E:\GitHub`、`TMP=E:\GitHub`。

## 13. 随路线替换的文档与清理

必须完成：

1. 旧 P2 spec/plan 标记 superseded，明确旧 Task 9 不再执行。
2. roadmap 的 P2 改为 Confucius4-TTS clone，P0 lint/preflight/BAT 清理保持，Appendix A 不再把 CustomVoice/VoiceDesign 作为本地能力。
3. 原始 plan 的架构改为独立 Confucius worker，不再写成全部 TTS 经 ComfyUI。
4. gateway README 的启动顺序、配置、示例改为 Confucius，不宣称旧 Qwen 路线可用。
5. P0/P1 spec 增加 superseding 说明，不重写 P0/P1 已完成事实。
6. 移除 Qwen local CLI/setup/base64/R2 分支和旧 local 测试，保留 Qwen 云端支持。

继续 defer：vLLM、CustomVoice、VoiceDesign、跨参考混音、reference session、P3/P5、广泛重构和权重许可证结论。模型权重与参考音频的许可证在发布前单独核查，不因代码 Apache-2.0 而推定权重许可相同。

## 14. 真实验收

### 14.1 素材与进程

参考音频只读：`C:/Users/kenyo/Downloads/Music/voice_02.mp3`，不再传 `ref_text`。启动顺序：先启动 8765 Confucius worker，再启动 8123 gateway。P2 不需要启动 ComfyUI。验收前必须核实 `<Confucius根>/models/hf/` 的四组模型与 `<Confucius根>/checkpoints/` 的 tokenizer/统计文件，worker 显式使用 `HF_HUB_CACHE=<Confucius根>/models/hf`，不得依赖旧 home cache。验收后只停止本次创建的 worker/gateway，不停止已有 9188 进程。

### 14.2 三项真实 clone

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p2
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

验证：

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

### 14.3 Health 与回归

```bash
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
'

set -euo pipefail
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py docs/superpowers/specs/2026-11-18-confucius4-tts-design.md local-gateway/README.md
```

全部通过后才能勾选 P2 checkbox，且必须在真实三项 clone、health 和完整回归之后。

## 15. 提交边界

规格与实施计划在 `feat/local-comfyui-provider-p2` 上分别审阅和提交。实现计划至少区分：

1. 文档废止与接口准备
2. Confucius worker 与 ConfuciusClient
3. Confucius 编排、音频与注册表
4. confucius4_tts CLI 与 voiceover provider
5. health、文档和三项真实验收

实施计划本身纳入首个实现阶段文档提交，保证最终工作区干净。推送与 PR 不在本规格范围内。
