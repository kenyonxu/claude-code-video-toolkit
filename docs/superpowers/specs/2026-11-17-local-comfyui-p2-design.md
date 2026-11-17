# P2 设计规格：local ComfyUI TTS 路由

> 状态：**分节设计已于 2026-11-17 获确认，待书面规格审阅，未实施**。
> 前置文档：[P0/P1 设计规格](../../local-comfyui-provider-p0-p1-design.md) 与 [实施路线图](../../local-comfyui-provider-roadmap.md)。本文细化 P2，P3-P5 仍按路线图执行。
> 本规格在 `feat/local-comfyui-provider-p2` 分支提交。本文中的「必须」是验收要求，「不得」是边界。

## 1. 目标与范围

P2 建立第一条真实生成链路：ccvt CLI 经 local provider 调用网关 `POST /tts/generate`，网关把统一 TTS 请求翻译为原创 ComfyUI workflow，完成上传、提交、轮询、下载、音频处理和响应。

完成时必须同时满足：

1. CustomVoice、VoiceClone、VoiceDesign 三种模式均可用，全部使用 Advanced 节点。
2. 支持单文本和有序批量，支持 mp3/wav、可选模型名和现有 temperature/top_p。
3. local clone 不经过 R2、litterbox、0x0.st 或任何外部存储，所有结果只通过 base64 返回。
4. `qwen3_tts.py` 与 `voiceover.py` 的 local 路径完整贯通 model、timeout 和错误契约。
5. CustomVoice、voiceover、VoiceDesign、真实 clone 四项生成全部通过，真实 clone 是收口门槛。
6. 双测试域、ruff、copy lint 和真机 `/health` 全部通过，TTS 路由才能标记 implemented/available=true。

## 2. 非目标

- 不实现图像或视频生成，不提前执行 P3/P4/P5。
- 不增加 0.6B 变体、插件发现、跨请求 VoiceDesign prompt 缓存或 AILab 原生批处理优化。
- 不暴露 `x_vector_only`、`max_new_tokens`、`top_k`、`repetition_penalty`、`attention`、`device`、`precision` 等新公共参数。
- 不改变 Modal/RunPod 的请求、R2 或结果下载行为。
- 不修改 `video_job.py`、`verify_setup.py`、根 `.env.example` 或 toolkit registry。
- 不自动 `/interrupt`，不重试 `/prompt`，不承诺客户端断开后取消 ComfyUI 任务。
- 不复制第三方 example workflow JSON、PrimitiveNode 图或 OpenMontage 代码。

## 3. 已确认的决策

1. **真实 clone 纳入 P2 收口**，不再只是次级验证。
2. **三种模式统一使用 Advanced 节点**，保留现有 temperature/top_p 能力，不采用 Basic 或混合双路径。
3. **MP3 由 ComfyUI 直接输出，WAV 由 FLAC 中间文件转码**，不为了统一而重编码 MP3。
4. **真实 clone 使用 P2 CustomVoice 生成的音频作为参考**，其逐字稿就是生成时的输入文本，不依赖不存在的本机语音 fixture。
5. **非阻塞清理随 P2 并入，但只限低风险、直接相关项**，见第 13 节。

## 4. 模块与文件边界

```text
local-gateway/local_gateway/
├── tts.py                 # 请求校验、批量编排、deadline、并发锁
├── tts_workflow.py        # 原创 prompt 模板与补丁、history 音频定位
├── audio.py               # 有界下载、MP3/WAV、ffprobe、base64
├── server.py              # 注册 POST /tts/generate，只装配依赖
└── workflows/
    ├── tts_custom_voice.json
    ├── tts_voice_clone.json
    └── tts_voice_design.json
```

同步修改：

- `local_gateway/registry.py`：Advanced 节点、非空权重目录、TTS 能力、默认参数。
- `local_gateway/comfyui.py`：有界 view、HTTP timeout 错误分类、poll sleep 截断。
- `local_gateway/health.py`：独立 fallback 列表、依赖检查、TTS available 语义。
- `local_gateway/server.py`：app state 的 TTS lock 与路由装配。
- `tools/qwen3_tts.py`、`tools/voiceover.py`：local CLI 与 model/timeout 贯通。
- `tests/python/test_qwen3_tts_local.py`、`tests/python/test_voiceover_local.py`。
- `local-gateway/tests/test_tts.py`、`test_tts_workflow.py`、`test_audio.py`，以及现有 health/comfyui 测试。
- `docs/local-comfyui-provider-roadmap.md`、`docs/local-comfyui-provider-p0-p1-design.md`：第 13 节文档清理。

