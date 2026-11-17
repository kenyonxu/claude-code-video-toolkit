# P0/P1 设计规格：local ComfyUI provider 仓库准备与链路骨架

> 状态：**分节设计已于 2026-11-17 获确认，待书面规格审阅，未实施**。
> 上游文档：[技术 plan](./local-comfyui-provider-plan.md) 与 [实施路线图](./local-comfyui-provider-roadmap.md)。本文细化其中的 P0/P1，不重新打开已确认的 P2-P5 范围。
> 本文中的「必须」是验收要求，「不得」是边界。实施前不得把 checkbox 或路由能力标为已完成。

## 1. 背景与目标

仓库当前只有 RunPod/Modal provider。P0 建立可提交的 fork 工作区，P1 建立不依赖 torch 的本地网关骨架，使后续 TTS、图像和视频路由可以共享同一套配置、模型注册表、ComfyUI 客户端与健康检查。

P1 完成时必须同时满足：

1. `tools/cloud_gpu.py` 能以 local provider 调用一个同步 HTTP JSON 端点，并保持现有 `(result_dict, elapsed_seconds)` 契约。
2. 独立网关能在 loopback 启动，读取自己的 `.env`，检查 ComfyUI 连通性、首发模型文件和节点元数据。
3. `/health` 对本机全部首发模型返回 `ready=true`、`models_ready=true`，同时明确四条生成路由尚未实现。
4. 根项目测试和网关测试分别在各自 uv 环境运行，不互相引入运行时依赖。
5. 所有网络单测均使用 fake，不访问真实 ComfyUI，不提交真实 `/prompt`，不生成媒体。

## 2. 非目标

P0/P1 不做以下工作：

- 不实现 `/tts/generate`、`/image/generate`、`/image/edit`、`/video/generate`。
- 不创建 workflow JSON，不实现 TTS、图像或视频契约翻译。
- 不修改 `qwen3_tts.py`、`voiceover.py`，不新增 `qwen_image.py` 或 `h3_video.py`。
- 不修改 `video_job.py`、`verify_setup.py`、根目录 `.env.example` 或 toolkit registry。
- 不把网关纳入根 uv workspace，不给根项目增加 FastAPI/uvicorn 依赖。
- 不自动修改 ComfyUI 的启动参数。当前 `0.0.0.0:9188` 只在文档中标记风险。
- 不实现生成任务取消、POST 重试、结果缓存、插件发现或远程 ComfyUI 支持。
- 不在 main 上提交规格或代码，不在设计批准前执行 fork、建分支或脚手架。

## 3. 已确认的设计决策

1. **P0/P1 共用一份规格**，二者构成仓库准备与 walking skeleton，不再拆成独立项目。
2. **网关使用独立 `local-gateway/.env`**。根目录 `.env` 只保存四条 LOCAL_* URL。
3. **网关模块化，根测试与网关测试分离**。网关运行时依赖只存在于自己的 uv 项目。
4. **P1 只接受 loopback HTTP**。远程访问、认证和远程模型文件检查不在当前范围。
5. **P1 的 `/health` 可以基础设施就绪，但四条生成路由必须仍为 unavailable**。
6. **规格先在 main 的工作区写入，不在 main 提交**。P0 执行后与 plan、路线图一起进入分支首个文档 commit。

## 4. P0：仓库准备

### 4.1 只读预检

实施 P0 前必须确认：

```bash
gh auth status
git status --short --branch --untracked-files=all
git remote -v
git show-ref --verify --quiet refs/heads/feat/local-comfyui-provider; test $? -eq 1
```

另用 `gh repo view kenyonxu/claude-code-video-toolkit` 确认 fork 尚不存在。若 gh 未登录、fork/分支已存在或工作区出现预期外改动，停止并报告，不尝试自动覆盖。

当前预期未跟踪文档为：

- `docs/local-comfyui-provider-plan.md`
- `docs/local-comfyui-provider-roadmap.md`
- `docs/local-comfyui-provider-p0-p1-design.md`
- `docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md`

### 4.2 建立工作区

获得实施批准后，按顺序执行：

1. `gh repo fork --remote`。
2. `git checkout -b feat/local-comfyui-provider`。
3. 检查远程实际结果，必要时只修正 remote 名称和 URL，使最终状态为：
   - origin：`https://github.com/kenyonxu/claude-code-video-toolkit.git`
   - upstream：`https://github.com/digitalsamba/claude-code-video-toolkit.git`
