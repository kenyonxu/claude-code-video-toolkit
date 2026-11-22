# 改造路线图：local ComfyUI provider 分阶段实施

> 状态：**设计已于 2026-11-16 逐节确认，2026-11-17 完成审阅修订，尚未实施**。
> 本文是 [local-comfyui-provider-plan.md](./local-comfyui-provider-plan.md) 的实施路线图。plan 保留技术 spec 的历史定稿，冲突处以本文「与 plan 的修订差异」为准。
> 本次修订依据仓库静态核对、本机节点源码，以及 2026-11-17 对 ComfyUI 0.45.0 @127.0.0.1:9188 的 `/system_stats` 与 `/object_info` 只读复核。未运行生成媒体，运行时元数据不冒充媒体验收。
> 验收原则：**每阶段收口 = 下文可复制的真实验收命令块通过 + CI 等价 unittest 全绿**。纯逻辑单测不能代替真实生成。代码行号只作定位线索，实施时以符号名为准。

## 阶段总览

| 阶段 | 目标 | 收口门槛 | 规模 |
|---|---|---|---|
| **P0** 仓库准备 | fork、upstream、工作分支、设计文档首 commit | origin/upstream URL 与分支名正确 | S |
| **P1** 链路骨架 | `cloud_gpu.py` local 分发、网关与注册表、ComfyUI 客户端、`/health` | `curl --fail-with-body .../health` 返回 `ready=true`、`models_ready=true`；缺模型测试给出可执行修复提示 | M |
| **P2** TTS 路由 | 独立 Confucius4-TTS worker（基础 Transformers）+ `/tts/generate` clone、`confucius4_tts.py` 与 `voiceover.py` 接 local | qwen3 已移除 local：`confucius4_tts` clone MP3、clone WAV、voiceover clone 三项真实生成通过；local clone 不触发外部上传 | M |
| **P3** 图像路由 | `/image/generate`、`/image/edit`、新 `tools/qwen_image.py` | generate 出 1024x1024 PNG；edit 用该图产出新 PNG | M |
| **P4** 视频路由 | `/video/generate`、新 `tools/h3_video.py` | t2v/i2v 出 MP4；t2v 实测 124 帧、24 fps | L |
| **P5** 集成收尾 | `video_job` 全链路、零成本计量、`verify_setup --local`、registry、文档与 CI | Local 检查退出 0；异步 job 到 `phase=done`，estimate/actual/receipts 均为数值 0；MP4 有效 | L |

**依赖**：P0 → P1 → {P2, P3, P4} → P5。P2/P3/P4 只依赖 P1，仍按 P2、P3、P4 串行实施。P5 等三条路由全通。

**统一边界**：

- 所有命令从仓库根目录运行，默认 shell 为 Git Bash。ComfyUI 与网关需在另一个终端先启动。
- 验收产物统一放 `scratch/local-comfyui/`，不在仓库根目录遗留 `test.mp3`、`test.png` 等文件。
- 根目录本地 `.env` 在 P1 写入 **四条** `LOCAL_*` URL，`.env.example` 在 P5 登记。网关自己的 `COMFYUI_BASE_URL`、`COMFYUI_MODELS_ROOT` 也在 P1 配置。
- local 路径不得隐式访问 R2、litterbox、0x0.st、Modal、RunPod、Scribe 或其他云服务。`$0.00` 是显式计量结果，不是缺少价格分支时的偶然值。
- P4 的 i2v 可复用 P3 PNG，也可自备参考图，不构成 P3 → P4 的额外硬依赖。

**P1 起每阶段统一回归**：

```bash
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
```

## 各阶段详情

### P0 仓库准备

- [x] `gh repo fork --remote`：origin 指向 `kenyonxu/claude-code-video-toolkit`，upstream 指向 `digitalsamba/claude-code-video-toolkit`。
- [x] `git checkout -b feat/local-comfyui-provider`。
- [x] 首个 `DOCS:` commit **同时包含 plan 与本文**。两份文件当前都未跟踪，只提交本文会留下断链。
- [x] 首 commit 前直接对两份文件跑 copy lint；plan 中既有的 dash 命中可在 P0 做纯标点清理，不改技术含义。

验收：

```bash
git remote get-url origin
git remote get-url upstream
git branch --show-current
uv run tools/copy_lint.py \
  docs/local-comfyui-provider-plan.md \
  docs/local-comfyui-provider-roadmap.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md
```