`tts_workflow.py` 不访问网络或文件系统；`audio.py` 不解析 TTS 业务参数；`tts.py` 不写 FastAPI response；`server.py` 只做依赖装配和 JSON 响应。

## 5. 请求契约

`POST /tts/generate` 的 JSON 顶层只允许以下字段：

| 字段 | 规则 |
|---|---|
| `model` | 可选字符串，缺省 `qwen3-tts-1.7B`，未知值返回 `unknown_model` |
| `text` | 必填，非空字符串或最多 8 项非空字符串数组 |
| `mode` | `custom_voice`（默认）、`clone`、`voice_design` |
| `language` | 默认 `Auto`；标量广播，数组必须与 text 等长；值必须在运行时枚举中 |
| `output_format` | `mp3`（默认）或 `wav`，其他值拒绝 |
| `speaker` | CustomVoice 使用，默认 `Ryan`，必须在运行时枚举中 |
| `instruct` | CustomVoice 可选；VoiceDesign 必填且非空；Clone 不使用 |
| `ref_audio_base64` | Clone 必填，可带 data-URL 前缀，解码后最大 16 MiB |
| `ref_text` | Clone 必填的非空逐字稿 |
| `temperature` | 可选有限数值，范围 0.1-2.0 |
| `top_p` | 可选有限数值，范围 0.0-1.0 |
| `ref_audio_url` | 不允许，返回 `external_url_unsupported` |
| `r2` | 不允许，返回 `external_url_unsupported` |

其他未知字段返回 `validation_error`，防止静默拼写错误。显式 null 的 temperature/top_p 视为未提供。VoiceDesign 只接受单个文本，批量直接拒绝。

请求不得下载 `ref_audio_url`，不得把 r2 凭据转发给 ComfyUI，不得从 ComfyUI 外部 URL 读取结果。

## 6. Advanced workflow 设计

### 6.1 共同规则

三个模板都是原创 API prompt dict，只含 `class_type` 和 `inputs`。每个模板一个 Advanced TTS 节点和一个 `SaveAudioAdvanced`。节点 id 在模板内稳定，补丁函数只修改白名单输入。

共同固定值：

```text
model_size=1.7B
max_new_tokens=2048
top_k=50
repetition_penalty=1.0
attention=auto
device=auto
precision=bf16
unload_models=true
seed=-1
```

未提供采样参数时 `do_sample=false`。temperature 或 top_p 任一显式提供时 `do_sample=true`，并只补丁提供的值，不补写云端默认数值。

`SaveAudioAdvanced.format` 是扁平字符串，不接受嵌套 dict。MP3 使用 `format="mp3"` 和独立的 `format.quality="V0"`；FLAC 使用 `format="flac"`，不写 `format.quality`。

### 6.2 CustomVoice

`AILab_Qwen3TTSCustomVoice_Advanced` 的 `text/speaker/language/instruct` 来自请求，输出 AUDIO 接 SaveAudioAdvanced。

### 6.3 VoiceClone

`AILab_Qwen3TTSVoiceClone_Advanced` 使用：

- `target_text`：来自公共请求的 `text`。
- `reference_audio`：来自 `LoadAudio` 的输出。
- `reference_text`：来自 `ref_text`。
- `x_vector_only=false`：保持现有必须提供逐字稿的契约。

参考音频 base64 解码后上传至 ComfyUI input，每次请求只上传一次。`LoadAudio.audio` 使用上传响应的 `name` 和非空 `subfolder` 拼接，不使用调用方原文件名。上传响应无法确认文件名时返回 `generation_failed`。

### 6.4 VoiceDesign

`AILab_Qwen3TTSVoiceDesign_Advanced` 的 `text/instruct/language` 来自请求，只允许 1.7B。输出 AUDIO 接 SaveAudioAdvanced。