4. plan 只做 copy lint 所要求的标点清理，不改变技术含义。
5. 首个 `DOCS:` commit 同时包含 plan、路线图和本文。

### 4.3 P0 验收

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

`git status` 输出必须只显示 `## feat/local-comfyui-provider...` 与已提交文件，`--untracked-files=all` 确认没有漏提交的验收产物。

P0 没有代码改动，不要求运行 Python 测试。P0 checkbox 只能在远程、分支、文档和首 commit 全部完成后勾选。

## 5. P1：模块与依赖边界

```text
tools/cloud_gpu.py
tests/python/test_cloud_gpu_local.py

local-gateway/
├── pyproject.toml
├── uv.lock
├── .env.example
├── README.md
├── start_gateway.bat
├── local_gateway/
│   ├── __init__.py
│   ├── config.py
│   ├── registry.py
│   ├── comfyui.py
│   ├── health.py
│   └── server.py
└── tests/
    ├── test_config.py
    ├── test_comfyui.py
    └── test_health.py
```

另修改：

- `.github/workflows/python-tests.yml`
- `.github/workflows/lint-python.yml`
- `docs/local-comfyui-provider-roadmap.md`，同步双测试命令和最终 checkbox

各模块职责：

| 模块 | 唯一职责 | 不得承担 |
|---|---|---|
| `config.py` | 读取、规范化、校验网关配置 | HTTP 请求、健康聚合 |
| `registry.py` | 声明模型、文件、节点、选项和路由实现状态 | 文件系统或网络访问 |
| `comfyui.py` | ComfyUI HTTP 协议与 poll 终止语义 | FastAPI 路由、模型注册 |
| `health.py` | 聚合配置、文件、节点、GPU/RAM 和错误 | workflow 生成 |
| `server.py` | app factory、依赖装配、`GET /health` | 业务规则堆叠 |
| `tools/cloud_gpu.py` | ccvt 侧 provider 配置与同步 transport | ComfyUI 专用协议 |

`local-gateway/pyproject.toml` 要求 Python `>=3.10`。运行时依赖只有 FastAPI、uvicorn、requests、python-dotenv；`httpx` 只放在 dev dependency group 供 TestClient 使用。`uv.lock` 必须提交。不得引入 torch、ComfyUI Python 包或 OpenMontage 代码。

## 6. 配置设计

### 6.1 网关 `.env`

`local-gateway/.env.example` 内容：

```ini
COMFYUI_BASE_URL=http://127.0.0.1:9188
COMFYUI_MODELS_ROOT=E:/path/to/ComfyUI/models
```

实际 `local-gateway/.env` 被现有根 `.gitignore` 的 `.env` 规则覆盖，不得提交。`config.py` 使用 python-dotenv 加载网关目录 `.env`，`override=False`，进程环境变量优先。

配置规则：

- `COMFYUI_BASE_URL` 默认 `http://127.0.0.1:9188`，去除末尾 `/`。
- base URL 只允许 scheme `http`、loopback host、可选端口，不允许用户名密码、query、fragment 或非根路径。
- loopback host 只接受 `127.0.0.1`、`localhost`、`::1`。
- `COMFYUI_MODELS_ROOT` 无默认值，展开 `~` 后必须是绝对目录。
- 缺配置、非法 URL 或目录不存在不阻止 app 启动；`/health` 返回 503 和对应修复动作。

### 6.2 根目录 `.env`

P1 在本地根 `.env` 写入或更新以下四项，已存在的其他配置保持不变：

```ini
LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate
LOCAL_IMAGE_ENDPOINT_URL=http://127.0.0.1:8123/image/generate
LOCAL_IMAGE_EDIT_ENDPOINT_URL=http://127.0.0.1:8123/image/edit
LOCAL_VIDEO_ENDPOINT_URL=http://127.0.0.1:8123/video/generate
```

根 `.env.example` 仍在 P5 更新。`start_gateway.bat` 不得改写根 `.env`。

## 7. `cloud_gpu.py` local transport

### 7.1 Provider 配置

新增 `_LOCAL_ENV_VARS`：

```text
qwen3_tts  -> LOCAL_TTS_ENDPOINT_URL
qwen_image -> LOCAL_IMAGE_ENDPOINT_URL
image_edit -> LOCAL_IMAGE_EDIT_ENDPOINT_URL
h3_video   -> LOCAL_VIDEO_ENDPOINT_URL
```