前两条分别显示 fork 与上游，分支名为 `feat/local-comfyui-provider`。`git remote -v` 通常有四行，不再用「两行」作判据。

### P1 链路骨架（walking skeleton）

- [x] `tools/cloud_gpu.py` 增加 `_LOCAL_ENV_VARS`，映射固定为：
  - `confucius4_tts` → `LOCAL_TTS_ENDPOINT_URL`
  - `qwen_image` → `LOCAL_IMAGE_ENDPOINT_URL`
  - `image_edit` → `LOCAL_IMAGE_EDIT_ENDPOINT_URL`
  - `h3_video` → `LOCAL_VIDEO_ENDPOINT_URL`
- [x] `get_provider_config("local", tool_name)` 与 `call_cloud_endpoint()` 接入 `_call_local`。保持 `_call_modal` 的 `(result_dict, elapsed_seconds)` 返回形态、payload 解包和错误字典约定；缺少 URL、连接失败、HTTP/JSON 错误返回 `{"error": ...}`，未知 provider 仍抛 `ValueError`，提示包含 local。
- [x] `_call_local` 不使用 token，不显示 Modal 专属提示；网关不可达时提示按能力先启动 Confucius worker 或 ComfyUI，再启动 `start_gateway.bat`。loopback 请求不得继承 HTTP 代理；解析响应后先检查应用层 `error`，再发成功进度事件。
- [x] `local-gateway/` 独立 uv 子项目：`pyproject.toml`、lockfile、FastAPI/uvicorn/requests，无 torch；默认只绑定 `127.0.0.1:8123`。阻塞的 requests/poll 放在普通 `def` 端点或线程中，不得阻塞 `/health`。
- [x] 网关配置：`COMFYUI_BASE_URL` 默认 `http://127.0.0.1:9188`，`COMFYUI_MODELS_ROOT` 指向本机 `models/`。后者用于真实文件检查，不在代码或 tracked 配置中写死 `E:\ugit\...`。
- [x] 原创 ComfyUI 客户端：submit `/prompt`、poll `/history`、view `/view`、upload `/upload/image`。每次 HTTP 调用有独立短超时；长任务的总 deadline 在各路由实现。
- [x] 注册表先建立 P0-P5 所需模型元数据、权重清单与能力标志。路由未实现时不得标为可用。
- [x] `GET /health` 同时做三类检查：`/system_stats` 的 GPU/RAM、`/object_info` 的节点与 loader 可选值、`COMFYUI_MODELS_ROOT` 下的附录 B 文件。返回 `ready`、`models_ready`、逐模型状态、逐路由 `implemented` 和可执行 `errors`。`ready` 只表示当前已启用路由可服务，P1 不得把未实现的 P2-P4 路由标为可用。完全就绪返回 200，否则返回 503；TTS 不得只凭 `model_size` 枚举就宣称权重目录存在。
- [x] `start_gateway.bat` 与网关 README：先 ComfyUI(9188)，再网关(8123)，说明两个网关环境变量与 loopback 限制。无局域网访问需求时，ComfyUI 也应使用 `--listen 127.0.0.1`；本次运行时发现它监听 `0.0.0.0`，不得把该现状写成安全默认值。
- [x] 根目录本地 `.env` 写入四条 URL：TTS、IMAGE、IMAGE_EDIT、VIDEO，均指向 `http://127.0.0.1:8123` 对应能力端点。
- [x] CI 纳入网关：`.github/workflows/python-tests.yml` 的 push/pull_request paths 加 `local-gateway/**`；`lint-python.yml` 的 ruff 目标加 `local-gateway/`。
- [x] 单测：local config 的四个工具键、未知 provider、缺 URL、连接失败与 tuple 契约、应用层 error 进度语义、图像 generate/edit 键选择、ComfyUI prompt 组装和 poll 终止判定。全部 fake 网络。

验收：

```bash
curl --noproxy '*' --fail-with-body http://127.0.0.1:8123/health
```

响应须含 GPU 信息、附录 B 全部必需项、`ready=true`、`models_ready=true`。另用临时测试配置制造缺模型，确认 HTTP 503 与修复提示；不得通过改名真实权重制造故障。

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

### P3 图像路由 + `qwen_image.py`