### 6.5 批量与原子性

每个文本独立 submit/poll/view，保持输入顺序。clone 参考文件在同一请求内复用，但不宣称共享 voice prompt 优化。任一 item 失败时整个请求失败，不返回已生成 item 的部分 base64，临时文件按请求清理。

单个网关进程使用 app state 中的 `threading.Lock` 串行化完整 TTS 请求，避免线程、批量和显存竞争。`/health` 不获取此锁，仍能响应。

## 7. Deadline 与前置检查

总 deadline 从进入路由、等待 lock 开始，覆盖上传、submit、poll、view、ffprobe 和 FFmpeg：

```text
deadline_seconds = 270 * max(1, item_count // 3 + 1)
```

该公式与客户端 `timeout=300` 的 `timeout * max(1, len(texts)//3 + 1)` 一致，给客户端 read timeout 的额外 30 秒留余量。`voiceover.py` 新增 `--timeout` 后默认也为 300，避免旧的 batch 120 秒与网关不一致。

每个阶段开始前检查剩余时间；poll sleep 按剩余时间截断；FFmpeg subprocess timeout 使用剩余时间。HTTP 单次请求还受自身短 timeout 限制，超时错误必须区分于整体 deadline。

路由前置只检查 TTS 所需项，不调用全量 `/health`，不让图像或视频缺项阻塞 TTS。前置检查包括：

- 四个非空目录：`Qwen3-TTS-12Hz-1.7B-Base`、`Qwen3-TTS-12Hz-1.7B-CustomVoice`、`Qwen3-TTS-12Hz-1.7B-VoiceDesign`、`Qwen3-TTS-Tokenizer-12Hz`。
- 三种 Advanced 节点、`LoadAudio`、`SaveAudioAdvanced` 及所需输入。
- `speaker`、`language`、`model_size`、采样字段和保存格式选项。
- 当前进程 PATH 中的 ffmpeg 和 ffprobe。

非空目录检查必须至少有一个目录项，避免 AILab 回退到 `snapshot_download`。前置失败在 submit 前返回 `dependency_missing`，不发起模型生成。

## 8. 音频管线

`wait()` 成功后直接读取返回的 history entry：

```text
entry["outputs"][save_node_id]["audio"][0]
```

只接受含 `filename/subfolder/type` 的元数据，调用 `client.view()` 下载。不得自行拼 ComfyUI 输出路径，也不依赖 history 提供时长。

`view()` 改为有界读取：单文件最大 64 MiB，单请求所有输出最大 128 MiB。超限返回 `generation_failed`，不得继续分配内存。

- MP3：直接使用 ComfyUI 的 MP3 字节。
- WAV：将 FLAC 字节写入请求临时目录，用 FFmpeg 参数数组和 `shell=False` 转为 `pcm_s16le` WAV。
- 对最终字节执行 ffprobe，要求可解析且 `duration_seconds > 0`。
- base64 不带 data-URL 前缀。

成功响应：

```text
success=true
mode=<实际 mode>
model=<实际模型 key>
processing_time_seconds=<路由总耗时>
outputs=[
  { audio_base64=<最终字节>, duration_seconds=<最终 ffprobe 时长> }
]
```

单文本额外把 `audio_base64` 和 `duration_seconds` 镜像到顶层，保持现有客户端兼容。响应不包含 audio_url 或 r2_key。

## 9. 错误契约

所有错误响应都为非空字符串 `error`、`code` 和可选 `details`，不返回部分结果：

| 情况 | HTTP | code |
|---|---:|---|
| 字段、mode、format、language、批量或未知字段校验 | 422 | `validation_error` |
| 未知模型 | 422 | `unknown_model` |
| ref_audio_url、r2 或外部存储 | 422 | `external_url_unsupported` |
| TTS 权重、节点、ffmpeg/ffprobe 前置失败 | 503 | `dependency_missing` |
| ComfyUI 不可达 | 503 | `comfyui_unreachable` |
| 单次 HTTP 请求 timeout | 504 | `comfyui_request_timeout` |
| 总 deadline 到期 | 504 | `comfyui_timeout` |
| ComfyUI URL、HTTP 或 JSON 协议错误 | 502 | `comfyui_error` |
| ComfyUI 执行、history 或音频处理失败 | 500 | `generation_failed` |