`get_provider_config("local", tool_name)` 返回 `{"endpoint_url": ...}`。未登记工具或缺环境变量得到 `None`，由 `_call_local` 返回可执行错误。模块 docstring、函数 docstring、provider 参数说明和未知 provider 错误均加入 local。既有 RunPod/Modal 分支的配置键和分支专属错误文本不得因 local 改动而改变；共享的未知 provider 错误按本节要求加入 local。

### 7.2 请求规则

`_call_local` 保持 `_call_modal` 的同步调用形态，并遵循以下规则：

1. 缺 URL 时在发请求前返回 `({"error": ...}, 0)`，错误指出对应工具和 LOCAL_* 配置。
2. local endpoint 只允许 HTTP loopback，不允许用户名密码、query 或 fragment；endpoint 必须带非根路径，如 `/tts/generate`。第 6.1 节针对 ComfyUI base URL 的根路径限制不应用于此处。
3. 解包 `payload.get("input", payload)`，只发送该结果。
4. 每次调用创建并关闭一个 `requests.Session`，`trust_env=False`。
5. 请求头只有 `Content-Type: application/json`，不得发送 Modal/RunPod 凭据。
6. 单次 POST，不重试。timeout 为 `(5, timeout + 30)`，分别是连接与读等待秒数。
7. heartbeat 与 `_call_modal` 相同，但文档必须说明读 timeout 不是总 deadline，客户端超时不会取消 ComfyUI 任务。
8. P1 不修改 `_estimate_cost`，显式 local 零成本由 P5 处理。

### 7.3 响应与进度

仅当 HTTP 200、JSON 顶层为 dict 且没有 truthy `error` 时返回成功。成功仍返回 `(result, elapsed)`，随后发出 complete/success。

以下情况全部返回 `({"error": message}, elapsed)`，不得抛 transport 异常：

- 连接失败、连接/读 timeout
- 非 200 HTTP 状态
- 非 JSON 响应或 JSON 顶层不是 dict
- HTTP 200 但包含应用层 `error`
- 其他 requests 或响应解析异常

非 200 响应优先提取 JSON 的 `error` 或 `detail`，否则使用响应文本，最多保留 500 字符。连接失败提示先启动 ComfyUI 和 `start_gateway.bat`。应用层 `error` 必须发出 error 进度事件，不得再发 complete/success。未知 provider 仍在配置阶段抛 `ValueError`。

## 8. ComfyUI 客户端

`ComfyUIClient` 使用独立 Session，`trust_env=False`。每个网关请求创建自己的客户端并关闭，避免跨线程共享 Session。客户端支持上下文管理器和 `close()`。

### 8.1 方法契约

| 方法 | 行为 | 成功返回 |
|---|---|---|
| `system_stats()` | `GET /system_stats` | JSON dict |
| `object_info(node_name=None)` | `GET /object_info` 或 `/object_info/{node}` | JSON dict |
| `submit(prompt)` | `POST /prompt` | `prompt_id` 字符串 |
| `history(prompt_id)` | `GET /history/{prompt_id}` | JSON dict |
| `view(filename, subfolder, file_type)` | `GET /view`，传三个 query 参数 | 响应 bytes |
| `upload(filename, content, content_type)` | multipart `POST /upload/image`，`type=input` | JSON dict |
| `wait(prompt_id, deadline, poll_interval)` | 单调时钟轮询 history | 成功的 history entry |

普通 HTTP 调用使用 `(2, 10)` 连接/读 timeout。POST 不重试，GET 也不在 P1 自动重试。`view` 的 `file_type` 默认 `output`，`subfolder` 默认空字符串。`upload` 的 multipart 字段名固定为 `image`，即使内容是音频也保持 ComfyUI 协议名称；表单值 `overwrite` 固定序列化为字符串 `false`，不得默认覆盖已有输入文件。

### 8.2 `wait` 终止语义

- history 中没有 prompt id：继续等待。
- entry 的 `status.completed=true` 且 `status.status_str="success"`：返回 entry。
- entry 表示执行错误：抛 `ComfyUIError`，保留 prompt id 和可诊断状态消息。
- 到 `time.monotonic()` 指定的 deadline 仍未终止：抛 `ComfyUITimeout`，包含 prompt id。
- `poll_interval` 必须大于 0；deadline 必须大于当前单调时钟。
- 不自动调用 `/interrupt`，不声称客户端超时取消后台任务。