- [ ] **开工第一步**：用 `/object_info` 校验 `TextEncodeQwenImage21`、loader、latent、采样、保存和 edit 节点。运行时已确认 `resolution` 是参考图送入编码器前的缩放尺寸，不是输出宽高；实施时仍须复核。
- [ ] `POST /image/generate` 与 `/image/edit`：首条目 `qwen-image-2.1`，权重见附录 B，`CLIPLoader.type=qwen_image`。注册表声明默认参数、尺寸约束、最大参考图数、generate/edit 能力；输出宽高只补丁 latent/canvas，编码器 `resolution` 作为独立的参考图缩放参数，默认 1024。不支持的能力明确报错。
- [ ] 本地图像响应统一为 `success`、`image_base64`、`seed`、`model`、实际宽高与实际步数。edit 不另用 `edited_image_base64`，避免客户端分裂。
- [ ] edit：参考图上传至 ComfyUI input，经 `LoadImage` 进入 i2i/edit 通路。**该调用使用 `tool_name="image_edit"` 选择 `LOCAL_IMAGE_EDIT_ENDPOINT_URL`**；generate 才用 `tool_name="qwen_image"`。
- [ ] 新 `tools/qwen_image.py`：CLI 仿 `flux2.py`，含 `--input/-i`（可多图）、`--output/-o`、`--steps`、`--negative-prompt`、`--no-open`，timeout 600。`--model` 为自由字符串，缺省不发送，由网关注册表选择默认值 `qwen-image-2.1` 并校验未知值；禁止静态 argparse choices。
- [ ] 单测：width/height → latent/canvas 映射、编码器 resolution 独立补丁、generate/edit 的 env 与 tool_name 分支、model 透传、参考图数量限制、统一响应解码。

验收：

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p3
uv run tools/qwen_image.py --cloud local --model qwen-image-2.1 \
  --prompt "A red cube on a white table" -W 1024 -H 1024 \
  --output scratch/local-comfyui/p3/generated.png --no-open

test "$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
  -of csv=s=x:p=0 scratch/local-comfyui/p3/generated.png)" = "1024x1024"

uv run tools/qwen_image.py --cloud local --model qwen-image-2.1 \
  --input scratch/local-comfyui/p3/generated.png --prompt "Turn the cube blue" \
  --output scratch/local-comfyui/p3/edited.png --no-open
test "$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
  -of csv=p=0 scratch/local-comfyui/p3/edited.png)" = "png"
```

多参考图只在注册表声明支持后验收，不照搬 FLUX2 的 4 步或三图上限，也不预先承诺 Qwen Modal 的十图能力。

### P4 视频路由 + `h3_video.py`

- [ ] **开工第一步**：用 `/object_info` 校验 H3 t2v/i2v 完整节点链、采样与解码、`VHS_VideoCombine`。静态源码已确认 `length` 采用 `17k+5` 网格、模型为 24 fps，运行时仍需复核。
- [ ] `POST /video/generate`：首条目 `minimax-h3-fl2va`，支持 t2v 与 i2v `first_frame`，`CLIPLoader.type=minimax`，权重见附录 B，输出 `video/h264-mp4`。首版只承诺视频流，不把 H3 AUDIO 接入 VHS，因此音频 VAE 是可选检查项，不阻塞 P0-P5。
- [ ] 只使用本地 H3 节点。禁止 `ComfyCloudMiniMaxH3*`、`MinimaxHailuo*` 等云 API 节点进入 workflow，避免隐性计费与外部调用。
- [ ] 公共请求字段仍是 `num_frames`，只在节点补丁中映射为 `length`。请求不是 `17k+5` 时向上对齐，响应返回实际 `num_frames`；对齐后仍不得超过节点 max 3600，否则报错。首版 fps 只接受 24，`VHS_VideoCombine.frame_rate=24`，其他 fps 明确报错，不靠改封装帧率伪装支持。
- [ ] 宽高按 H3 的 32 倍数约束校验，不继承 LTX2 的 64 倍数或默认尺寸。`quality` 映射注册表推荐步数；显式 `steps` 覆盖 quality 默认值并做范围校验，响应返回实际步数。
- [ ] 新 `tools/h3_video.py`：CLI 仿 `ltx2.py`，`--cloud` 首版只有 local；含 `--input/-i`、`--output/-o`、`--num-frames/-n`、`--fps`、`--quality`、`--steps`、`--no-open`。`--model` 为自由字符串并透传，缺省由网关选 `minimax-h3-fl2va`；CLI timeout 默认 1800 秒。
- [ ] deadline 顺序固定为：网关内部 deadline < CLI/HTTP timeout < P5 `video_job.run_tool` 外层 timeout。客户端超时不会自动取消 ComfyUI；超时错误应带可诊断的 prompt id，不擅自 `/interrupt` 其他任务。
- [ ] 单测：`17k+5` 对齐、非法 fps/尺寸、quality 与 steps 优先级、实际参数响应、t2v/i2v 补丁、model 透传、deadline 配置。

验收使用训练区间内的最短值 124，不使用虽合法但未训练的 5 帧：

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p4
uv run tools/h3_video.py --cloud local --model minimax-h3-fl2va \
  --prompt "A flag moving gently in the wind" --num-frames 124 --fps 24 \
  --quality fast --output scratch/local-comfyui/p4/t2v.mp4 --no-open

uv run tools/h3_video.py --cloud local --model minimax-h3-fl2va \
  --input scratch/local-comfyui/p3/generated.png \
  --prompt "The scene comes alive with subtle camera motion" \
  --num-frames 124 --fps 24 --quality fast \
  --output scratch/local-comfyui/p4/i2v.mp4 --no-open

for name in t2v i2v; do
  ffprobe -v error -select_streams v:0 -count_frames \
    -show_entries stream=nb_read_frames,avg_frame_rate -of json \
    "scratch/local-comfyui/p4/$name.mp4" > "scratch/local-comfyui/p4/$name-probe.json"
  uv run python -I -c 'import json,sys; s=json.load(open(sys.argv[1]))["streams"][0]; assert s["nb_read_frames"]=="124"; assert s["avg_frame_rate"]=="24/1"' \
    "scratch/local-comfyui/p4/$name-probe.json"
done
```