`ComfyUIClient` 的 requests Timeout 映射为 `comfyui_request_timeout`；连接失败才是 `comfyui_unreachable`，InvalidURL/MissingSchema 和其他请求错误归为 `comfyui_error`。既有 `ComfyUITimeout` 继续表示 `wait()` 的总 deadline。

FastAPI 不再把 ReadTimeout 提示为「先启动 ComfyUI」。错误消息必须包含可操作信息，但不得泄露 r2 凭据、完整模型根路径或 traceback。

## 10. CLI 设计

### 10.1 `qwen3_tts.py`

- `--cloud` 加 local，默认仍为 modal。
- 新增自由字符串 `--model`，`generate_audio(..., model=None)` 贯通，只有非 None 才加入 payload。
- local clone 直接读取参考文件并 base64 编码，不调用 `upload_to_storage()`，不发送 `ref_audio_url`。
- 所有 local mode 都删除 r2 payload 和结果上传分支，结果只接受 `audio_base64`；出现 audio_url/r2_key 明确失败。
- `--setup --cloud local` 不再进入 RunPod setup，返回本地配置提示和退出码 2。
- 文档中的 provider 默认说明改为实际的 modal。

### 10.2 `voiceover.py`

- `--cloud` 加 local，默认仍为 modal。
- 新增 `--qwen-model`，贯通串行、批处理、scene directory 和品牌 VoiceDesign。ElevenLabs 的 `--model` 保持不变。
- 新增 `--timeout`，默认 300，贯通所有 Qwen single/batch 调用，公式与 `qwen3_tts.py` 一致。
- local 错误最终保持现有 `success=false/error` 形态，云 provider 行为不变。

P5 才处理 `video_job.voice.model`、零成本收据和 registry。

## 11. Health 与注册表

TTS 注册条目补充：

- `modes=(custom_voice, clone, voice_design)`
- `output_formats=(mp3, wav)`
- `model_size=1.7B`
- Advanced 固定参数和批量/deadline 上限
- 四个非空权重目录及 Advanced 节点/选项

health 增加：

```text
dependencies: { ffmpeg: boolean, ffprobe: boolean }
```

缺工具时新增 `dependency_missing`，全局 `ready` 同时要求两个工具可用。`models_ready` 仍只描述模型；TTS route 的 available 还要求 implemented、模型 ready 和依赖 ready。

P1 的四条生成路由保持 false 直到各自验收。只有本规格第 14 节全部通过后，`IMPLEMENTED_ROUTES` 才加入 `tts`，其他三项仍为 false。

异常 fallback 不再使用浅拷贝共享列表，改用 skeleton 工厂为每个模型生成独立列表。object_info 每节点每次 health 请求只拉取一次，并新增调用次数断言。

## 12. 测试设计

### 12.1 Workflow 与编排

覆盖：

- 三模式 class_type、节点连接、输入补丁和禁止字段
- `do_sample` 开关、有限数值与范围、DynamicCombo 扁平字段
- 单文本、批量、language 广播/长度错误、VoiceDesign 批量拒绝
- clone data-URL、大小限制、上传去重后的 name/subfolder
- 模型默认值与未知模型、未知 mode/format/字段
- deadline 公式、lock 等待、item fail-fast、无部分结果
- TTS-only preflight 和非空权重目录

### 12.2 音频与客户端

覆盖：

- history 音频定位、缺失/多项元数据、view 大小上限
- MP3/WAV 真实编码、ffprobe 时长、FFmpeg timeout 与错误截断
- requests Timeout、ConnectionError、InvalidURL 的错误分类
- poll sleep 不超过剩余 deadline
- local clone base64、无 r2、外部上传下载函数零调用
- model/timeout 在 qwen3_tts 和 voiceover 的所有分支透传
- `--setup --cloud local` 不进入 RunPod setup
- local 响应出现 URL/r2_key 时明确失败，云路径行为不变

### 12.3 Health 与清理

覆盖：

- ffmpeg/ffprobe 缺失、TTS 依赖失败、最终 route 状态
- fallback 模型列表互不影响
- object_info 唯一拉取次数
- 根 transport non-JSON 与 non-dict 的消息级断言
- 文档示例与配置行为保持一致