### 8.3 客户端错误

`ComfyUIError` 至少包含 `message`、`code` 和可选 `details`。连接失败、HTTP 错误、JSON 错误、缺失 prompt id、执行失败都使用结构化错误。`ComfyUITimeout` 是其子类。健康检查翻译这些错误，不能把 traceback 或完整启动参数返回给调用方。

## 9. 注册表

`registry.py` 只含普通 Python 数据和纯查询函数，不访问文件系统或网络。P1 不需要插件机制、动态 import 或 JSON schema 框架。

### 9.1 模型条目字段

每个模型条目必须声明：

- `key`、`capability`
- `required_files`、`required_directories`、`optional_files`
- `required_nodes`
- `required_options`，每项为 `{node, input, values}`

`required_options` 同时覆盖 loader choices 和 TTS 节点枚举。文件路径均相对 `COMFYUI_MODELS_ROOT`，响应不得泄漏完整本机路径。

### 9.2 首发条目

**`qwen3-tts-1.7B`，capability `tts`**

- required directory：`TTS/Qwen3-TTS/`
- required nodes：`AILab_Qwen3TTSCustomVoice`、`AILab_Qwen3TTSVoiceClone`、`AILab_Qwen3TTSVoiceDesign`、`LoadAudio`、`SaveAudioAdvanced`
- required options：三个 AILab 节点的 `model_size` 均包含 `1.7B`；CustomVoice 的 `speaker` 包含 `Ryan`

**`qwen-image-2.1`，capability `image`**

- required files：
  - `diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors`
  - `text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors`
  - `text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors`
  - `vae/qwen_image_2.1_vae_bf16.safetensors`
- required nodes：`UNETLoader`、`CLIPLoader`、`VAELoader`、`TextEncodeQwenImage21`、`KSampler`、`SaveImage`、`LoadImage`
- required options：对应 UNET、两个 CLIP 文件、VAE 文件均出现；`CLIPLoader.type` 包含 `qwen_image`

**`minimax-h3-fl2va`，capability `video`**

- required files：
  - `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors`
  - `text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors`
  - `vae/minimax_h3_video_vae_fp16.safetensors`
  - `vae/minimax_h3_image_vae_fp16.safetensors.safetensors`
- optional file：`vae/minimax_h3_audio_vae_fp32.safetensors`
- required nodes：`UNETLoader`、`CLIPLoader`、`VAELoader`、`EmptyMiniMaxH3LatentAV`、`MiniMaxH3ImageToVideo`、`KSampler`、`VAEDecode`、`VHS_VideoCombine`
- required options：对应 UNET、CLIP、video/image VAE 文件均出现；`CLIPLoader.type` 包含 `minimax`；`VHS_VideoCombine.format` 包含 `video/h264-mp4`

P1 节点集合是健康检查基线，不声称已验证未来 workflow 的全部节点连接。P2-P4 开工时仍必须重新校验 `/object_info` 并扩充注册表。云 API 节点不得进入注册表。

### 9.3 路由条目

```text
tts            -> qwen3-tts-1.7B
image_generate -> qwen-image-2.1
image_edit     -> qwen-image-2.1
video          -> minimax-h3-fl2va
```

P1 四项 `implemented=false`。后续阶段只能在真实路由验收通过后改为 true。

## 10. `/health` 设计

### 10.1 数据流

`server.py` 提供 `create_app(settings=None, client_factory=None)` 和模块级 `app = create_app()`。测试通过参数注入配置和 fake client factory；创建 app 本身不得发网络请求。`health.py` 的核心聚合函数返回 `(payload, http_status)`，FastAPI 层只负责依赖装配和响应。

处理顺序：

1. 校验配置。
2. 通过独立 ComfyUIClient 获取 `/system_stats`。
3. 按需获取节点 `/object_info/{node}`，同一次健康请求内复用已获取的 loader 元数据。
4. 在 `COMFYUI_MODELS_ROOT` 下检查 required paths。
5. 聚合逐模型、逐路由和全局状态。
6. 返回白名单字段，不代理原始 `argv` 或启动命令。

P1 不做跨请求缓存。`/health` 使用普通 `def`，由 Starlette 在线程池执行阻塞操作。