两条生成均允许分钟级耗时。若请求 124 而响应实际帧数不同，仍视为失败，不得只检查 MP4 存在。

### P5 流水线集成与收尾

- [ ] `tools/video_job.py` 完整接线，不再概括为「四符号」：
  - `VISUAL_KINDS` 加 qwen/h3；`normalize_spec()` 分别分配 `images/*.png` 与 `clips/*.mp4`。
  - `_visual_args()` 调用正确 CLI，固定 `--cloud local`，传输出路径、尺寸、seed/steps、H3 帧参数及每场景 `visual.model`，fallback cost 为 0。
  - `_NEEDS` 加三组 local 条目；`missing_for()` 加 qwen/h3 分支，缺 URL 时在 submit 前拒绝。
  - `host_capabilities()`：`kinds` 加 qwen/h3；`voices` 加 `confucius4`（仅 local；`qwen3` 不加 local，保持 Modal/RunPod）；`flux` 与 `music` 不变。同步 `presets()["visual_kinds"]`。
  - `voice.model` 经 `voiceover.py --tts-model` 透传；confucius4 clone 传 `--cloud local`、`--language zh` 与 `voice.refAudio` 对应的 `--ref-audio`，不传 `--ref-text`/`--speaker`。`visual.provider` 不新增语义，qwen/h3 kind 自身表示 local。
  - H3 的 `run_tool` 外层 timeout 大于 CLI/HTTP timeout；`_STEP_SECONDS` 与 `step_model()` 识别 qwen/h3，避免沿用 FLUX 的约 20 秒 ETA。
- [ ] 估价与收据：`confucius4 + local` voice、qwen、h3 显式估价为 0；现有 Modal/RunPod 价格不变。更新 `PRICES_VERSION`，统一 estimate、fallback actual、`remaining_estimates()` 与 receipts，local 收据的 `cost_usd` 必须是数值 `0.0`，不是 `null`。
- [ ] `tools/verify_setup.py` 增加 `--local --json`：只检查四个 LOCAL_*、loopback HTTP URL、`/health`、模型清单及三条路由的 `implemented` 状态，不跑 Modal/RunPod/R2/smoke test。输出 `summary.local.ready`；失败退出 1，成功退出 0。full check 的 voice/summary 也识别 confucius4 local。
- [ ] 测试隔离：`Isolated.KEYS` 加四个 LOCAL_*，防止开发者 `.env` 污染 bare-host 用例。
- [ ] 单测：asset 路径、visual args 与 model 透传、`missing_for()`、host/presets、H3 外层 timeout、估价/actual/receipts/resume 均为 0、verify_setup Local-only 退出码；扩展 fake pipeline 覆盖 qwen 与 h3。真实 P5 E2E 不必再次生成 H3，但不能用 fake 取代 P4 的真实 H3 验收。
- [ ] `.env.example` 登记四条 LOCAL_*；更新 README、`skills/video-toolkit/SKILL.md`、`docs/agent-video-jobs.md`。后者写明 voice.cloud、visual.model、异步 submit/status/result/resume、local 零成本收据与 H3 耗时。
- [ ] `_internal/toolkit-registry.json` 登记 qwen_image/h3_video，更新 qwen3_tts/voiceover/verify_setup/video_job 与 local cloud provider、四条 env。新增工具同步 agent-facing skill。
- [ ] 文档列出模型权重与节点包许可证注意事项。零 API 账单不代表无硬件、电费或模型使用限制。