除第 14 节真实生成和 `/health` 外，所有网络、模型目录和 ComfyUI 都使用 fake。所有 uv-run 测试在当前沙箱使用进程级 `TEMP=E:\GitHub`、`TMP=E:\GitHub`。

## 13. 随 P2 并入的非阻塞清理

必须完成：

1. P0 验收文档统一为四文档 copy lint 和 `git status --short --branch --untracked-files=all`。
2. BAT 文档示例从 `"%~dp0"` 修正为已验证的 `"%~dp0."`。
3. 根 transport 的 non-JSON/non-dict 测试改为消息级断言。
4. ComfyUI HTTP timeout 与其他请求错误分类，health 不再统一提示先启动服务。
5. health fallback 改为独立列表工厂，并补 object_info 去重断言。
6. SaveAudioAdvanced DynamicCombo schema 覆盖，避免按普通 combo 误读。
7. qwen3_tts 文档默认 provider 修正为 modal，setup local 加防护。

继续 defer：`input=None` 的云端边界、quote 的 `/`、BAT LF、CI 矩阵化、报告文字、P5 成本/registry、依赖升级和广泛重构。

## 14. 真机验收

### 14.1 四项生成

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p2
REF_TEXT='今天的本地语音测试使用一段足够长的中文文本，用来验证参考音频上传、声音克隆、音频格式转换以及最终文件是否能够正常播放。'

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "$REF_TEXT" --output scratch/local-comfyui/p2/custom.mp3

printf '%s\n' '测试 voiceover 本地链路' > scratch/local-comfyui/p2/voiceover.txt
uv run tools/voiceover.py --provider qwen3 --cloud local --qwen-model qwen3-tts-1.7B \
  --script scratch/local-comfyui/p2/voiceover.txt --output scratch/local-comfyui/p2/voiceover.mp3

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "This is a local voice design test." \
  --design-instruct "A calm, warm narrator" --format wav \
  --output scratch/local-comfyui/p2/designed.wav

uv run tools/qwen3_tts.py --cloud local --model qwen3-tts-1.7B \
  --text "这段声音是在本地网关生成的第二个音频文件，用来确认克隆流程已经完整跑通。" \
  --ref-audio scratch/local-comfyui/p2/custom.mp3 --ref-text "$REF_TEXT" \
  --output scratch/local-comfyui/p2/clone.mp3
```

验证：

```bash
set -euo pipefail
for name in custom voiceover clone; do
  file="scratch/local-comfyui/p2/$name.mp3"
  test -s "$file"
  codec="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$file")"
  test "$codec" = "mp3"
  duration="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file")"
  uv run python -I -c 'import sys; assert float(sys.argv[1]) > 0' "$duration"
done

file=scratch/local-comfyui/p2/designed.wav
test -s "$file"
test "$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$file")" = "pcm_s16le"
duration="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file")"
uv run python -I -c 'import sys; assert float(sys.argv[1]) > 0' "$duration"
```

clone 的参考音频就是 custom.mp3，`REF_TEXT` 是它的准确逐字稿。不得用缺失的 `assets/voices/lugh-ref.wav` 代替。

### 14.2 Health 与回归

```bash
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
'

set -euo pipefail
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
TEMP='E:\GitHub' TMP='E:\GitHub' uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py docs/superpowers/specs/2026-11-17-local-comfyui-p2-design.md local-gateway/README.md
```

全部通过后才能勾选 P2 checkbox。P2 checkbox 的提交必须在真实四项生成、health 和完整回归之后。

## 15. 提交边界

规格与实施计划在 `feat/local-comfyui-provider-p2` 上分别审阅和提交。实现计划应至少区分：

1. 非阻塞清理与接口准备
2. Advanced workflow 与注册表
3. 音频管线和客户端
4. TTS 编排与 FastAPI 路由
5. qwen3_tts/voiceover CLI
6. 真机验收、health 和文档收尾

不得把只有 fake 测试通过、未跑四项真实生成、未跑最终 health 或 BAT 启动失败的状态记为 P2 完成。推送与 PR 不在本规格范围内。