### 10.2 响应结构

```text
ready: boolean
models_ready: boolean
comfyui: {
  base_url: string,
  reachable: boolean,
  version: string | null,
  gpu: { name, vram_total, vram_free } | null,
  ram: { total, free } | null
}
models: {
  model_key: {
    ready: boolean,
    files_ready: boolean,
    nodes_ready: boolean,
    optional_files_present: string[],
    missing_files: string[],
    missing_nodes: string[],
    missing_loader_values: string[]
  }
}
routes: {
  route_key: { implemented: boolean, available: boolean }
}
errors: [
  { code: string, scope: string, message: string, action: string }
]
```

逐模型 `ready = files_ready && nodes_ready`，optional files 不参与 ready，只在 `optional_files_present` 报告已存在的相对路径。`missing_loader_values` 字符串统一为 `node.input=value`。`models_ready` 仅在三个首发模型都 ready 时为 true。

ComfyUI 不可达时仍报告文件检查结果，但 `nodes_ready=false`，`missing_nodes` 和 `missing_loader_values` 保持空数组，表示未验证而不是全部缺失。模型根目录未通过配置校验时，`files_ready=false`，`missing_files` 同样保持空数组。

全局：

```text
ready = config_valid && comfyui.reachable && models_ready
```

`ready` 只表示 P1 基础设施和模型基线就绪，不表示生成路由可用。未来路由 `available = implemented && 对应模型 ready`，P1 四条路由均为 false。

`ready=true` 返回 HTTP 200，否则返回 503。响应仍包含完整检查结果，便于一次修复多个问题。

### 10.3 错误码

| code | 触发条件 | action 必须指出 |
|---|---|---|
| `config_missing` | 缺模型根目录配置 | 编辑 `local-gateway/.env` 并设置绝对路径 |
| `config_invalid` | 非法 URL 或模型根目录 | 修正具体配置项 |
| `comfyui_unreachable` | ComfyUI 连接失败 | 先启动 9188 ComfyUI，再检查 base URL |
| `model_file_missing` | 文件或目录缺失 | 相对路径及应放入的模型目录 |
| `node_missing` | 必需节点不存在 | 安装/启用对应本地节点包 |
| `loader_option_missing` | loader 或枚举缺值 | 具体 node/input/value |
| `comfyui_error` | ComfyUI HTTP/JSON 错误 | 检查 ComfyUI 日志和版本 |
| `internal_error` | 未预期网关异常 | 查看网关日志，不返回 traceback |

## 11. 启动与安全边界

`start_gateway.bat` 使用脚本所在目录，不要求调用者先 cd：

```bat
uv run --directory "%~dp0." uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123
```

网关 README 必须说明：

1. 先启动 ComfyUI，再启动网关。
2. 如何复制并编辑网关 `.env.example`，且不得覆盖已有 `.env`。
3. 默认只绑定 loopback，不启用 CORS。
4. 无局域网需求时 ComfyUI 也应绑定 `127.0.0.1`；当前 `0.0.0.0` 不是推荐默认值。
5. `NO_PROXY` 不是 transport 的替代品，代码必须显式禁用环境代理。
6. 健康检查失败时按 `errors[].action` 修复。

P1 不添加 token 认证，因为服务只绑定 loopback。任何未来非 loopback 暴露都必须重新设计认证和访问控制。

## 12. 测试设计

### 12.1 根项目测试

`tests/python/test_cloud_gpu_local.py` 使用 fake Session 和环境隔离，至少覆盖：

- 四个工具键、已设置/未设置 URL、未登记工具、未知 provider 提示
- RunPod/Modal 原配置行为不变
- payload 解包、请求 JSON、无 Authorization、`trust_env=False`
- `(5, timeout + 30)`、Session 关闭、POST 只调用一次
- 成功 tuple 与 complete 事件
- 缺 URL、非 loopback、连接失败、timeout、非 200、非法 JSON、非 dict JSON
- HTTP 200 应用层 error 返回错误且没有 complete/success
- 非 200 的 `error`、`detail`、纯文本和 500 字符截断

不得 requests 到 `127.0.0.1` 或任何外部地址。

### 12.2 网关测试

`test_config.py`：

- 默认 base URL、末尾斜杠、环境变量优先于 `.env`
- 非 HTTP、非 loopback、凭据、query、fragment、非根路径拒绝
- 缺/相对/不存在模型根目录产生配置错误而非 import 崩溃