验收分两层：

1. `uv run tools/verify_setup.py --local --json` 退出 0。
2. 附录 C 的异步 E2E 命令块退出 0。最小 spec 显式关闭 captions/music、强制 local 背景，voice 为 confucius4 local clone（`lang=zh` + `refAudio`，无 speaker/ref_text/sampling），包含一幕 qwen 图与一幕 text；断言 estimate/actual/receipts 全为 0、无 R2 URL、MP4 有效。H3 的 job 参数与超时由 fake pipeline 覆盖。

## 决议（设计确认记录）

1. **CLI 旗标沿用仓库惯例**：新工具用 `--input/-i`、`--output/-o`、`--steps`，payload 字段才叫 `num_inference_steps`；视频用 `--num-frames/-n`。
2. **`host_capabilities()` 按四个枚举 dict 处理**：flux 不变，kinds 加 qwen/h3，voices 加 `confucius4`（仅 local；qwen3 不加 local），music 不变。
3. **单测集中纯逻辑与集成边界**：P1 测 cloud_gpu/客户端，P2-P4 测契约翻译，P5 测 video_job 全链路、价格、隔离和 verify_setup。所有单测不碰真网关或真 ComfyUI，真实媒体验收单独执行。
4. **`start_gateway.bat` 与网关 README 属于 P1**。
5. **许可证红线贯穿全程**：客户端与 workflow 原创实现，不复制 OpenMontage 代码或第三方 example workflow JSON；可借鉴公开协议与节点连接概念。引入任何素材前先查许可证，提 PR 前自查 diff。
6. **P5 保持单阶段，规模上调为 L**；实施时仍可拆 5a（集成与检查器）/5b（registry 与文档）。
7. **P2-P4 开工先校验 `/object_info`**；附录 A 是 2026-11-16 实测加 2026-11-17 本地源码与 `/object_info` 复核的基线，不代替后续实施时的运行时校验。
8. **三条路由统一「能力端点 + 变体注册表」**：端点按能力命名，请求带可选 `model`，注册表条目含 workflow、loader 补丁、推荐参数、能力标志。新增模型时 **ccvt 零改动**，但网关可同时需要注册表与新 workflow，不承诺只改一行。所有相关 CLI 用自由字符串透传，网关负责默认值、未知模型与能力校验。

## 与 plan 的修订差异

| # | plan 原文或原路线图 | 修订为 | 原因 |
|---|---|---|---|
| 1 | 模型专属端点 | `/tts/generate`、`/image/generate`、`/image/edit`、`/video/generate` | 能力端点支持多模型 |
| 2 | 三条模型专属 env | 四条 `LOCAL_TTS/IMAGE/IMAGE_EDIT/VIDEO_ENDPOINT_URL` | 能力命名，edit 独立镜像现有配置方式 |
| 3 | `--image`、`--out`、`--num-inference-steps` | `--input/-i`、`--output/-o`、`--steps` | 对齐真实 CLI；qwen3_tts 也只有 `--output/-o` |
| 4 | 写死 121 帧，或帧数等于请求的 `length` | 公共字段为 `num_frames`；H3 向上对齐 `17k+5`，响应返回实际值；124 帧验收必须相等 | `length` 只是节点字段，H3 有自己的网格 |
| 5 | host_capabilities 三处枚举、voices 待决定 | 四个 dict；kinds 与 voices 都改，flux/music 不改 | 静态代码核对结果 |
| 6 | 请求无 model，或 CLI 静态 choices | TTS/image/video 都可透传自由字符串 model，缺省由网关决定 | 新增模型不再改 ccvt |
| 7 | qwen_image 统一 `tool_name="qwen_image"` | generate 用 qwen_image，edit 用 image_edit | 现有 config 签名只能靠 tool_name 选择两条 URL |
| 8 | clone 只改网关接收 base64 | qwen3_tts local 分支直接 base64，跳过外部上传与 R2 | 当前客户端默认先上传参考音频 |
| 9 | H3 fps 映射到任意 VHS frame_rate | 首版只接受模型原生 24 fps，其他值报错 | 改封装 fps 会改变速度，不等于模型支持该 fps |
| 10 | Qwen 图像完全沿用 FLUX2 契约 | 本地 generate/edit 统一 `image_base64`，步数与参考图能力由注册表声明 | FLUX2 与 Qwen edit 默认值、响应字段和上限不同 |
| 11 | P5 只改四个符号，submit 直接出 MP4 | 补 asset/missing_for/价格/收据/timeout/ETA/presets；submit 后轮询 status，再取 result | submit 是异步，原清单不足以跑通 job |
| 12 | `/health` 只经 `/object_info` 推断全部权重 | `/object_info` 查节点与 loader，`COMFYUI_MODELS_ROOT` 查真实文件 | TTS 目录存在性不能由 model_size 枚举证明 |
| 13 | `$0.00` 只作为 E2E 预期 | local 价格、actual、remaining 与 receipts 全部显式为数值 0，并隔离其他云配置 | 现有 qwen3 voice 默认估价非零，默认 captions/music/R2 也可能出外部调用 |
| 14 | width/height 同时映射 `TextEncodeQwenImage21.resolution` 与 latent | width/height 只控制 latent/canvas；resolution 独立控制参考图缩放，默认 1024 | 2026-11-17 `/object_info` tooltip 明确 resolution 作用于 reference images |

## 附录 A：参数映射基线

| 差异点 | ccvt 公共契约 | ComfyUI/H3 实际处理 |
|---|---|---|
| TTS clone（Confucius4） | `text` + `ref_audio_base64` + `language`（14 小写码）+ 可选 `model=confucius4-tts` | 网关逐项 multipart 转发 worker `/api/tts`；custom_voice、voice_design、ref_text、speaker、instruct 与 sampling 参数在本地不可用（`mode_unsupported`） |
| TTS 格式 | `output_format=mp3\|wav` | worker 固定输出 PCM16 mono 22050 Hz WAV；MP3 由网关用 libmp3lame 真转码，不改名伪装 |
| 图像分辨率 | `width` + `height` | 只补丁 latent/canvas；`TextEncodeQwenImage21.resolution` 独立控制参考图缩放，默认 1024，不是输出宽高别名 |
| CLIP 类型 | 注册表模型条目 | Qwen Image 用 `CLIPLoader.type=qwen_image`，H3 用 `minimax`，不从文件名猜测 |
| H3 帧数 | `num_frames` | 节点 `length` 默认 124，min 5、max 3600，向上对齐 `17k+5`；训练区间约 124-362 |
| H3 fps | `fps` | 模型固定 24 fps，VHS `frame_rate=24`；首版拒绝其他值 |
| H3 尺寸 | `width` + `height` | 32 倍数，默认节点为 1344x768，不继承 LTX2 约束 |
| quality/steps | `quality`、可选 `num_inference_steps` | quality 选注册表推荐步数，显式 steps 覆盖默认值，响应返回实际步数 |

H3 网格、24 fps 与 `CLIPLoader.type` 已于 2026-11-17 通过本机源码和 `/object_info` 复核；Qwen resolution 语义及 loader 权重清单也已通过运行时元数据复核。旧 TTS 节点复核结论随 Qwen 路线废止。附录 B 的图像与视频文件已在文件系统逐项确认（旧 Qwen `TTS/Qwen3-TTS/` 目录检查同随 Qwen 路线废止，不再属于附录 B）。2026-11-18 的三项真实 clone 生成属已废止的 Qwen 路线，不构成 Confucius 验收；Confucius4 的 TTS 验收仅为上方 P2 未勾选清单对应的真实验收门。图像与视频仍未运行真实生成。

## 附录 B：`/health` 模型检查清单

以下路径均相对 `COMFYUI_MODELS_ROOT`。健康检查同时验证文件存在与相关 `/object_info` 节点/loader 可用：