`test_comfyui.py`：

- 各 HTTP 方法的路径、JSON、query、multipart 和 timeout
- Session 关闭、POST 不重试、缺失 prompt id、HTTP/JSON 错误
- wait 的未出现、成功、执行失败、非法参数和 deadline

`test_health.py`：

- 使用临时目录建立完整/缺失模型树，使用 fake client 提供元数据
- 文件、目录、节点、loader value、TTS model_size 和 optional VAE 状态检查
- GPU/RAM 白名单，不返回 argv
- 全量 ready、任一模型失败、ComfyUI 不可达、配置错误、未预期异常
- FastAPI TestClient 验证 200/503 与结构化 errors
- P1 四路由均 `implemented=false`、`available=false`

网关测试不得读取开发者真实 `.env`、真实模型目录或真实 ComfyUI。根测试在导入 `cloud_gpu` 后必须隔离并替换相关 provider 环境变量，不能让本机配置决定断言结果。

### 12.3 CI 与统一回归

`python-tests.yml` 的 push/pull_request paths 加 `local-gateway/**`，并增加第二个测试步骤。`lint-python.yml` 的 ruff 目标加 `local-gateway/`。

```bash
set -euo pipefail
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py docs/local-comfyui-provider-p0-p1-design.md local-gateway/README.md
```

路线图中的统一回归同步加入网关测试命令。

## 13. P1 真机验收

前置条件：

- 当前 ComfyUI 0.45.0 已在 `127.0.0.1:9188` 提供 `/system_stats` 和 `/object_info`。
- 首发模型文件已逐项确认存在，但实施时仍以 `/health` 新结果为准。
- 网关 `.env` 使用实际绝对模型根目录，ComfyUI 与网关分别在终端运行。

执行：

```bash
set -euo pipefail
mkdir -p scratch/local-comfyui/p1
curl --noproxy '*' --fail-with-body \
  http://127.0.0.1:8123/health > scratch/local-comfyui/p1/health.json

uv run python -I -c '
import json
p = "scratch/local-comfyui/p1/health.json"
d = json.load(open(p, encoding="utf-8"))
assert d["ready"] is True
assert d["models_ready"] is True
assert d["comfyui"]["reachable"] is True
assert d["comfyui"]["gpu"] is not None
assert set(d["models"]) == {"qwen3-tts-1.7B", "qwen-image-2.1", "minimax-h3-fl2va"}
assert all(m["ready"] for m in d["models"].values())
assert set(d["routes"]) == {"tts", "image_generate", "image_edit", "video"}
assert all(not r["implemented"] and not r["available"] for r in d["routes"].values())
assert d["errors"] == []
assert "argv" not in d["comfyui"]
'
```

随后运行第 12.3 节全部回归。缺模型、缺节点和不可达的 503 由 TestClient 验收，不通过移动真实权重或停止 ComfyUI 制造故障。P1 不调用真实 `/prompt`。

## 14. 交付与提交边界

推荐提交顺序：

1. `DOCS:` P0 首 commit，包含 plan、路线图、本文和 implementation plan。
2. `FEAT:` ccvt local transport，包含 `cloud_gpu.py` 与根测试。
3. `FEAT:` gateway walking skeleton，包含网关、网关测试、启动说明和 CI。

P1 收尾顺序固定为：真机 `/health` 验收 → 双测试域 → ruff → copy lint → 更新 P0/P1 checkbox → 检查 `git status` → 提交剩余改动。

不得把以下情况记为完成：

- 只有单元测试通过，真机 `/health` 未运行
- `/health` 返回 200 但生成路由被误标为 implemented
- 只运行根测试，没有运行网关测试
- fork/branch 已建立但首 commit 缺 plan 或路线图
- 把本地 `.env`、真实模型路径配置或生成产物提交到仓库

## 15. 后续接口约束

P2-P4 必须复用本规格的 `ComfyUIClient`、模型注册表和路由 implemented/available 语义，不得各自新建一套 HTTP 客户端。每个生成路由自行设置总 deadline，并满足：

```text
网关内部 deadline < ccvt CLI/HTTP timeout < video_job 外层 timeout
```

P2 开始时才能新增 TTS workflow 与生成端点，并将其路由 implemented 改为 true。图像和视频同理。P5 才处理显式价格、`verify_setup --local` 和 toolkit registry。