- TTS：Confucius4-TTS worker `127.0.0.1:8765`（仓库内 `models/hf` 缓存 T2S/S2A、w2v-bert-2.0、CAMPPlus、BigVGAN；`HF_HUB_CACHE` 指向该目录，不依赖 COMFYUI_MODELS_ROOT 或用户 home cache）。
- 图像：`diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors`、`text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors`、`text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors`、`vae/qwen_image_2.1_vae_bf16.safetensors`。
- 视频：`diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors`、`vae/minimax_h3_video_vae_fp16.safetensors`、`vae/minimax_h3_image_vae_fp16.safetensors.safetensors`。最后一个双扩展名是本机实测文件名，不在代码里擅自修正。
- 可选视频音频：`vae/minimax_h3_audio_vae_fp32.safetensors` 已在运行时发现且文件存在。首版不接 H3 生成音轨，因此只报告其状态，不纳入 P0-P5 `models_ready` 门槛。

## 扩展槽位（P0-P5 范围外）

| 槽位 | 网关侧工作 | ccvt 侧工作 | 规模 |
|---|---|---|---|
| `moss-image` | 注册表、workflow、权重与许可证检查 | 无，现有 `--model moss-image` 直接选择 | S |
| `h3-image` | 注册表、workflow、权重与许可证检查 | 无 | S |
| `qwen3-tts-0.6B` | TTS 注册表变体与能力检查 | 无，`qwen3_tts --model` 或 `voice.model` 选择 | S |
| `minimax-h3-ref2va` | 视频注册表、ref2va workflow 与能力检查 | 无，`h3_video --model` 或 `visual.model` 选择 | S/M |

「ccvt 零改动」不等于网关只改注册表，也不免除真实生成验收。

## 风险与对策

| # | 风险 | 对策 |
|---|---|---|
| 1 | 节点名或参数与运行时不符 | P2-P4 先过 `/object_info`，不通过则不写路由 |
| 2 | H3 慢或外层先超时 | 124 帧冒烟；网关 < CLI < run_tool 的 deadline；错误保留 prompt id |
| 3 | 客户端超时后后台仍生成 | 文档明确非取消语义；不全局 `/interrupt`，必要时由用户按 prompt id 处理 |
| 4 | AGPL 或模型许可证污染 | 原创代码与 workflow；第三方素材和模型许可证分别审查 |
| 5 | 上游同步冲突或重写公开历史 | 每阶段开工前同步 upstream/main；已推送分支不重写历史，确需重写时先确认并用 force-with-lease |
| 6 | 代理、事件循环阻塞或未启动导致误报 | loopback 禁用环境代理；阻塞调用放线程；health 与客户端都给可执行提示 |
| 7 | local 路径偷偷访问外部存储 | clone 单测断言外部上传零调用；P5 清空云配置并断言无 R2 URL |
| 8 | 缺 VAE 或健康检查假绿 | 附录 B 完整文件检查 + object_info；缺项 HTTP 503 |
| 9 | 零成本只是漏写价格分支 | 显式价格版本、estimate/actual/resume/receipts 数值 0 断言 |
| 10 | ComfyUI 暴露到非 loopback 接口 | 本次发现 `0.0.0.0:9188`；无局域网需求时改绑 `127.0.0.1`，需要对外提供时先确认并配置访问控制 |

## 提交纪律

- 每阶段少量 commit，消息沿用仓库的大写类型前缀 `FEAT:`、`DOCS:`、`FIX:`。
- 收口顺序：真实验收命令 → CI 等价 unittest → ruff → copy lint → 勾选本文 checkbox → `git status` 检查 → commit。checkbox 必须在实际验收后勾选。
- 首 commit 同时包含 plan 与本文。首 commit 前直接 lint 两文件，后续执行：

```bash
uv run tools/copy_lint.py --diff upstream/main --commits upstream/main \
  . ':(exclude).claude/skills/remotion-official'
```

- 不把 `submit` 返回 `queued`、单测全绿或文件存在误记为真实生成成功。E2E 必须到 `phase=done` 并验证结果与收据。

## 附录 C：P5 零外部调用 E2E 验收命令块

以下命令在 P5 实现完成后运行。P5 尚未实现，今天执行预期会失败，不能据此勾选验收。

```bash
set -euo pipefail
ROOT="$(cygpath -m "$PWD")"
mkdir -p "$ROOT/scratch/local-comfyui"
S="$(mktemp -d "$ROOT/scratch/local-comfyui/p5.XXXXXX")"
mkdir -p "$S/jobs" "$S/logs"

export VIDEO_JOBS_DIR="$S/jobs"
export MEDIA_SPEND_LOG="$S/logs/media-spend.log"
export VIDEO_JOB_WORKSPACE=
export LONGARM_WORKSPACE=
export VIDEO_JOB_RESERVE_AT_SUBMIT=0
export VIDEO_JOB_SKIP_HOST_CHECK=0

# 防止 load_dotenv() 或已配置环境把验收切回云服务。
export ELEVENLABS_API_KEY= IDEOGRAM_API_KEY= ACEMUSIC_API_KEY= SIXTYDB_API_KEY=
export R2_ACCOUNT_ID= R2_ACCESS_KEY_ID= R2_SECRET_ACCESS_KEY=
export MODAL_QWEN3_TTS_ENDPOINT_URL= MODAL_FLUX2_ENDPOINT_URL=
export RUNPOD_API_KEY= RUNPOD_QWEN3_TTS_ENDPOINT_ID= RUNPOD_FLUX2_ENDPOINT_ID=

uv run tools/verify_setup.py --local --json > "$S/verify.json"

# voice.refAudio 指向只读参考音频（Confucius 规格 14.1 素材，按需替换为本机素材路径）；
# confucius4 local clone 不接受 speaker/ref_text/sampling。
cat > "$S/spec.json" <<'JSON'
{
  "title": "Local provider smoke test",
  "format": "16:9",
  "voice": {"provider": "confucius4", "cloud": "local", "lang": "zh", "refAudio": "C:/Users/kenyo/Downloads/Music/voice_02.mp3"},
  "backgrounds": "local",
  "captions": {"enabled": false},
  "music": false,
  "scenes": [
    {
      "slug": "image",
      "text": "This scene uses an image rendered on the local gateway.",
      "visual": {
        "kind": "qwen",
        "model": "qwen-image-2.1",
        "prompt": "A plain blue circle on a white background, no text"
      }
    },
    {
      "slug": "text",
      "text": "This scene uses a background drawn on this machine.",
      "visual": {"kind": "text", "headline": "LOCAL BACKGROUND", "source": "local"}
    }
  ]
}
JSON

SUBMIT="$(uv run tools/video_job.py submit --spec "$S/spec.json" --max-usd 1 --json)"
JOB_ID="$(printf '%s' "$SUBMIT" | uv run python -I -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
PHASE=
for ((i=0; i<180; i++)); do
  STATUS="$(uv run tools/video_job.py status "$JOB_ID" --json)"
  PHASE="$(printf '%s' "$STATUS" | uv run python -I -c 'import json,sys; print(json.load(sys.stdin)["phase"])')"
  if [[ "$PHASE" == "done" ]]; then break; fi
  if [[ "$PHASE" == "failed" || "$PHASE" == "cancelled" ]]; then
    printf '%s\n' "$STATUS"
    exit 1
  fi
  sleep 20
done
[[ "$PHASE" == "done" ]] || { printf 'job timed out\n'; exit 1; }

printf '%s\n' "$STATUS" > "$S/status.json"
uv run tools/video_job.py result "$JOB_ID" --json > "$S/result.json"

uv run python -I -c '
import json, subprocess, sys
from pathlib import Path
s = Path(sys.argv[1])
r = json.loads((s / "result.json").read_text(encoding="utf-8"))
status = json.loads((s / "status.json").read_text(encoding="utf-8"))
assert r["ready"] and r["phase"] == "done"
assert r["estimate_usd"] == 0.0 and r["actual_usd"] == 0.0
assert r.get("url") is None
assert not (status.get("output") or {}).get("r2_key")
receipts = [json.loads(x) for x in (s / "jobs" / r["id"] / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
assert {x["tool"] for x in receipts} == {"voiceover.py", "qwen_image.py"}
assert all(x["cost_usd"] == 0.0 for x in receipts)
assert all(x["args"][x["args"].index("--cloud") + 1] == "local" for x in receipts)
mp4 = Path(r["mp4"])
assert mp4.is_file() and mp4.stat().st_size > 0
duration = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(mp4)], text=True).strip())
assert duration > 0
print(mp4)
' "$S"
```

失败后只有人工确认可重试时才运行 `resume JOB_ID --max-usd 1 --json`。resume 同样只返回 queued，必须重新执行 status 轮询与 result 断言。
