# Local ComfyUI Provider (P0/P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the P0 fork workspace and the P1 walking skeleton: a `local` provider in `tools/cloud_gpu.py` plus an independent loopback FastAPI gateway whose `GET /health` verifies ComfyUI connectivity, the three first-release model baselines and the four not-yet-implemented generation routes.

**Architecture:** The ccvt side grows a third provider (`local`) inside the existing `call_cloud_endpoint` dispatch, calling a synchronous loopback HTTP endpoint with the same `(result_dict, elapsed_seconds)` contract as Modal. The gateway is a separate uv project under `local-gateway/` with five modules: config (read/normalize/validate settings), registry (pure data: models, files, nodes, options, routes), comfyui (original HTTP client with poll termination semantics), health (aggregation returning `(payload, http_status)`), server (app factory and `GET /health` only). Tests run in two domains: root `tests/python` and `local-gateway/tests`, each inside its own uv environment, all network interactions faked.

**Tech Stack:** Python >=3.10, uv (no pytest anywhere, plain `unittest`), FastAPI + uvicorn + requests + python-dotenv (gateway runtime), httpx (gateway dev group only, for `fastapi.testclient`), requests (root project, already present), Git Bash on Windows, GitHub CLI (`gh`) for the fork.

**Spec:** `docs/local-comfyui-provider-p0-p1-design.md` (the approved P0/P1 design spec; authoritative for every interface named below). Companion documents: `docs/local-comfyui-provider-roadmap.md` (P0/P1 phases, appendix B file list) and `docs/local-comfyui-provider-plan.md` (historical technical plan). Executors read the spec alongside this plan; where wording could differ, the spec wins.

## Global Constraints

Copied verbatim from the spec; every task implicitly includes all of these.

- Gateway runtime dependencies: "FastAPI、uvicorn、requests、python-dotenv"; "`httpx` 只放在 dev dependency group 供 TestClient 使用". Gateway `pyproject.toml` requires Python `">=3.10"`. "`uv.lock` 必须提交".
- "不得引入 torch、ComfyUI Python 包或 OpenMontage 代码" (original client code only; no third-party example workflow JSON).
- "根项目测试和网关测试分别在各自 uv 环境运行，不互相引入运行时依赖" and the root project gains no FastAPI/uvicorn/httpx runtime dependency; the gateway is not part of the root uv workspace.
- All network unit tests use fakes: "所有网络单测均使用 fake，不访问真实 ComfyUI，不提交真实 `/prompt`，不生成媒体" and gateway tests "不得读取开发者真实 `.env`、真实模型目录或真实 ComfyUI"; root tests must isolate the provider environment variables after importing `cloud_gpu`.
- Loopback only: gateway binds `127.0.0.1:8123`; base URL and local endpoints accept only http scheme and loopback hosts `127.0.0.1`、`localhost`、`::1`; no CORS, no token auth in P1.
- Every requests session on the gateway side and in `_call_local` sets `trust_env=False` ("`NO_PROXY` 不是 transport 的替代品，代码必须显式禁用环境代理").
- `_call_local`: single POST, no retry, timeout `(5, timeout + 30)`; missing URL returns `({"error": ...}, 0)`; unknown provider still raises `ValueError` at config stage with local listed; RunPod/Modal config keys and provider-specific error text unchanged.
- ComfyUI client: normal HTTP calls use `(2, 10)` timeouts, no automatic retries; `wait(prompt_id, deadline, poll_interval)` uses `time.monotonic()`; never calls `/interrupt`.
- Real-machine verification in this plan is limited to `GET /health` (curl). Never submit a real `/prompt`.
- Do not modify `qwen3_tts.py`, `voiceover.py`, `video_job.py`, `verify_setup.py`, root `.env.example`, or the toolkit registry (P2+ scope). Do not implement the four generation routes; all four stay `implemented=false`.
- Never commit `local-gateway/.env`, the root `.env`, real model paths or generated artifacts; acceptance output goes under `scratch/local-comfyui/` (gitignored).
- Commit message style: uppercase prefixes `DOCS:`, `FEAT:`, `FIX:`. The first `DOCS:` commit includes the plan, the roadmap, the design spec and this implementation plan.
- Commands run from the repository root in Git Bash unless a step says otherwise. This plan is written while `main` is checked out; no fork/branch/commit happens until Task 1 executes.

## File Structure

One responsibility per file, matching spec section 5:

| File | Responsibility |
|---|---|
| `tools/cloud_gpu.py` (modify) | ccvt provider config + synchronous local transport (`_LOCAL_ENV_VARS`, `get_provider_config` local branch, `_call_local`) |
| `tests/python/test_cloud_gpu_local.py` (create) | Root-domain tests for local config and transport, fake sessions only |
| `local-gateway/pyproject.toml` (create) | Independent uv project: four runtime deps, httpx in dev group, no build system |
| `local-gateway/uv.lock` (generate + commit) | Lockfile for the gateway project |
| `local-gateway/.env.example` (create) | Documented `COMFYUI_BASE_URL` / `COMFYUI_MODELS_ROOT` template |
| `local-gateway/README.md` (create) | Start order, .env copying, loopback/security notes, error-following guidance |
| `local-gateway/start_gateway.bat` (create) | Start uvicorn from the script's own directory, loopback 8123 |
| `local-gateway/local_gateway/__init__.py` (create) | Package marker |
| `local-gateway/local_gateway/config.py` (create) | Read, normalize, validate gateway settings; no HTTP, no aggregation |
| `local-gateway/local_gateway/registry.py` (create) | Plain data: three models, required files/directories/nodes/options, route targets, implemented-flag; no filesystem or network access |
| `local-gateway/local_gateway/comfyui.py` (create) | Original ComfyUI HTTP client: `system_stats`, `object_info`, `submit`, `history`, `view`, `upload`, `wait`; `ComfyUIError` / `ComfyUITimeout` |
| `local-gateway/local_gateway/health.py` (create) | Aggregate config + ComfyUI metadata + model files into the health payload; returns `(payload, http_status)` |
| `local-gateway/local_gateway/server.py` (create) | `create_app(settings=None, client_factory=None)`, module-level `app`, `GET /health` |
| `local-gateway/tests/test_config.py` (create) | Settings loading/validation tests |
| `local-gateway/tests/test_comfyui.py` (create) | Client HTTP method tests and `wait` termination tests |
| `local-gateway/tests/test_health.py` (create) | Registry shape tests, health aggregation tests, FastAPI TestClient tests |
| `.github/workflows/python-tests.yml` (modify) | Add `local-gateway/**` to paths and a second gateway test step |
| `.github/workflows/lint-python.yml` (modify) | Add `local-gateway/` to the ruff targets |
| `docs/local-comfyui-provider-roadmap.md` (modify) | Sync the unified regression block (Task 10) and tick P0/P1 checkboxes (Task 11) |
| `docs/local-comfyui-provider-p0-p1-design.md` (modify, Task 1 only) | Three document-list edits so the spec also recognizes this implementation plan |
| `docs/local-comfyui-provider-plan.md` (modify, Task 1 only) | Five punctuation-only fixes for the copy lint dashes |
| Repo root `.env` (local edit, never committed) | Four `LOCAL_*` URLs (Task 11) |
| `local-gateway/.env` (local edit, never committed) | Real `COMFYUI_BASE_URL` / `COMFYUI_MODELS_ROOT` (Task 11) |

No other files change. No `.gitignore` change is needed: the existing `.env` and `.venv/` patterns already match at any depth (verified with the current root `.gitignore`).

## Execution conventions

- Repository root: `E:\GitHub\claude-code-video-toolkit`. Shell: Git Bash. Python invocations always go through uv.
- Root-domain tests: `uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v` (or a single file: `uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v`, the invocation pattern documented in `tests/python/test_modal_setup.py`).
- Gateway tests: `uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v` (or a single file with `-p 'test_config.py'` etc.).
- Never use pytest. Never let a test touch `127.0.0.1`, a real `.env`, or a real models directory; temp directories come from `tempfile.TemporaryDirectory()`.
- Tasks 2 through 10 run on the branch created in Task 1 (`feat/local-comfyui-provider`). Do not commit on `main`.

---

### Task 1: P0 repository preparation and first DOCS commit

**Files:**
- Modify: `docs/local-comfyui-provider-plan.md` (five punctuation-only replacements)
- Modify: `docs/local-comfyui-provider-p0-p1-design.md` (three document-list edits)
- Add to git (first commit): `docs/local-comfyui-provider-plan.md`, `docs/local-comfyui-provider-roadmap.md`, `docs/local-comfyui-provider-p0-p1-design.md`, `docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md`

**Interfaces:**
- Consumes: nothing (first task). The three `docs/local-comfyui-provider-*.md` files and this plan file currently sit untracked in the working tree of `main`.
- Produces: fork `kenyonxu/claude-code-video-toolkit` with remotes `origin` (fork) and `upstream` (`https://github.com/digitalsamba/claude-code-video-toolkit.git`); branch `feat/local-comfyui-provider`; one `DOCS:` commit containing all four documents. Tasks 2 and later depend on this branch existing.

No code in this task; it is the spec's section 4 (P0) executed exactly.

- [ ] **Step 1: Run the read-only preflight checks**

From the repository root:

```bash
gh auth status
git status --short --branch --untracked-files=all
git remote -v
git show-ref --verify --quiet refs/heads/feat/local-comfyui-provider; test $? -eq 1 && echo "branch absent: OK"
gh repo view kenyonxu/claude-code-video-toolkit --json name
```

Expected: `gh auth status` shows a logged-in account; `git status` shows `## main...origin/main` plus exactly four untracked paths (`docs/local-comfyui-provider-plan.md`, `docs/local-comfyui-provider-roadmap.md`, `docs/local-comfyui-provider-p0-p1-design.md`, `docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md`); both `origin` URLs point at `https://github.com/digitalsamba/claude-code-video-toolkit.git`; "branch absent: OK" prints. `gh repo view` must produce a repository-not-found response, proving the fork is absent. A successful `gh repo view` means the fork already exists, while an authentication, permission or network error is inconclusive: stop in all three cases. Never convert an arbitrary `gh` failure into "fork absent". If anything else appears, stop and report; do not try to overwrite anything.

- [ ] **Step 2: Create the fork and fix the remotes**

```bash
gh repo fork --remote
git remote -v
```

Make the final state exactly: `origin` fetch/push `https://github.com/kenyonxu/claude-code-video-toolkit.git` and `upstream` fetch/push `https://github.com/digitalsamba/claude-code-video-toolkit.git`. Run only the case that matches what `gh` produced:

```bash
# Case A: origin still points at digitalsamba and no upstream remote exists
git remote rename origin upstream
git remote add origin https://github.com/kenyonxu/claude-code-video-toolkit.git

# Case B: gh added a remote literally named "fork" and left the source on origin
git remote remove fork
git remote rename origin upstream
git remote add origin https://github.com/kenyonxu/claude-code-video-toolkit.git

# Case C: names are right but a URL is wrong
git remote set-url origin https://github.com/kenyonxu/claude-code-video-toolkit.git
git remote set-url upstream https://github.com/digitalsamba/claude-code-video-toolkit.git
```

If the actual remotes do not match A, B or C, stop and report them instead of guessing a remote name.

- [ ] **Step 3: Create the working branch**

```bash
git checkout -b feat/local-comfyui-provider
git branch --show-current
```

Expected output: `feat/local-comfyui-provider`. The four untracked documents carry over.

- [ ] **Step 4: Apply the five punctuation fixes to the historical plan doc**

`uv run tools/copy_lint.py` currently reports 9 dash findings in `docs/local-comfyui-provider-plan.md`. Apply exactly these five find/replace edits (punctuation only, no technical meaning changes) with your editor's literal-string replace:

```
find:    | 音乐 | 不变（默认 acemusic） | — |
replace: | 音乐 | 不变（默认 acemusic） | 不适用 |

find:    抛错 —— 两处是
replace: 抛错，两处是

find:    不抛异常）—— `_call_local` 直接克隆此流程
replace: 不抛异常），`_call_local` 直接克隆此流程

find:    会误报本地 URL —— local 检查块要自写
replace: 会误报本地 URL，local 检查块要自写

find:    `/health` —— 打通链路
replace: `/health`，打通链路
```

- [ ] **Step 5: Apply the three document-list edits to the design spec**

These make the approved spec acknowledge this implementation plan; no technical content changes.

Edit 1, in section 4.1, extend the untracked-documents list with a fourth bullet after the `docs/local-comfyui-provider-p0-p1-design.md` line:

```markdown
- `docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md`
```

Edit 2, in section 4.3, replace the copy lint command block with:

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

Edit 3, in section 14 item 1, replace:

```markdown
1. `DOCS:` P0 首 commit，包含 plan、路线图和本文。
```

with:

```markdown
1. `DOCS:` P0 首 commit，包含 plan、路线图、本文和 implementation plan。
```

- [ ] **Step 6: Run copy lint over all four documents**

```bash
uv run tools/copy_lint.py \
  docs/local-comfyui-provider-plan.md \
  docs/local-comfyui-provider-roadmap.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md
```

Expected: exit 0, no findings. If a finding appears, fix the flagged line in the offending document (punctuation or phrasing only) and rerun.

- [ ] **Step 7: Run the P0 acceptance block (spec 4.3)**

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
echo "P0 acceptance: OK"
```

Expected: `P0 acceptance: OK`. Do not tick the roadmap P0 checkboxes before the first commit exists; Step 9 records P0 immediately after that commit.

- [ ] **Step 8: Commit the four documents**

```bash
git add docs/local-comfyui-provider-plan.md \
  docs/local-comfyui-provider-roadmap.md \
  docs/local-comfyui-provider-p0-p1-design.md \
  docs/superpowers/plans/2026-11-17-local-comfyui-p0-p1.md
git status --short
git commit -m "DOCS: local ComfyUI provider plan, roadmap, P0/P1 design spec and implementation plan"
```

Expected: the commit contains exactly those four files. `git status --short` after the commit shows a clean tree.

- [ ] **Step 9: Mark P0 complete and fold the checkbox state into the first commit**

In `docs/local-comfyui-provider-roadmap.md`, replace only the four P0 checklist items with the following completed state. P1 through P5 remain untouched:

```markdown
- [x] `gh repo fork --remote`：origin 指向 `kenyonxu/claude-code-video-toolkit`，upstream 指向 `digitalsamba/claude-code-video-toolkit`。
- [x] `git checkout -b feat/local-comfyui-provider`。
- [x] 首个 `DOCS:` commit **同时包含 plan 与本文**。两份文件当前都未跟踪，只提交本文会留下断链。
- [x] 首 commit 前直接对两份文件跑 copy lint；plan 中既有的 dash 命中可在 P0 做纯标点清理，不改技术含义。
```

The third item's wording describes the original roadmap/plan pair; the implementation plan and this design spec are also present in the same commit as required by the expanded P0 spec. Amend only before any push:

```bash
git add docs/local-comfyui-provider-roadmap.md
git commit --amend --no-edit
git status --short
git log -1 --format='%s' --stat
```

Expected: the amended first commit still contains exactly four documents and P0 is checked. The working tree is clean. Task 11 will tick only P1.

---

### Task 2: cloud_gpu local provider configuration

**Files:**
- Modify: `tools/cloud_gpu.py` (module docstring; `_LOCAL_ENV_VARS` after `_MODAL_ENV_VARS`; `get_provider_config` local branch, docstring and unknown-provider message; `call_cloud_endpoint` docstring and its unknown-provider message)
- Test: `tests/python/test_cloud_gpu_local.py` (create)

**Interfaces:**
- Consumes: existing `tools/cloud_gpu.py` symbols `_RUNPOD_ENV_VARS`, `_MODAL_ENV_VARS`, `get_provider_config(provider, tool_name)`, `call_cloud_endpoint(...)`.
- Produces:
  - `cloud_gpu._LOCAL_ENV_VARS: dict[str, str]` with exactly `{"qwen3_tts": "LOCAL_TTS_ENDPOINT_URL", "qwen_image": "LOCAL_IMAGE_ENDPOINT_URL", "image_edit": "LOCAL_IMAGE_EDIT_ENDPOINT_URL", "h3_video": "LOCAL_VIDEO_ENDPOINT_URL"}`
  - `cloud_gpu.get_provider_config("local", tool_name) -> {"endpoint_url": str | None}` (None when the env var is unset or the tool is not registered)
  - Unknown-provider `ValueError` messages now list `'runpod', 'modal' or 'local'` in both `get_provider_config` and `call_cloud_endpoint`.

- [ ] **Step 1: Write the failing test file**

Create `tests/python/test_cloud_gpu_local.py`:

```python
"""local provider in tools/cloud_gpu.py: config lookup and transport.

    uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v

Every network interaction is faked. Nothing here contacts 127.0.0.1 or any
other host, and no assertion depends on this machine's .env: the four
LOCAL_* variables are isolated per test.
"""
from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import cloud_gpu as cg  # noqa: E402

LOCAL_VARS = (
    "LOCAL_TTS_ENDPOINT_URL",
    "LOCAL_IMAGE_ENDPOINT_URL",
    "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
    "LOCAL_VIDEO_ENDPOINT_URL",
)


@contextmanager
def local_env(**values):
    """Snapshot, clear and optionally set the four LOCAL_* vars."""
    saved = {k: os.environ.get(k) for k in LOCAL_VARS}
    for k in LOCAL_VARS:
        os.environ.pop(k, None)
    for k, v in values.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ProviderConfigTests(unittest.TestCase):
    def test_local_env_var_mapping_is_the_four_capability_urls(self):
        self.assertEqual(cg._LOCAL_ENV_VARS, {
            "qwen3_tts": "LOCAL_TTS_ENDPOINT_URL",
            "qwen_image": "LOCAL_IMAGE_ENDPOINT_URL",
            "image_edit": "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
            "h3_video": "LOCAL_VIDEO_ENDPOINT_URL",
        })

    def test_local_config_returns_endpoint_url_for_each_tool(self):
        for tool, var in cg._LOCAL_ENV_VARS.items():
            with local_env(**{var: "http://127.0.0.1:8123/x"}):
                self.assertEqual(cg.get_provider_config("local", tool),
                                 {"endpoint_url": "http://127.0.0.1:8123/x"})

    def test_local_config_is_none_when_env_missing(self):
        with local_env():
            self.assertEqual(cg.get_provider_config("local", "qwen3_tts"),
                             {"endpoint_url": None})

    def test_local_config_is_none_for_unregistered_tool(self):
        with local_env(**dict.fromkeys(LOCAL_VARS, "http://127.0.0.1:8123/x")):
            self.assertEqual(cg.get_provider_config("local", "flux2"),
                             {"endpoint_url": None})

    def test_unknown_provider_error_lists_local(self):
        with self.assertRaises(ValueError) as ctx:
            cg.get_provider_config("gcp", "qwen3_tts")
        self.assertIn("local", str(ctx.exception))

    def test_call_cloud_endpoint_unknown_provider_error_lists_local(self):
        with self.assertRaises(ValueError) as ctx:
            cg.call_cloud_endpoint("gcp", {}, "qwen3_tts", verbose=False)
        self.assertIn("local", str(ctx.exception))

    def test_runpod_and_modal_config_unchanged(self):
        env = {"RUNPOD_API_KEY": "rk", "RUNPOD_QWEN3_TTS_ENDPOINT_ID": "rid",
               "MODAL_QWEN3_TTS_ENDPOINT_URL": "http://m.example", "MODAL_TOKEN_ID": "tid",
               "MODAL_TOKEN_SECRET": "tsec"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cg.get_provider_config("runpod", "qwen3_tts"),
                             {"api_key": "rk", "endpoint_id": "rid"})
            self.assertEqual(cg.get_provider_config("modal", "qwen3_tts"),
                             {"endpoint_url": "http://m.example",
                              "token_id": "tid", "token_secret": "tsec"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v
```

Expected: FAIL/ERROR with `AttributeError: module 'cloud_gpu' has no attribute '_LOCAL_ENV_VARS'`.

- [ ] **Step 3: Implement the config surface**

In `tools/cloud_gpu.py`:

3a. Replace the module docstring (lines 1-10) with:

```python
"""Shared cloud GPU provider abstraction.

Routes job submission to RunPod, Modal or local endpoints with a unified interface.
Each tool builds its own payload dict, then calls call_cloud_endpoint() which
handles submission, polling, timeout, and cancellation for the chosen provider.

Supported providers:
- runpod: RunPod serverless endpoints (existing)
- modal: Modal web endpoints (existing)
- local: loopback local-gateway endpoints backed by local ComfyUI (new)
"""
```

3b. Directly after the `_MODAL_ENV_VARS = {...}` block, add:

```python
_LOCAL_ENV_VARS = {
    "qwen3_tts": "LOCAL_TTS_ENDPOINT_URL",
    "qwen_image": "LOCAL_IMAGE_ENDPOINT_URL",
    "image_edit": "LOCAL_IMAGE_EDIT_ENDPOINT_URL",
    "h3_video": "LOCAL_VIDEO_ENDPOINT_URL",
}
```

3c. Replace the body of `get_provider_config` with:

```python
def get_provider_config(provider: str, tool_name: str) -> dict:
    """Get configuration for a provider + tool combination.

    Returns dict with provider-specific keys:
    - RunPod: {"api_key": str, "endpoint_id": str}
    - Modal: {"endpoint_url": str, "token_id": str, "token_secret": str}
    - Local: {"endpoint_url": str | None}. The URL comes from the tool's
      LOCAL_*_ENDPOINT_URL env var; None means the var is unset or the tool
      is not registered for local, which _call_local turns into an
      actionable error dict.
    """
    if provider == "runpod":
        env_var = _RUNPOD_ENV_VARS.get(tool_name)
        return {
            "api_key": os.getenv("RUNPOD_API_KEY"),
            "endpoint_id": os.getenv(env_var) if env_var else None,
        }
    elif provider == "modal":
        env_var = _MODAL_ENV_VARS.get(tool_name)
        return {
            "endpoint_url": os.getenv(env_var) if env_var else None,
            "token_id": os.getenv("MODAL_TOKEN_ID"),
            "token_secret": os.getenv("MODAL_TOKEN_SECRET"),
        }
    elif provider == "local":
        env_var = _LOCAL_ENV_VARS.get(tool_name)
        return {
            "endpoint_url": os.getenv(env_var) if env_var else None,
        }
    else:
        raise ValueError(f"Unknown provider: {provider}. Use 'runpod', 'modal' or 'local'.")
```

3d. In `call_cloud_endpoint`, update the `Args:` docstring line for `provider` to:

```python
        provider: "runpod", "modal" or "local" (local posts once to the
                   tool's LOCAL_* endpoint on the loopback gateway)
```

and change the trailing dispatch `else` to:

```python
    else:
        raise ValueError(f"Unknown provider: {provider}. Use 'runpod', 'modal' or 'local'.")
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v
```

Expected: 7 tests, OK.

- [ ] **Step 5: Run the full root test suite to prove nothing regressed**

```bash
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
```

Expected: all tests OK.

- [ ] **Step 6: Commit**

```bash
git add tools/cloud_gpu.py tests/python/test_cloud_gpu_local.py
git commit -m "FEAT: cloud_gpu local provider config mapping and unknown-provider hint"
```

---

### Task 3: cloud_gpu local transport (`_call_local`) and dispatch

**Files:**
- Modify: `tools/cloud_gpu.py` (add `from urllib.parse import urlsplit` to the imports; add the local gateway section after the Modal section; add the `local` dispatch branch in `call_cloud_endpoint`)
- Test: `tests/python/test_cloud_gpu_local.py` (append the transport test classes and helpers)

**Interfaces:**
- Consumes: Task 2's `_LOCAL_ENV_VARS` and `get_provider_config("local", tool_name) -> {"endpoint_url": ...}`; existing `ProgressReporter` and `contextmanager`.
- Produces: `cloud_gpu._call_local(payload: dict, endpoint_url: str | None, tool_name: str, timeout: int = 600, progress_label: str = "Processing", progress: ProgressReporter | None = None) -> tuple[dict, float]`, wired into `call_cloud_endpoint(provider="local", ...)`. Tuple contract: success returns `(result_dict, elapsed_seconds)` after a `complete` progress event; every failure returns `({"error": message}, elapsed)` with `elapsed == 0` for pre-request rejections; application-level errors emit an `error` event and never `complete`; no transport exception escapes.

- [ ] **Step 1: Append the failing transport tests**

Append to `tests/python/test_cloud_gpu_local.py`, above the `if __name__ == "__main__":` block (also add `import requests` to the imports at the top):

```python
import requests  # noqa: E402  (exceptions for the fake failures)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


class FakeSession:
    """Stands in for the requests.Session created inside _call_local."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.post_calls = []
        self.trust_env = True
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        self.closed = True

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeProgress:
    def __init__(self):
        self.events = []

    def event(self, stage, msg, pct=None, level="dim"):
        self.events.append(stage)

    @contextmanager
    def heartbeat(self, stage="waiting", msg_template=""):
        self.events.append("heartbeat")
        yield


def run_local(session, endpoint_url, tool_name="qwen3_tts", payload=None,
              timeout=600, progress=None):
    with mock.patch.object(cg.requests, "Session", return_value=session):
        return cg._call_local(
            payload=payload if payload is not None else {"input": {"text": "hi"}},
            endpoint_url=endpoint_url,
            tool_name=tool_name,
            timeout=timeout,
            progress_label="Testing",
            progress=progress,
        )


class LocalTransportTests(unittest.TestCase):
    url = "http://127.0.0.1:8123/tts/generate"

    def test_success_returns_tuple_and_complete_event(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True, "audio_base64": "AA"}))
        progress = FakeProgress()
        result, elapsed = run_local(session, self.url, payload={"input": {"text": "hi"}},
                                    progress=progress)
        self.assertEqual(result, {"success": True, "audio_base64": "AA"})
        self.assertGreaterEqual(elapsed, 0)
        self.assertIn("complete", progress.events)

    def test_payload_input_key_is_unwrapped_into_the_json_body(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url, payload={"input": {"text": "hi"}})
        sent_url, kwargs = session.post_calls[0]
        self.assertEqual(sent_url, self.url)
        self.assertEqual(kwargs["json"], {"text": "hi"})

    def test_payload_without_input_is_sent_as_is(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url, payload={"text": "hi"})
        self.assertEqual(session.post_calls[0][1]["json"], {"text": "hi"})

    def test_headers_content_type_only_trust_env_false_session_closed(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url)
        kwargs = session.post_calls[0][1]
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/json"})
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(session.trust_env)
        self.assertTrue(session.closed)

    def test_timeout_is_5_and_timeout_plus_30_with_a_single_post(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        run_local(session, self.url, timeout=600)
        kwargs = session.post_calls[0][1]
        self.assertEqual(kwargs["timeout"], (5, 630))
        self.assertEqual(len(session.post_calls), 1)

    def test_missing_url_error_names_tool_and_env_var_before_any_request(self):
        session = FakeSession(response=FakeResponse(json_data={}))
        result, elapsed = run_local(session, None, tool_name="qwen3_tts")
        self.assertIn("qwen3_tts", result["error"])
        self.assertIn("LOCAL_TTS_ENDPOINT_URL", result["error"])
        self.assertEqual(elapsed, 0)
        self.assertEqual(session.post_calls, [])

    def test_unregistered_tool_error_lists_all_local_env_vars(self):
        result, _ = run_local(FakeSession(), None, tool_name="flux2")
        self.assertIn("flux2", result["error"])
        for var in LOCAL_VARS:
            self.assertIn(var, result["error"])

    def test_non_loopback_endpoint_rejected_before_request(self):
        session = FakeSession(response=FakeResponse(json_data={}))
        result, elapsed = run_local(session, "http://192.168.1.9:8123/tts/generate")
        self.assertIn("loopback", result["error"])
        self.assertEqual(elapsed, 0)
        self.assertEqual(session.post_calls, [])

    def test_endpoint_validation_rejects_each_violation(self):
        cases = [
            "https://127.0.0.1:8123/tts/generate",   # not http
            "http://user:pass@127.0.0.1:8123/tts/generate",  # credentials
            "http://127.0.0.1:8123/tts/generate?x=1",  # query
            "http://127.0.0.1:8123/tts/generate#frag",  # fragment
            "http://127.0.0.1:8123/",                # root path
            "ftp://127.0.0.1:8123/tts/generate",     # not http
            "http://127.0.0.1:bad/tts/generate",     # invalid port
        ]
        for url in cases:
            with self.subTest(url=url):
                session = FakeSession(response=FakeResponse(json_data={}))
                result, elapsed = run_local(session, url)
                self.assertIn("error", result)
                self.assertEqual(elapsed, 0)
                self.assertEqual(session.post_calls, [])

    def test_connection_failure_mentions_comfyui_and_start_script(self):
        session = FakeSession(exc=requests.exceptions.ConnectionError("connection refused"))
        result, _ = run_local(session, self.url)
        self.assertIn("start_gateway.bat", result["error"])
        self.assertIn("ComfyUI", result["error"])

    def test_timeout_returns_error_dict_not_exception(self):
        session = FakeSession(exc=requests.exceptions.ReadTimeout("read timed out"))
        result, _ = run_local(session, self.url)
        self.assertIn("timed out", result["error"])

    def test_non_200_prefers_json_error_field(self):
        body = FakeResponse(status_code=422, json_data={"error": "bad input"},
                            text='{"error": "bad input"}')
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("422", result["error"])
        self.assertIn("bad input", result["error"])

    def test_non_200_prefers_json_detail_field(self):
        body = FakeResponse(status_code=422, json_data={"detail": "field required"},
                            text='{"detail": "field required"}')
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("field required", result["error"])

    def test_non_200_plain_text_is_truncated_to_500_chars(self):
        body = FakeResponse(status_code=500, text="x" * 2000)
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("x" * 500, result["error"])
        self.assertNotIn("x" * 501, result["error"])

    def test_non_json_response_returns_error(self):
        body = FakeResponse(status_code=200, text="<html>oops</html>", json_error=True)
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("error", result)

    def test_non_dict_json_returns_error(self):
        body = FakeResponse(status_code=200, json_data=[1, 2, 3], text="[1, 2, 3]")
        result, _ = run_local(FakeSession(response=body), self.url)
        self.assertIn("error", result)

    def test_application_error_emits_error_event_and_no_complete(self):
        body = FakeResponse(status_code=200, json_data={"error": "comfyui node failed"})
        progress = FakeProgress()
        result, _ = run_local(FakeSession(response=body), self.url, progress=progress)
        self.assertEqual(result, {"error": "comfyui node failed"})
        self.assertIn("error", progress.events)
        self.assertNotIn("complete", progress.events)


class LocalDispatchTests(unittest.TestCase):
    tts_url = "http://127.0.0.1:8123/tts/generate"

    def test_call_cloud_endpoint_dispatches_local_end_to_end(self):
        session = FakeSession(response=FakeResponse(json_data={"success": True}))
        progress = FakeProgress()
        with local_env(LOCAL_TTS_ENDPOINT_URL=self.tts_url):
            with mock.patch.object(cg.requests, "Session", return_value=session):
                result, _ = cg.call_cloud_endpoint(
                    "local", {"input": {"text": "hi"}}, "qwen3_tts",
                    timeout=100, progress=progress, verbose=False)
        self.assertEqual(result, {"success": True})
        sent_url, kwargs = session.post_calls[0]
        self.assertEqual(sent_url, self.tts_url)
        self.assertEqual(kwargs["json"], {"text": "hi"})
        self.assertEqual(kwargs["timeout"], (5, 130))
        self.assertIn("complete", progress.events)

    def test_image_generate_and_edit_select_their_own_env_vars(self):
        cases = [
            ("qwen_image", "LOCAL_IMAGE_ENDPOINT_URL", "/image/generate"),
            ("image_edit", "LOCAL_IMAGE_EDIT_ENDPOINT_URL", "/image/edit"),
        ]
        for tool, var, path in cases:
            with self.subTest(tool=tool):
                url = f"http://127.0.0.1:8123{path}"
                session = FakeSession(response=FakeResponse(json_data={"success": True}))
                with local_env(**{var: url}):
                    with mock.patch.object(cg.requests, "Session", return_value=session):
                        cg.call_cloud_endpoint("local", {"input": {}}, tool, verbose=False)
                self.assertEqual(session.post_calls[0][0], url)

    def test_missing_url_through_dispatch_returns_actionable_error(self):
        with local_env():
            result, elapsed = cg.call_cloud_endpoint(
                "local", {"input": {}}, "h3_video", verbose=False)
        self.assertIn("LOCAL_VIDEO_ENDPOINT_URL", result["error"])
        self.assertEqual(elapsed, 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v
```

Expected: the Task 2 tests pass; the new tests ERROR with `AttributeError: module 'cloud_gpu' has no attribute '_call_local'`.

- [ ] **Step 3: Implement the transport**

In `tools/cloud_gpu.py`:

3a. Add to the stdlib imports (after `import time`):

```python
from urllib.parse import urlsplit
```

3b. In `call_cloud_endpoint`, add the local branch after the `modal` branch and before the final `else`:

```python
    elif provider == "local":
        result, elapsed = _call_local(
            payload=payload,
            endpoint_url=config["endpoint_url"],
            tool_name=tool_name,
            timeout=timeout,
            progress_label=progress_label,
            progress=progress,
        )
```

3c. Append a new section at the end of the file:

```python
# ---------------------------------------------------------------------------
# Local gateway implementation
# ---------------------------------------------------------------------------

# The local gateway is loopback-only, so a connection failure almost always
# means "not started yet": point at the two start steps instead of Modal hints.
_LOCAL_GATEWAY_HINT = (
    " Is the local gateway running? Start ComfyUI (127.0.0.1:9188) first,"
    " then run local-gateway/start_gateway.bat."
)

_LOCAL_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _validate_local_endpoint(endpoint_url: str) -> str | None:
    """Return an error message when the URL breaks the local transport rules.

    Unlike the gateway's COMFYUI_BASE_URL (root path only), a local endpoint
    must carry a non-root path such as /tts/generate, because the four
    capability endpoints live below the gateway root.
    """
    parts = urlsplit(endpoint_url)
    if parts.scheme != "http":
        return f"Local endpoint URL must use http, got {endpoint_url!r}."
    try:
        parts.port
    except ValueError:
        return f"Local endpoint URL has an invalid port, got {endpoint_url!r}."
    host = parts.hostname
    if host is None or host.lower() not in _LOCAL_LOOPBACK_HOSTS:
        return (f"Local endpoint URL must point at a loopback host"
                f" (127.0.0.1, localhost or ::1), got {endpoint_url!r}.")
    if parts.username or parts.password:
        return f"Local endpoint URL must not embed credentials, got {endpoint_url!r}."
    if parts.query or parts.fragment:
        return f"Local endpoint URL must not contain a query or fragment, got {endpoint_url!r}."
    if parts.path in ("", "/"):
        return (f"Local endpoint URL must include a non-root path such as"
                f" /tts/generate, got {endpoint_url!r}.")
    return None


def _extract_error_body(response) -> str:
    """Prefer a JSON error/detail field, else the raw text, for non-200 bodies."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(body, dict):
        if body.get("error"):
            return str(body["error"])
        if body.get("detail"):
            return str(body["detail"])
    return response.text.strip()


def _call_local(
    payload: dict,
    endpoint_url: str | None,
    tool_name: str,
    timeout: int = 600,
    progress_label: str = "Processing",
    progress: ProgressReporter | None = None,
) -> tuple[dict, float]:
    """POST one synchronous request to the local gateway.

    Mirrors _call_modal's synchronous shape with local rules: loopback HTTP
    only, no credentials, one requests.Session per call with trust_env=False,
    a single POST (no retry) and a (5, timeout + 30) connect/read timeout.
    The read timeout is not a total deadline for the ComfyUI job behind the
    gateway: when this client gives up, the gateway-side task keeps running;
    this call never cancels it.
    """
    def _emit(stage, msg, level="dim", pct=None):
        if progress:
            progress.event(stage, msg, pct=pct, level=level)

    if not endpoint_url:
        env_var = _LOCAL_ENV_VARS.get(tool_name)
        if env_var:
            return {"error": (
                f"Local endpoint URL not set for {tool_name}."
                f" Set {env_var} in .env (e.g. http://127.0.0.1:8123/tts/generate)."
            )}, 0
        return {"error": (
            f"Tool {tool_name!r} is not registered for the local provider."
            f" Registered tools and their env vars: {dict(_LOCAL_ENV_VARS)}."
        )}, 0

    url_problem = _validate_local_endpoint(endpoint_url)
    if url_problem:
        return {"error": url_problem}, 0

    # RunPod wraps payloads in {"input": ...}; the gateway, like Modal, wants
    # the inner dict. Unwrap when the caller used the RunPod format.
    local_payload = payload.get("input", payload) if isinstance(payload, dict) else payload
    headers = {"Content-Type": "application/json"}

    start = time.time()
    _emit("submit", f"{progress_label} via local gateway...", level="info")

    try:
        heartbeat_ctx = (progress.heartbeat(
            "waiting", f"{progress_label} via local gateway... ({{elapsed:.0f}}s)"
        ) if progress else contextmanager(lambda: (yield))())
        with requests.Session() as session:
            session.trust_env = False
            with heartbeat_ctx:
                response = session.post(
                    endpoint_url,
                    json=local_payload,
                    headers=headers,
                    timeout=(5, timeout + 30),
                )
        elapsed = time.time() - start
    except requests.exceptions.Timeout:
        elapsed = time.time() - start
        msg = (f"Local gateway request timed out after {elapsed:.0f}s."
               f" The ComfyUI job was not cancelled and may still finish.{_LOCAL_GATEWAY_HINT}")
        _emit("error", msg, level="error")
        return {"error": msg}, elapsed
    except requests.exceptions.RequestException as e:
        elapsed = time.time() - start
        msg = f"Local gateway connection failed: {e}.{_LOCAL_GATEWAY_HINT}"
        _emit("error", msg, level="error")
        return {"error": msg}, elapsed
    except Exception as e:
        elapsed = time.time() - start
        _emit("error", f"Local gateway request failed: {e}", level="error")
        return {"error": f"Local gateway request failed: {e}"}, elapsed

    if response.status_code != 200:
        message = _extract_error_body(response)[:500]
        msg = f"Local gateway HTTP {response.status_code}: {message}"
        _emit("error", msg, level="error")
        return {"error": msg}, elapsed

    try:
        result = response.json()
    except ValueError:
        msg = f"Local gateway returned a non-JSON body: {response.text[:500]}"
        _emit("error", msg, level="error")
        return {"error": msg}, elapsed
    if not isinstance(result, dict):
        msg = f"Local gateway returned JSON that is not an object: {response.text[:500]}"
        _emit("error", msg, level="error")
        return {"error": msg}, elapsed
    if result.get("error"):
        _emit("error", f"Local gateway error: {result['error']}", level="error")
        return {"error": str(result["error"])}, elapsed

    _emit("complete", f"Completed in {elapsed:.1f}s", pct=100, level="success")
    return result, elapsed
```

- [ ] **Step 4: Run the transport tests to verify they pass**

```bash
uv run --extra youtube python -m unittest tests/python/test_cloud_gpu_local.py -v
```

Expected: all 27 tests OK (7 config + 20 transport/dispatch).

- [ ] **Step 5: Run the full root suite**

```bash
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
```

Expected: all OK. `_estimate_cost` is untouched; local returns no cost event because no "local" GPU rate exists.

- [ ] **Step 6: Commit**

```bash
git add tools/cloud_gpu.py tests/python/test_cloud_gpu_local.py
git commit -m "FEAT: cloud_gpu _call_local loopback transport with (5, timeout+30) and no retry"
```

---

### Task 4: local-gateway project scaffold and config module

**Files:**
- Create: `local-gateway/pyproject.toml`, `local-gateway/.env.example`, `local-gateway/local_gateway/__init__.py`, `local-gateway/local_gateway/config.py`
- Create: `local-gateway/tests/test_config.py`
- Generate (then commit): `local-gateway/uv.lock`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent project; python-dotenv is a gateway dependency, not the root one).
- Produces:
  - `local_gateway.config.load_settings(env: Mapping[str, str] | None = None, dotenv_path: Path | None = None) -> GatewaySettings`. When `env` is None it loads `local-gateway/.env` (or the given `dotenv_path`) with `load_dotenv(..., override=False)` and reads `os.environ`; tests always pass an explicit `env` or a temp `dotenv_path`, so the developer's real `.env` is never read.
  - `GatewaySettings(base_url: str, models_root: str | None, base_url_valid: bool, models_root_valid: bool, errors: tuple[SettingsError, ...])` with property `config_valid -> bool` (True iff `errors` is empty).
  - `SettingsError(code: str, message: str, action: str)` with `code` in `{"config_missing", "config_invalid"}`.
  - Config rules (spec 6.1): `COMFYUI_BASE_URL` default `http://127.0.0.1:9188`, trailing `/` stripped, http scheme, loopback host (`127.0.0.1`/`localhost`/`::1`), optional port, no credentials/query/fragment, root path only; `COMFYUI_MODELS_ROOT` no default, `~` expanded, must be an absolute existing directory. Bad config never blocks app startup.

- [ ] **Step 1: Create the project scaffold**

`local-gateway/pyproject.toml` (no build system on purpose, same convention as the root project; uv treats it as a virtual project and imports resolve from the directory):

```toml
# ccvt local gateway: loopback FastAPI service in front of a local ComfyUI.
#
# Independent uv project on purpose: the root toolkit must not carry
# FastAPI/uvicorn or any gateway dependency, and the gateway must not pull in
# torch. Run everything through:
#
#   uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
#   uv run --directory local-gateway uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123

[project]
name = "ccvt-local-gateway"
version = "0.1.0"
description = "Loopback FastAPI gateway in front of a local ComfyUI install"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "requests>=2.28",
    "python-dotenv>=1.0",
]

[dependency-groups]
# fastapi.testclient.TestClient needs httpx; the gateway runtime never does.
dev = ["httpx>=0.27"]
```

`local-gateway/.env.example` (content from spec 6.1, verbatim):

```ini
COMFYUI_BASE_URL=http://127.0.0.1:9188
COMFYUI_MODELS_ROOT=E:/path/to/ComfyUI/models
```

`local-gateway/local_gateway/__init__.py`:

```python
"""ccvt local gateway: a loopback FastAPI service in front of local ComfyUI."""
```

Create an empty `local-gateway/tests/` directory (no `__init__.py`; unittest discovery imports these as top-level modules).

- [ ] **Step 2: Generate the lockfile and verify the environment**

```bash
uv lock --directory local-gateway
uv run --directory local-gateway python -c "import fastapi, uvicorn, requests, dotenv; print('gateway deps ok')"
git check-ignore local-gateway/.env local-gateway/.venv
```

Expected: lock resolves; `gateway deps ok` prints; `git check-ignore` lists both paths (the root `.gitignore` `.env` and `.venv/` patterns match at any depth).

- [ ] **Step 3: Write the failing config tests**

Create `local-gateway/tests/test_config.py`:

```python
"""local_gateway.config: read, normalize and validate gateway settings.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_config.py' -v

No test touches the developer's real .env (explicit env mappings or a temp
dotenv path only), no real models directory (temp dirs only), no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import config  # noqa: E402


class BaseUrlTests(unittest.TestCase):
    def test_defaults_when_env_is_empty(self):
        s = config.load_settings(env={})
        self.assertEqual(s.base_url, "http://127.0.0.1:9188")
        self.assertTrue(s.base_url_valid)
        self.assertIsNone(s.models_root)
        self.assertFalse(s.models_root_valid)
        self.assertFalse(s.config_valid)
        self.assertEqual([e.code for e in s.errors], ["config_missing"])

    def test_trailing_slash_is_stripped(self):
        s = config.load_settings(env={"COMFYUI_BASE_URL": "http://127.0.0.1:9188/"})
        self.assertEqual(s.base_url, "http://127.0.0.1:9188")

    def test_loopback_hosts_are_accepted(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                s = config.load_settings(env={"COMFYUI_BASE_URL": f"http://{host}:9188"})
                self.assertTrue(s.base_url_valid)

    def test_rejected_base_urls(self):
        bad = [
            "https://127.0.0.1:9188",          # not http
            "http://10.1.2.3:9188",            # not loopback
            "http://example.com",              # not loopback
            "http://user:pw@127.0.0.1:9188",   # credentials
            "http://127.0.0.1:9188?x=1",       # query
            "http://127.0.0.1:9188#frag",      # fragment
            "http://127.0.0.1:9188/comfy",     # non-root path
            "127.0.0.1:9188",                  # no scheme
            "http://127.0.0.1:bad",            # invalid port
        ]
        for url in bad:
            with self.subTest(url=url):
                s = config.load_settings(env={"COMFYUI_BASE_URL": url})
                self.assertFalse(s.base_url_valid)
                self.assertIn("config_invalid", [e.code for e in s.errors])

    def test_process_env_wins_over_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("COMFYUI_BASE_URL=http://127.0.0.1:9199\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COMFYUI_BASE_URL": "http://127.0.0.1:9200"}):
                s = config.load_settings(dotenv_path=dotenv)
        self.assertEqual(s.base_url, "http://127.0.0.1:9200")

    def test_dotenv_is_used_when_process_env_has_no_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("COMFYUI_BASE_URL=http://127.0.0.1:9199/\n", encoding="utf-8")
            with mock.patch.dict(os.environ):
                os.environ.pop("COMFYUI_BASE_URL", None)
                s = config.load_settings(dotenv_path=dotenv)
        self.assertEqual(s.base_url, "http://127.0.0.1:9199")


class ModelsRootTests(unittest.TestCase):
    def test_missing_models_root_is_config_missing(self):
        s = config.load_settings(env={})
        self.assertIn("config_missing", [e.code for e in s.errors])
        self.assertIsNone(s.models_root)
        action = next(e for e in s.errors if e.code == "config_missing").action
        self.assertIn("local-gateway/.env", action)

    def test_relative_models_root_is_config_invalid(self):
        s = config.load_settings(env={"COMFYUI_MODELS_ROOT": "ComfyUI/models"})
        self.assertFalse(s.models_root_valid)
        self.assertIn("config_invalid", [e.code for e in s.errors])
        self.assertIsNone(s.models_root)

    def test_nonexistent_models_root_is_config_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-dir"
            s = config.load_settings(env={"COMFYUI_MODELS_ROOT": str(missing)})
        self.assertFalse(s.models_root_valid)
        self.assertIn("config_invalid", [e.code for e in s.errors])

    def test_existing_absolute_models_root_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = config.load_settings(env={"COMFYUI_MODELS_ROOT": tmp})
            self.assertTrue(s.models_root_valid)
            self.assertTrue(s.config_valid)
            self.assertEqual(s.models_root, tmp)
            self.assertEqual(s.errors, ())

    def test_tilde_models_root_is_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "models").mkdir()
            with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}):
                s = config.load_settings(env={"COMFYUI_MODELS_ROOT": "~/models"})
        self.assertTrue(s.models_root_valid)
        self.assertEqual(s.models_root, os.path.join(tmp, "models"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run the tests to verify they fail**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_config.py' -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'local_gateway.config'` (or `ImportError: cannot import name 'config'`).

- [ ] **Step 5: Implement `local-gateway/local_gateway/config.py`**

```python
"""Gateway configuration: read, normalize and validate.

This module never opens sockets and never aggregates health: it turns
COMFYUI_BASE_URL and COMFYUI_MODELS_ROOT into a GatewaySettings object with
structured errors, so the app can still start when the configuration is
wrong and /health can explain what to fix.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

GATEWAY_DIR = Path(__file__).resolve().parents[1]

DEFAULT_BASE_URL = "http://127.0.0.1:9188"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

ACTION_SET_MODELS_ROOT = (
    "Edit local-gateway/.env and set COMFYUI_MODELS_ROOT to the absolute path"
    " of the ComfyUI models directory."
)
ACTION_FIX_BASE_URL = "Fix COMFYUI_BASE_URL in local-gateway/.env."


@dataclass(frozen=True)
class SettingsError:
    code: str  # "config_missing" or "config_invalid"
    message: str
    action: str


@dataclass(frozen=True)
class GatewaySettings:
    base_url: str
    models_root: str | None
    base_url_valid: bool
    models_root_valid: bool
    errors: tuple[SettingsError, ...] = ()

    @property
    def config_valid(self) -> bool:
        return not self.errors


def _base_url_ok(base_url: str) -> bool:
    parts = urlsplit(base_url)
    if parts.scheme != "http":
        return False
    try:
        parts.port
    except ValueError:
        return False
    if parts.hostname is None or parts.hostname.lower() not in _LOOPBACK_HOSTS:
        return False
    if parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    return parts.path in ("", "/")


def load_settings(env: Mapping[str, str] | None = None,
                  dotenv_path: Path | None = None) -> GatewaySettings:
    """Load gateway settings from the process env plus local-gateway/.env.

    dotenv uses override=False, so process environment variables win over
    .env values. Tests pass an explicit `env` (skipping dotenv entirely) or a
    temp `dotenv_path`; the developer's real .env is only read in production.
    """
    if env is None:
        load_dotenv(dotenv_path if dotenv_path is not None else GATEWAY_DIR / ".env",
                    override=False)
        env = os.environ

    errors: list[SettingsError] = []

    raw_base = env.get("COMFYUI_BASE_URL", "").strip() or DEFAULT_BASE_URL
    base_url = raw_base.rstrip("/")
    base_url_valid = _base_url_ok(base_url)
    if not base_url_valid:
        errors.append(SettingsError(
            "config_invalid",
            f"COMFYUI_BASE_URL must be an http loopback URL like {DEFAULT_BASE_URL},"
            f" without credentials, query, fragment or a path; got {base_url!r}.",
            ACTION_FIX_BASE_URL,
        ))

    raw_root = env.get("COMFYUI_MODELS_ROOT", "").strip()
    models_root: str | None = None
    models_root_valid = False
    if not raw_root:
        errors.append(SettingsError(
            "config_missing",
            "COMFYUI_MODELS_ROOT is not set.",
            ACTION_SET_MODELS_ROOT,
        ))
    else:
        expanded = os.path.expanduser(raw_root)
        if not os.path.isabs(expanded):
            errors.append(SettingsError(
                "config_invalid",
                f"COMFYUI_MODELS_ROOT must be an absolute directory, got {raw_root!r}.",
                ACTION_SET_MODELS_ROOT,
            ))
        elif not Path(expanded).is_dir():
            errors.append(SettingsError(
                "config_invalid",
                f"COMFYUI_MODELS_ROOT does not exist or is not a directory: {raw_root!r}.",
                ACTION_SET_MODELS_ROOT,
            ))
        else:
            models_root = expanded
            models_root_valid = True

    return GatewaySettings(
        base_url=base_url,
        models_root=models_root,
        base_url_valid=base_url_valid,
        models_root_valid=models_root_valid,
        errors=tuple(errors),
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_config.py' -v
```

Expected: 11 tests, OK.

- [ ] **Step 7: Commit**

```bash
git add local-gateway/pyproject.toml local-gateway/uv.lock local-gateway/.env.example \
  local-gateway/local_gateway/__init__.py local-gateway/local_gateway/config.py \
  local-gateway/tests/test_config.py
git commit -m "FEAT: local-gateway uv project with loopback config validation"
```

---

### Task 5: ComfyUIClient HTTP methods

**Files:**
- Create: `local-gateway/local_gateway/comfyui.py`
- Create: `local-gateway/tests/test_comfyui.py`

**Interfaces:**
- Consumes: gateway uv environment (requests).
- Produces:
  - `ComfyUIError(message: str, code: str = "comfyui_error", details: dict | None = None)` with attributes `.message`, `.code`, `.details`.
  - `ComfyUIClient(base_url: str, timeout: tuple[float, float] = (2, 10), session: requests.Session | None = None)`. The optional `session` parameter is a test seam; production callers get a fresh `requests.Session` with `trust_env=False`. Context manager plus `close()`.
  - Methods (spec 8.1): `system_stats() -> dict`, `object_info(node_name: str | None = None) -> dict`, `submit(prompt: dict) -> str` (POSTs `{"prompt": prompt}`), `history(prompt_id: str) -> dict`, `view(filename: str, subfolder: str = "", file_type: str = "output") -> bytes`, `upload(filename: str, content: bytes, content_type: str = "application/octet-stream") -> dict` (multipart field name `image`, form `type=input`, `overwrite` serialized as the string `"false"`). All calls use `(2, 10)` timeouts, no retries. A single-node `object_info("X")` call unwraps ComfyUI 0.45.0's `{ "X": {schema} }` envelope and returns the schema; a missing schema raises `ComfyUIError(code="node_missing")`. Connection failures raise `ComfyUIError(code="comfyui_unreachable")`; other HTTP/JSON problems raise `ComfyUIError(code="comfyui_error")`.

- [ ] **Step 1: Write the failing client tests**

Create `local-gateway/tests/test_comfyui.py`:

```python
"""local_gateway.comfyui: HTTP methods of the ComfyUI client.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v

Fakes only: no request leaves this process.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway.comfyui import ComfyUIClient, ComfyUIError  # noqa: E402

BASE = "http://127.0.0.1:9188"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text="",
                 json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


class FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.get_response = FakeResponse(json_data={})
        self.post_response = FakeResponse(json_data={})
        self.get_responses = []  # when set, popped per GET (for wait tests)
        self.trust_env = True
        self.closed = False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.get_responses:
            return self.get_responses.pop(0)
        return self.get_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response

    def close(self):
        self.closed = True


def make_client(session):
    return ComfyUIClient(BASE, session=session)


class HttpClientTests(unittest.TestCase):
    def test_system_stats_path_and_timeout(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={"system": {"comfyui_version": "0.45.0"}})
        stats = make_client(s).system_stats()
        self.assertEqual(stats, {"system": {"comfyui_version": "0.45.0"}})
        url, kwargs = s.get_calls[0]
        self.assertEqual(url, BASE + "/system_stats")
        self.assertEqual(kwargs["timeout"], (2, 10))

    def test_object_info_all_nodes_and_single_node(self):
        s = FakeSession()
        schema = {"input": {"required": {"unet_name": [["model.safetensors"], {}]}}}
        s.get_response = FakeResponse(json_data={"UNETLoader": schema})
        client = make_client(s)
        all_nodes = client.object_info()
        single = client.object_info("UNETLoader")
        self.assertEqual(all_nodes, {"UNETLoader": schema})
        self.assertEqual(single, schema)
        self.assertEqual(s.get_calls[0][0], BASE + "/object_info")
        self.assertEqual(s.get_calls[1][0], BASE + "/object_info/UNETLoader")

    def test_submit_posts_prompt_wrapper_and_returns_prompt_id(self):
        s = FakeSession()
        s.post_response = FakeResponse(json_data={"prompt_id": "abc-1"})
        prompt_id = make_client(s).submit({"3": {"class_type": "KSampler"}})
        self.assertEqual(prompt_id, "abc-1")
        url, kwargs = s.post_calls[0]
        self.assertEqual(url, BASE + "/prompt")
        self.assertEqual(kwargs["json"], {"prompt": {"3": {"class_type": "KSampler"}}})
        self.assertEqual(kwargs["timeout"], (2, 10))
        self.assertEqual(len(s.post_calls), 1)  # single POST, no retry

    def test_submit_without_prompt_id_raises_structured_error(self):
        s = FakeSession()
        s.post_response = FakeResponse(json_data={"number": 1})
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).submit({})
        self.assertIn("prompt_id", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "comfyui_error")

    def test_history_path_contains_prompt_id(self):
        s = FakeSession()
        make_client(s).history("abc-1")
        self.assertEqual(s.get_calls[0][0], BASE + "/history/abc-1")

    def test_view_sends_three_query_params_and_returns_bytes(self):
        s = FakeSession()
        s.get_response = FakeResponse(content=b"PNGDATA")
        data = make_client(s).view("img_00001_.png", "history", "temp")
        self.assertEqual(data, b"PNGDATA")
        url, kwargs = s.get_calls[0]
        self.assertEqual(url, BASE + "/view")
        self.assertEqual(kwargs["params"], {
            "filename": "img_00001_.png", "subfolder": "history", "type": "temp",
        })

    def test_view_defaults_subfolder_empty_and_type_output(self):
        s = FakeSession()
        make_client(s).view("x.png")
        self.assertEqual(s.get_calls[0][1]["params"], {
            "filename": "x.png", "subfolder": "", "type": "output",
        })

    def test_upload_uses_image_field_and_string_false_overwrite(self):
        s = FakeSession()
        s.post_response = FakeResponse(json_data={"name": "ref.wav", "type": "input"})
        result = make_client(s).upload("ref.wav", b"RIFF", "audio/wav")
        self.assertEqual(result, {"name": "ref.wav", "type": "input"})
        url, kwargs = s.post_calls[0]
        self.assertEqual(url, BASE + "/upload/image")
        self.assertEqual(kwargs["files"], {"image": ("ref.wav", b"RIFF", "audio/wav")})
        self.assertEqual(kwargs["data"], {"type": "input", "overwrite": "false"})

    def test_session_trust_env_false_and_context_manager_closes(self):
        s = FakeSession()
        with make_client(s) as client:
            client.system_stats()
        self.assertFalse(s.trust_env)
        self.assertTrue(s.closed)

    def test_http_error_raises_comfyui_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(status_code=500, text="boom")
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_error")

    def test_json_error_raises_comfyui_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(text="<html>", json_error=True)
        with self.assertRaises(ComfyUIError):
            make_client(s).system_stats()

    def test_non_dict_json_raises_comfyui_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data=[1, 2], text="[1, 2]")
        with self.assertRaises(ComfyUIError):
            make_client(s).system_stats()

    def test_connection_failure_is_comfyui_unreachable(self):
        class Broken(FakeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.ConnectionError("connection refused")

        with self.assertRaises(ComfyUIError) as ctx:
            make_client(Broken()).system_stats()
        self.assertEqual(ctx.exception.code, "comfyui_unreachable")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'local_gateway.comfyui'`.

- [ ] **Step 3: Implement `local-gateway/local_gateway/comfyui.py`**

```python
"""Original ComfyUI HTTP client: protocol and poll termination semantics.

Written against the public ComfyUI HTTP API only (no ComfyUI Python packages,
no third-party client code). One client owns one requests.Session with
trust_env=False; create a client per gateway request and close it, so no
Session is shared across threads.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import requests

DEFAULT_TIMEOUT = (2, 10)


class ComfyUIError(Exception):
    """Structured client error: message, machine-readable code, optional details."""

    def __init__(self, message: str, code: str = "comfyui_error",
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class ComfyUIClient:
    """Small typed wrapper over the ComfyUI HTTP API."""

    def __init__(self, base_url: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False

    def __enter__(self) -> "ComfyUIClient":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._session.close()

    # -- request helpers ---------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        url = self.base_url + path
        try:
            if method == "GET":
                return self._session.get(url, timeout=self._timeout, **kwargs)
            return self._session.post(url, timeout=self._timeout, **kwargs)
        except requests.exceptions.RequestException as e:
            raise ComfyUIError(
                f"Cannot reach ComfyUI at {self.base_url}: {e}",
                code="comfyui_unreachable",
            ) from e

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        response = self._request("GET", path, params=params)
        return self._json_or_error(response, "GET " + path)

    def _post_json(self, path: str, *, json=None, files=None, data=None) -> dict:
        response = self._request("POST", path, json=json, files=files, data=data)
        return self._json_or_error(response, "POST " + path)

    @staticmethod
    def _json_or_error(response, what: str) -> dict:
        if response.status_code != 200:
            raise ComfyUIError(
                f"ComfyUI returned HTTP {response.status_code} for {what}:"
                f" {response.text[:200]}",
                code="comfyui_error",
            )
        try:
            body = response.json()
        except ValueError as e:
            raise ComfyUIError(
                f"ComfyUI returned a non-JSON body for {what}: {response.text[:200]}",
                code="comfyui_error",
            ) from e
        if not isinstance(body, dict):
            raise ComfyUIError(
                f"ComfyUI returned JSON that is not an object for {what}.",
                code="comfyui_error",
            )
        return body

    # -- API methods (spec 8.1) --------------------------------------------

    def system_stats(self) -> dict:
        """GET /system_stats as a JSON dict."""
        return self._get_json("/system_stats")

    def object_info(self, node_name: str | None = None) -> dict:
        """GET /object_info, unwrapping the single-node envelope used by 0.45.0."""
        if node_name is None:
            return self._get_json("/object_info")
        body = self._get_json(f"/object_info/{quote(node_name)}")
        schema = body.get(node_name)
        if not isinstance(schema, dict):
            raise ComfyUIError(
                f"ComfyUI has no object_info schema for node {node_name!r}.",
                code="node_missing",
            )
        return schema

    def submit(self, prompt: dict) -> str:
        """POST /prompt with the workflow; returns the prompt_id string."""
        body = self._post_json("/prompt", json={"prompt": prompt})
        prompt_id = body.get("prompt_id")
        if not prompt_id or not isinstance(prompt_id, str):
            raise ComfyUIError(
                f"ComfyUI accepted POST /prompt but returned no prompt_id:"
                f" {str(body)[:200]}",
                code="comfyui_error",
            )
        return prompt_id

    def history(self, prompt_id: str) -> dict:
        """GET /history/{prompt_id}; callers treat a missing id as not-finished."""
        return self._get_json(f"/history/{quote(prompt_id)}")

    def view(self, filename: str, subfolder: str = "", file_type: str = "output") -> bytes:
        """GET /view with the three query parameters; returns raw bytes."""
        response = self._request(
            "GET", "/view",
            params={"filename": filename, "subfolder": subfolder, "type": file_type},
        )
        if response.status_code != 200:
            raise ComfyUIError(
                f"ComfyUI returned HTTP {response.status_code} for GET /view:"
                f" {response.text[:200]}",
                code="comfyui_error",
            )
        return response.content

    def upload(self, filename: str, content: bytes,
               content_type: str = "application/octet-stream") -> dict:
        """multipart POST /upload/image with type=input and overwrite=false.

        The multipart field stays `image` even for audio: that is the field
        name ComfyUI's protocol uses for input uploads.
        """
        return self._post_json(
            "/upload/image",
            files={"image": (filename, content, content_type)},
            data={"type": "input", "overwrite": "false"},
        )
```

Note: `wait` is deliberately absent in this task; Task 6 adds it.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v
```

Expected: 13 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add local-gateway/local_gateway/comfyui.py local-gateway/tests/test_comfyui.py
git commit -m "FEAT: original ComfyUI HTTP client methods with (2, 10) timeouts"
```

---

### Task 6: `wait` poll termination semantics

**Files:**
- Modify: `local-gateway/local_gateway/comfyui.py` (add `ComfyUITimeout` and `ComfyUIClient.wait`)
- Modify: `local-gateway/tests/test_comfyui.py` (append `WaitTests`)

**Interfaces:**
- Consumes: Task 5's `ComfyUIClient`, `ComfyUIError`, `history()`.
- Produces:
  - `ComfyUITimeout(prompt_id: str, deadline: float)`, subclass of `ComfyUIError`, `.code == "comfyui_timeout"`, `.prompt_id` attribute, message containing the prompt id.
  - `ComfyUIClient.wait(prompt_id: str, deadline: float, poll_interval: float = 1.0) -> dict`: `deadline` is an absolute `time.monotonic()` timestamp; returns the history entry on `status.completed=true` + `status.status_str="success"`; raises `ComfyUIError(code="execution_error")` with the prompt id and the status dict in `.details` for terminal non-success entries; raises `ComfyUITimeout` at the deadline; raises `ValueError` when `poll_interval <= 0` or `deadline <= time.monotonic()`; never calls `/interrupt`.

- [ ] **Step 1: Append the failing wait tests**

Append to `local-gateway/tests/test_comfyui.py`, above the `if __name__ == "__main__":` block (extend the import line to include `ComfyUITimeout` and add `import time` at the top):

```python
import time  # noqa: E402

from local_gateway.comfyui import ComfyUITimeout  # noqa: E402


def success_history(prompt_id):
    return {prompt_id: {"status": {"completed": True, "status_str": "success"},
                        "outputs": {}}}


class WaitTests(unittest.TestCase):
    def test_wait_returns_the_successful_history_entry(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data=success_history("p1"))
        entry = make_client(s).wait("p1", deadline=time.monotonic() + 5,
                                    poll_interval=0.01)
        self.assertEqual(entry["status"]["status_str"], "success")

    def test_wait_polls_until_the_prompt_appears(self):
        s = FakeSession()
        s.get_responses = [
            FakeResponse(json_data={}),
            FakeResponse(json_data={}),
            FakeResponse(json_data=success_history("p2")),
        ]
        entry = make_client(s).wait("p2", deadline=time.monotonic() + 5,
                                    poll_interval=0.01)
        self.assertEqual(entry["status"]["status_str"], "success")
        self.assertEqual(len(s.get_calls), 3)

    def test_wait_execution_error_keeps_prompt_id_and_status(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={
            "p3": {"status": {"completed": True, "status_str": "error",
                              "messages": ["Execution interrupted"]}},
        })
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).wait("p3", deadline=time.monotonic() + 5,
                                poll_interval=0.01)
        self.assertIn("p3", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "execution_error")
        self.assertEqual(ctx.exception.details["status_str"], "error")

    def test_wait_cancelled_entry_is_an_execution_error(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={
            "p4": {"status": {"completed": False, "status_str": "cancelled"}},
        })
        with self.assertRaises(ComfyUIError) as ctx:
            make_client(s).wait("p4", deadline=time.monotonic() + 5,
                                poll_interval=0.01)
        self.assertEqual(ctx.exception.code, "execution_error")

    def test_wait_rejects_nonpositive_poll_interval(self):
        with self.assertRaises(ValueError):
            make_client(FakeSession()).wait("p1", deadline=time.monotonic() + 5,
                                            poll_interval=0)

    def test_wait_rejects_deadline_in_the_past(self):
        with self.assertRaises(ValueError):
            make_client(FakeSession()).wait("p1", deadline=time.monotonic() - 1,
                                            poll_interval=0.01)

    def test_wait_timeout_raises_comfyui_timeout_with_prompt_id(self):
        s = FakeSession()
        s.get_response = FakeResponse(json_data={})  # prompt never appears
        with self.assertRaises(ComfyUITimeout) as ctx:
            make_client(s).wait("p9", deadline=time.monotonic() + 0.05,
                                poll_interval=0.01)
        self.assertEqual(ctx.exception.prompt_id, "p9")
        self.assertIn("p9", ctx.exception.message)
        self.assertTrue(issubclass(ComfyUITimeout, ComfyUIError))
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v
```

Expected: ERROR with `ImportError: cannot import name 'ComfyUITimeout'`.

- [ ] **Step 3: Implement `ComfyUITimeout` and `wait`**

In `local-gateway/local_gateway/comfyui.py`, add directly after `ComfyUIError`:

```python
class ComfyUITimeout(ComfyUIError):
    """wait() passed its deadline; the ComfyUI job was NOT cancelled."""

    def __init__(self, prompt_id: str, deadline: float):
        super().__init__(
            f"Timed out waiting for ComfyUI prompt {prompt_id}: monotonic deadline"
            f" {deadline:.1f} reached. The ComfyUI job keeps running; this client"
            f" never calls /interrupt.",
            code="comfyui_timeout",
        )
        self.prompt_id = prompt_id
```

And add the method at the end of the `ComfyUIClient` class:

```python
    def wait(self, prompt_id: str, deadline: float, poll_interval: float = 1.0) -> dict:
        """Poll history until the prompt finishes, fails, or the deadline passes.

        `deadline` is an absolute time.monotonic() timestamp. Individual poll
        timeouts are not a total deadline, and this method never calls
        /interrupt: a ComfyUITimeout leaves the backend job running.
        """
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be > 0, got {poll_interval}")
        if deadline <= time.monotonic():
            raise ValueError(
                f"deadline must be in the future of time.monotonic(), got {deadline}"
            )
        while True:
            data = self.history(prompt_id)
            entry = data.get(prompt_id) if isinstance(data, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                status_str = status.get("status_str")
                completed = bool(status.get("completed"))
                if completed and status_str == "success":
                    return entry
                if completed or status_str in ("error", "cancelled"):
                    raise ComfyUIError(
                        f"ComfyUI execution failed for prompt {prompt_id}:"
                        f" status_str={status_str!r}, completed={completed},"
                        f" status={status}",
                        code="execution_error",
                        details=status,
                    )
            if time.monotonic() >= deadline:
                raise ComfyUITimeout(prompt_id, deadline)
            time.sleep(poll_interval)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_comfyui.py' -v
```

Expected: 20 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add local-gateway/local_gateway/comfyui.py local-gateway/tests/test_comfyui.py
git commit -m "FEAT: ComfyUIClient.wait with monotonic deadline and non-cancelling timeout"
```

---

### Task 7: Model registry

**Files:**
- Create: `local-gateway/local_gateway/registry.py`
- Create: `local-gateway/tests/test_health.py` (first chunk: `RegistryTests`)

**Interfaces:**
- Consumes: nothing (pure data module).
- Produces (all in `local_gateway.registry`):
  - `RequiredOption(node: str, input: str, values: tuple[str, ...])` frozen dataclass.
  - `ModelEntry(key, capability, required_files=(), required_directories=(), optional_files=(), required_nodes=(), required_options=())` frozen dataclass.
  - `MODEL_ENTRIES: tuple[ModelEntry, ...]` with exactly the three first-release models (`qwen3-tts-1.7B`/`tts`, `qwen-image-2.1`/`image`, `minimax-h3-fl2va`/`video`) and the file/node/option lists from spec 9.2.
  - `ROUTE_TARGETS: dict[str, str]` = `{"tts": "qwen3-tts-1.7B", "image_generate": "qwen-image-2.1", "image_edit": "qwen-image-2.1", "video": "minimax-h3-fl2va"}`.
  - `IMPLEMENTED_ROUTES: frozenset[str]` = `frozenset()` in P1.
  - `required_nodes() -> tuple[str, ...]` (unique across entries, first-seen order) and `model_by_key(key: str) -> ModelEntry | None`.

  Registry convention: `required_files`/`optional_files` paths are relative to `COMFYUI_MODELS_ROOT` (forward slashes). `required_options` values are the exact strings ComfyUI lists in the loader's choice list, which for these weights is the bare filename (loader choices are relative to their own model folder: `diffusion_models`, `text_encoders`, `vae`).

- [ ] **Step 1: Write the failing registry tests**

Create `local-gateway/tests/test_health.py`:

```python
"""local_gateway registry, health aggregation and FastAPI /health tests.

    uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v

Fakes only: temp model trees, scripted client metadata, no network, no real
.env, no real ComfyUI.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_gateway import registry  # noqa: E402
from local_gateway.registry import RequiredOption  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_three_first_release_models_with_capabilities(self):
        self.assertEqual({e.key: e.capability for e in registry.MODEL_ENTRIES}, {
            "qwen3-tts-1.7B": "tts",
            "qwen-image-2.1": "image",
            "minimax-h3-fl2va": "video",
        })

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

    def test_image_entry_files_nodes_and_options(self):
        entry = registry.model_by_key("qwen-image-2.1")
        self.assertEqual(entry.required_files, (
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ))
        self.assertEqual(entry.required_nodes, (
            "UNETLoader", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21",
            "KSampler", "SaveImage", "LoadImage",
        ))
        self.assertIn(RequiredOption(
            "UNETLoader", "unet_name",
            ("qwen_image_2.1_turbo_int8_convrot.safetensors",)), entry.required_options)
        self.assertIn(RequiredOption(
            "CLIPLoader", "clip_name",
            ("qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
             "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors")),
            entry.required_options)
        self.assertIn(RequiredOption(
            "VAELoader", "vae_name", ("qwen_image_2.1_vae_bf16.safetensors",)),
            entry.required_options)
        self.assertIn(RequiredOption("CLIPLoader", "type", ("qwen_image",)),
                      entry.required_options)

    def test_video_entry_keeps_double_extension_and_optional_audio_vae(self):
        entry = registry.model_by_key("minimax-h3-fl2va")
        self.assertIn("vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
                      entry.required_files)
        self.assertEqual(entry.optional_files,
                         ("vae/minimax_h3_audio_vae_fp32.safetensors",))
        self.assertEqual(entry.required_nodes, (
            "UNETLoader", "CLIPLoader", "VAELoader", "EmptyMiniMaxH3LatentAV",
            "MiniMaxH3ImageToVideo", "KSampler", "VAEDecode", "VHS_VideoCombine",
        ))
        self.assertIn(RequiredOption(
            "VHS_VideoCombine", "format", ("video/h264-mp4",)), entry.required_options)
        self.assertIn(RequiredOption("CLIPLoader", "type", ("minimax",)),
                      entry.required_options)

    def test_route_targets_and_empty_implemented_set(self):
        self.assertEqual(registry.ROUTE_TARGETS, {
            "tts": "qwen3-tts-1.7B",
            "image_generate": "qwen-image-2.1",
            "image_edit": "qwen-image-2.1",
            "video": "minimax-h3-fl2va",
        })
        self.assertEqual(registry.IMPLEMENTED_ROUTES, frozenset())

    def test_required_nodes_is_unique_across_entries(self):
        nodes = registry.required_nodes()
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertIn("UNETLoader", nodes)
        self.assertIn("VHS_VideoCombine", nodes)

    def test_model_by_key_unknown_returns_none(self):
        self.assertIsNone(registry.model_by_key("no-such-model"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'local_gateway.registry'` (or `No module named 'local_gateway'` if the package import fails first).

- [ ] **Step 3: Implement `local-gateway/local_gateway/registry.py`**

```python
"""Model registry: plain data and pure lookups only.

No filesystem access, no network, no plugin mechanism. File paths are
relative to COMFYUI_MODELS_ROOT with forward slashes; required_options values
are the exact strings ComfyUI lists in the node input's choice list, which
for first-release weights is the bare filename (loader choices are relative
to their own model folder). The P1 node sets are the health baseline only;
P2-P4 must re-verify /object_info before building workflows. Cloud API nodes
never belong here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RequiredOption:
    """A value that must appear in a node input's allowed choices."""

    node: str
    input: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ModelEntry:
    key: str
    capability: str
    required_files: tuple[str, ...] = ()
    required_directories: tuple[str, ...] = ()
    optional_files: tuple[str, ...] = ()
    required_nodes: tuple[str, ...] = ()
    required_options: tuple[RequiredOption, ...] = ()


MODEL_ENTRIES: tuple[ModelEntry, ...] = (
    ModelEntry(
        key="qwen3-tts-1.7B",
        capability="tts",
        required_directories=("TTS/Qwen3-TTS/",),
        required_nodes=(
            "AILab_Qwen3TTSCustomVoice",
            "AILab_Qwen3TTSVoiceClone",
            "AILab_Qwen3TTSVoiceDesign",
            "LoadAudio",
            "SaveAudioAdvanced",
        ),
        required_options=(
            RequiredOption("AILab_Qwen3TTSCustomVoice", "model_size", ("1.7B",)),
            RequiredOption("AILab_Qwen3TTSVoiceClone", "model_size", ("1.7B",)),
            RequiredOption("AILab_Qwen3TTSVoiceDesign", "model_size", ("1.7B",)),
            RequiredOption("AILab_Qwen3TTSCustomVoice", "speaker", ("Ryan",)),
        ),
    ),
    ModelEntry(
        key="qwen-image-2.1",
        capability="image",
        required_files=(
            "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
            "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
        ),
        required_nodes=(
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "TextEncodeQwenImage21",
            "KSampler",
            "SaveImage",
            "LoadImage",
        ),
        required_options=(
            RequiredOption(
                "UNETLoader", "unet_name",
                ("qwen_image_2.1_turbo_int8_convrot.safetensors",),
            ),
            RequiredOption(
                "CLIPLoader", "clip_name",
                ("qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
                 "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"),
            ),
            RequiredOption(
                "VAELoader", "vae_name",
                ("qwen_image_2.1_vae_bf16.safetensors",),
            ),
            RequiredOption("CLIPLoader", "type", ("qwen_image",)),
        ),
    ),
    ModelEntry(
        key="minimax-h3-fl2va",
        capability="video",
        required_files=(
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            # Double extension is the real on-disk filename; do not "fix" it.
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ),
        optional_files=("vae/minimax_h3_audio_vae_fp32.safetensors",),
        required_nodes=(
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "EmptyMiniMaxH3LatentAV",
            "MiniMaxH3ImageToVideo",
            "KSampler",
            "VAEDecode",
            "VHS_VideoCombine",
        ),
        required_options=(
            RequiredOption(
                "UNETLoader", "unet_name",
                ("minimax_h3_fl2va_pruned_int8_convrot.safetensors",),
            ),
            RequiredOption(
                "CLIPLoader", "clip_name",
                ("qwen3vl_32b_minimax_h3_int8_convrot.safetensors",),
            ),
            RequiredOption(
                "VAELoader", "vae_name",
                ("minimax_h3_video_vae_fp16.safetensors",
                 "minimax_h3_image_vae_fp16.safetensors.safetensors"),
            ),
            RequiredOption("CLIPLoader", "type", ("minimax",)),
            RequiredOption("VHS_VideoCombine", "format", ("video/h264-mp4",)),
        ),
    ),
)

ROUTE_TARGETS: dict[str, str] = {
    "tts": "qwen3-tts-1.7B",
    "image_generate": "qwen-image-2.1",
    "image_edit": "qwen-image-2.1",
    "video": "minimax-h3-fl2va",
}

# Routes whose POST endpoint actually works. P1 implements none of them; a
# later phase flips a route only after its real generation acceptance passes.
IMPLEMENTED_ROUTES: frozenset[str] = frozenset()


def required_nodes() -> tuple[str, ...]:
    """Unique required node names across all entries, first-seen order."""
    seen: dict[str, None] = {}
    for entry in MODEL_ENTRIES:
        for node in entry.required_nodes:
            seen.setdefault(node, None)
    return tuple(seen)


def model_by_key(key: str) -> ModelEntry | None:
    for entry in MODEL_ENTRIES:
        if entry.key == key:
            return entry
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: 7 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add local-gateway/local_gateway/registry.py local-gateway/tests/test_health.py
git commit -m "FEAT: gateway model registry with three first-release baselines"
```

---

### Task 8: Health aggregation

**Files:**
- Create: `local-gateway/local_gateway/health.py`
- Modify: `local-gateway/tests/test_health.py` (append fixtures and `HealthAggregationTests`)

**Interfaces:**
- Consumes: Task 4 `GatewaySettings` (`.base_url`, `.base_url_valid`, `.models_root`, `.models_root_valid`, `.errors`, `.config_valid`), Task 5/6 `ComfyUIClient` + `ComfyUIError`, Task 7 registry symbols.
- Produces: `health.build_health_payload(settings: GatewaySettings, client_factory: Callable[[str], ComfyUIClient]) -> tuple[dict, int]`. The factory receives `settings.base_url` and must yield a context-manager client exposing `system_stats()` and `object_info(node_name)`. Payload shape per spec 10.2 (top-level `ready`, `models_ready`, `comfyui`, `models`, `routes`, `errors`; per-model `ready/files_ready/nodes_ready/optional_files_present/missing_files/missing_nodes/missing_loader_values`; route `implemented/available`; error items `{code, scope, message, action}`). Status 200 iff `ready` else 503. Scope values used: `config`, `comfyui`, `model`, `gateway`. Error codes emitted: `config_missing`, `config_invalid`, `comfyui_unreachable`, `comfyui_error`, `model_file_missing`, `node_missing`, `loader_option_missing`, `internal_error`. Never raises; never returns argv/launch-command fields.

- [ ] **Step 1: Append the failing health tests**

Append to `local-gateway/tests/test_health.py`, above the `if __name__ == "__main__":` block (add these imports at the top, next to the existing ones):

```python
import tempfile  # noqa: E402

from local_gateway import config, health  # noqa: E402
from local_gateway.comfyui import ComfyUIError  # noqa: E402


FULL_STATS = {
    "system": {
        "os": "nt",
        "ram_total": 68611555328,
        "ram_free": 34000000000,
        "comfyui_version": "0.45.0",
        "python_version": "3.12",
        "argv": ["python", "main.py", "--listen", "0.0.0.0", "--port", "9188"],
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
        "LoadAudio": {"input": {"required": [["audio", "AUDIO", {"tooltip": "x"}]]}},
        "SaveAudioAdvanced": {"input": {"required": [["format", ["flac", "mp3", "opus"]]]}},
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


def build_model_tree(root, *, include=("tts", "image", "video"),
                     optional_audio_vae=False):
    if "tts" in include:
        (root / "TTS" / "Qwen3-TTS").mkdir(parents=True, exist_ok=True)
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


class HealthAggregationTests(unittest.TestCase):
    def settings_for(self, root, base_url=None):
        env = {"COMFYUI_MODELS_ROOT": str(root)}
        if base_url is not None:
            env["COMFYUI_BASE_URL"] = base_url
        return config.load_settings(env=env)

    def test_full_ready_returns_200_with_whitelisted_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, optional_audio_vae=True)
            client = FakeClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["models_ready"])
        self.assertTrue(payload["comfyui"]["reachable"])
        self.assertEqual(payload["comfyui"]["version"], "0.45.0")
        self.assertEqual(payload["comfyui"]["gpu"]["name"], "NVIDIA GeForce RTX 4090")
        self.assertEqual(payload["comfyui"]["gpu"]["vram_total"], 25769803776)
        self.assertEqual(payload["comfyui"]["ram"]["total"], 68611555328)
        self.assertEqual(set(payload["comfyui"]),
                         {"base_url", "reachable", "version", "gpu", "ram"})
        self.assertNotIn("argv", payload["comfyui"])
        self.assertEqual(set(payload["models"]),
                         {"qwen3-tts-1.7B", "qwen-image-2.1", "minimax-h3-fl2va"})
        self.assertTrue(all(m["ready"] for m in payload["models"].values()))
        self.assertEqual(payload["models"]["minimax-h3-fl2va"]["optional_files_present"],
                         ["vae/minimax_h3_audio_vae_fp32.safetensors"])
        self.assertEqual(payload["models"]["qwen-image-2.1"]["optional_files_present"], [])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["routes"], {
            "tts": {"implemented": False, "available": False},
            "image_generate": {"implemented": False, "available": False},
            "image_edit": {"implemented": False, "available": False},
            "video": {"implemented": False, "available": False},
        })

    def test_missing_video_files_report_relative_paths_and_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("tts", "image"))
            client = FakeClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["models_ready"])
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["files_ready"])
        self.assertTrue(video["nodes_ready"])
        self.assertEqual(video["missing_files"], [
            "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "vae/minimax_h3_video_vae_fp16.safetensors",
            "vae/minimax_h3_image_vae_fp16.safetensors.safetensors",
        ])
        file_errors = [e for e in payload["errors"] if e["code"] == "model_file_missing"]
        self.assertEqual(len(file_errors), 4)
        self.assertEqual(set(file_errors[0]),
                         {"code", "scope", "message", "action"})
        self.assertIn("minimax-h3-fl2va", file_errors[0]["message"])

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

    def test_missing_node_and_loader_value_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture()
            meta.pop("VHS_VideoCombine")
            meta["CLIPLoader"] = {"input": {"required": [
                ["clip_name", ["qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
                               "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
                               "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"]],
                ["type", ["stable_diffusion", "qwen_image"]],
            ]}}
            client = FakeClient(object_info=meta)
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["nodes_ready"])
        self.assertEqual(video["missing_nodes"], ["VHS_VideoCombine"])
        self.assertEqual(video["missing_loader_values"], ["CLIPLoader.type=minimax"])
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("node_missing", codes)
        self.assertIn("loader_option_missing", codes)
        # The image model is unaffected: its CLIPLoader.type value still exists.
        self.assertTrue(payload["models"]["qwen-image-2.1"]["ready"])

    def test_tts_model_size_and_speaker_enums_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            meta = object_info_fixture(
                AILab_Qwen3TTSCustomVoice={"input": {"required": [
                    ["model_size", ["0.6B"]],
                    ["speaker", ["Chelsie"]],
                ]}},
            )
            client = FakeClient(object_info=meta)
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        tts = payload["models"]["qwen3-tts-1.7B"]
        self.assertFalse(tts["nodes_ready"])
        self.assertEqual(tts["missing_loader_values"], [
            "AILab_Qwen3TTSCustomVoice.model_size=1.7B",
            "AILab_Qwen3TTSCustomVoice.speaker=Ryan",
        ])

    def test_comfyui_unreachable_still_reports_file_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(
                exc=ComfyUIError("connection refused", code="comfyui_unreachable"))
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        self.assertFalse(payload["comfyui"]["reachable"])
        for model in payload["models"].values():
            self.assertTrue(model["files_ready"])
            self.assertFalse(model["nodes_ready"])
            self.assertEqual(model["missing_nodes"], [])
            self.assertEqual(model["missing_loader_values"], [])
        codes = [e["code"] for e in payload["errors"]]
        self.assertEqual(codes, ["comfyui_unreachable"])
        action = payload["errors"][0]["action"]
        self.assertIn("9188", action)
        self.assertIn("COMFYUI_BASE_URL", action)

    def test_comfyui_http_error_maps_to_comfyui_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(exc=ComfyUIError("HTTP 500 from ComfyUI",
                                                 code="comfyui_error"))
            payload, _ = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertIn("comfyui_error", [e["code"] for e in payload["errors"]])
        self.assertFalse(payload["comfyui"]["reachable"])

    def test_object_info_http_error_is_unverified_not_a_missing_node(self):
        class BrokenClient(FakeClient):
            def object_info(self, node_name=None):
                if node_name == "VHS_VideoCombine":
                    raise ComfyUIError("HTTP 500 for VHS_VideoCombine",
                                       code="comfyui_error")
                return super().object_info(node_name)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = BrokenClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(
                self.settings_for(root), lambda url: client)
        self.assertEqual(status, 503)
        video = payload["models"]["minimax-h3-fl2va"]
        self.assertFalse(video["nodes_ready"])
        self.assertEqual(video["missing_nodes"], [])
        self.assertEqual(video["missing_loader_values"], [])
        codes = [e["code"] for e in payload["errors"]]
        self.assertIn("comfyui_error", codes)
        self.assertNotIn("node_missing", codes)

    def test_missing_models_root_config_leaves_missing_files_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            settings = config.load_settings(env={})
            client = FakeClient(object_info=object_info_fixture())
            payload, status = health.build_health_payload(settings,
                                                           lambda url: client)
        self.assertEqual(status, 503)
        for model in payload["models"].values():
            self.assertFalse(model["files_ready"])
            self.assertEqual(model["missing_files"], [])
            self.assertTrue(model["nodes_ready"])  # ComfyUI checks still ran
        self.assertIn("config_missing", [e["code"] for e in payload["errors"]])

    def test_invalid_base_url_skips_the_client_entirely(self):
        def never(url):
            raise AssertionError("client_factory must not run with an invalid base URL")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = health.build_health_payload(
                self.settings_for(root, base_url="http://10.9.9.9:9188"), never)
        self.assertEqual(status, 503)
        self.assertFalse(payload["comfyui"]["reachable"])
        self.assertIn("config_invalid", [e["code"] for e in payload["errors"]])
        for model in payload["models"].values():
            self.assertFalse(model["nodes_ready"])
            self.assertEqual(model["missing_nodes"], [])

    def test_unexpected_exception_is_internal_error_without_traceback(self):
        def boom(url):
            raise ZeroDivisionError("division by zero")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            payload, status = health.build_health_payload(self.settings_for(root), boom)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(len(payload["errors"]), 1)
        error = payload["errors"][0]
        self.assertEqual(error["code"], "internal_error")
        self.assertIn("ZeroDivisionError", error["message"])
        self.assertNotIn("Traceback", str(payload))
        self.assertEqual(set(payload["models"]),
                         {"qwen3-tts-1.7B", "qwen-image-2.1", "minimax-h3-fl2va"})
        self.assertTrue(all(not r["available"] for r in payload["routes"].values()))
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: `RegistryTests` pass; new tests ERROR with `ImportError: cannot import name 'health'` (or `ModuleNotFoundError` for `local_gateway.health`).

- [ ] **Step 3: Implement `local-gateway/local_gateway/health.py`**

```python
"""Aggregate config, ComfyUI metadata and model files into the /health payload.

Pure aggregation: health.py opens no sockets itself. Filesystem checks are
limited to _check_files beneath the configured models root; metadata comes
from the injected client factory. build_health_payload returns
(payload, http_status) and never raises; unexpected exceptions are logged
without returning a traceback.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from . import registry
from .comfyui import ComfyUIError, ComfyUIClient
from .config import GatewaySettings

ClientFactory = Callable[[str], ComfyUIClient]
logger = logging.getLogger(__name__)

_MODEL_SKELETON = {
    "ready": False,
    "files_ready": False,
    "nodes_ready": False,
    "optional_files_present": [],
    "missing_files": [],
    "missing_nodes": [],
    "missing_loader_values": [],
}


def _err(code: str, scope: str, message: str, action: str) -> dict:
    return {"code": code, "scope": scope, "message": message, "action": action}


def _comfyui_error(e: ComfyUIError) -> dict:
    if e.code == "comfyui_unreachable":
        return _err(
            "comfyui_unreachable", "comfyui",
            f"ComfyUI is not reachable: {e.message}",
            "Start ComfyUI on 127.0.0.1:9188 first, then check COMFYUI_BASE_URL"
            " in local-gateway/.env.",
        )
    return _err(
        "comfyui_error", "comfyui",
        f"ComfyUI returned an error: {e.message}",
        "Check the ComfyUI console log and its version; this gateway expects"
        " the local ComfyUI HTTP API.",
    )


def _apply_system_stats(comfyui: dict, stats: dict) -> None:
    """Copy only the whitelisted system_stats fields; never argv or paths."""
    system = stats.get("system") if isinstance(stats, dict) else None
    if isinstance(system, dict):
        comfyui["version"] = system.get("comfyui_version")
        comfyui["ram"] = {"total": system.get("ram_total"),
                          "free": system.get("ram_free")}
    devices = stats.get("devices") if isinstance(stats, dict) else None
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        comfyui["gpu"] = {"name": devices[0].get("name"),
                          "vram_total": devices[0].get("vram_total"),
                          "vram_free": devices[0].get("vram_free")}


def _check_files(entry: registry.ModelEntry, models_root: str):
    missing: list[str] = []
    optional_present: list[str] = []
    ok = True
    root = Path(models_root)
    for rel in entry.required_directories:
        if not (root / rel).is_dir():
            ok = False
            missing.append(rel)
    for rel in entry.required_files:
        if not (root / rel).is_file():
            ok = False
            missing.append(rel)
    for rel in entry.optional_files:
        if (root / rel).is_file():
            optional_present.append(rel)
    return ok, missing, optional_present


def _input_values(meta: dict, input_name: str) -> list | None:
    """Return combo choices from both current and legacy object_info shapes.

    ComfyUI 0.45.0 uses required/optional dicts keyed by input name. Older
    snapshots used a list of [input_name, choices, ...] entries. The client
    has already unwrapped the single-node envelope before this function runs.
    """
    inputs = meta.get("input", {}) if isinstance(meta, dict) else {}
    for section in ("required", "optional"):
        fields = inputs.get(section)
        if isinstance(fields, dict):
            spec = fields.get(input_name)
            if (isinstance(spec, (list, tuple)) and spec
                    and isinstance(spec[0], list)):
                return spec[0]
            if spec is not None:
                return None
        elif isinstance(fields, list):
            for spec in fields:
                if (isinstance(spec, (list, tuple)) and len(spec) >= 2
                        and spec[0] == input_name):
                    return spec[1] if isinstance(spec[1], list) else None
    return None


def _check_nodes(entry: registry.ModelEntry, object_info_cache: dict,
                 unverified_nodes=frozenset()):
    relevant_unverified = {
        n for n in entry.required_nodes if n in unverified_nodes
    }
    missing_nodes = [
        n for n in entry.required_nodes
        if n not in unverified_nodes and not object_info_cache.get(n)
    ]
    missing_values: list[str] = []
    for option in entry.required_options:
        if option.node in unverified_nodes:
            relevant_unverified.add(option.node)
            continue  # HTTP/JSON failure: unverified, not a missing node/value
        meta = object_info_cache.get(option.node)
        if not meta:
            continue  # already reported through missing_nodes
        values = _input_values(meta, option.input)
        for value in option.values:
            if values is None or value not in values:
                missing_values.append(f"{option.node}.{option.input}={value}")
    ready = not relevant_unverified and not missing_nodes and not missing_values
    return ready, missing_nodes, missing_values


def build_health_payload(settings: GatewaySettings,
                         client_factory: ClientFactory) -> tuple[dict, int]:
    """Run every check and return (payload, 200 or 503). Never raises."""
    try:
        return _build(settings, client_factory)
    except Exception as e:
        logger.exception("Unexpected error while building local gateway health")
        message = f"{type(e).__name__}: {str(e)[:200]}"
        payload = {
            "ready": False,
            "models_ready": False,
            "comfyui": {
                "base_url": settings.base_url,
                "reachable": False,
                "version": None,
                "gpu": None,
                "ram": None,
            },
            "models": {entry.key: dict(_MODEL_SKELETON)
                       for entry in registry.MODEL_ENTRIES},
            "routes": {
                route: {"implemented": route in registry.IMPLEMENTED_ROUTES,
                        "available": False}
                for route in registry.ROUTE_TARGETS
            },
            "errors": [
                _err("internal_error", "gateway",
                     f"Unexpected gateway error: {message}",
                     "Check the gateway console log; this response never"
                     " includes tracebacks."),
            ],
        }
        return payload, 503


def _build(settings: GatewaySettings,
           client_factory: ClientFactory) -> tuple[dict, int]:
    errors = [_err(e.code, "config", e.message, e.action) for e in settings.errors]

    comfyui = {
        "base_url": settings.base_url,
        "reachable": False,
        "version": None,
        "gpu": None,
        "ram": None,
    }
    object_info_cache: dict[str, dict | None] = {}
    unverified_nodes: set[str] = set()

    if settings.base_url_valid:
        try:
            with client_factory(settings.base_url) as client:
                _apply_system_stats(comfyui, client.system_stats())
                comfyui["reachable"] = True
                for node in registry.required_nodes():
                    try:
                        object_info_cache[node] = client.object_info(node)
                    except ComfyUIError as e:
                        object_info_cache[node] = None
                        if e.code == "node_missing":
                            continue  # _check_nodes reports this exact absence
                        if e.code == "comfyui_unreachable":
                            # ComfyUI went away mid-check: stop instead of
                            # misreporting every remaining node as missing.
                            comfyui["reachable"] = False
                            errors.append(_comfyui_error(e))
                            break
                        unverified_nodes.add(node)
                        errors.append(_comfyui_error(e))
        except ComfyUIError as e:
            errors.append(_comfyui_error(e))

    models: dict[str, dict] = {}
    for entry in registry.MODEL_ENTRIES:
        if settings.models_root_valid and settings.models_root is not None:
            files_ready, missing_files, optional_present = _check_files(
                entry, settings.models_root)
            for rel in missing_files:
                errors.append(_err(
                    "model_file_missing", "model",
                    f"{entry.key}: required path {rel} is missing.",
                    f"Place {rel} under COMFYUI_MODELS_ROOT, in the matching"
                    f" ComfyUI models subdirectory.",
                ))
        else:
            files_ready, missing_files, optional_present = False, [], []

        if comfyui["reachable"]:
            nodes_ready, missing_nodes, missing_values = _check_nodes(
                entry, object_info_cache, unverified_nodes)
            for node in missing_nodes:
                errors.append(_err(
                    "node_missing", "model",
                    f"{entry.key}: ComfyUI node {node} is not available.",
                    "Install or enable the local custom node package that"
                    " provides this node.",
                ))
            for value in missing_values:
                node_input = value.split("=", 1)[0]
                errors.append(_err(
                    "loader_option_missing", "model",
                    f"{entry.key}: {value} is not offered by ComfyUI.",
                    f"Check {node_input} in ComfyUI /object_info; the expected"
                    f" value is missing. Verify the installed weights or node"
                    f" version.",
                ))
        else:
            nodes_ready, missing_nodes, missing_values = False, [], []

        models[entry.key] = {
            "ready": files_ready and nodes_ready,
            "files_ready": files_ready,
            "nodes_ready": nodes_ready,
            "optional_files_present": optional_present,
            "missing_files": missing_files,
            "missing_nodes": missing_nodes,
            "missing_loader_values": missing_values,
        }

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

    payload = {
        "ready": ready,
        "models_ready": models_ready,
        "comfyui": comfyui,
        "models": models,
        "routes": routes,
        "errors": errors,
    }
    return payload, (200 if ready else 503)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: 18 tests (7 registry + 11 aggregation), OK.

- [ ] **Step 5: Run the whole gateway suite**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all OK.

- [ ] **Step 6: Commit**

```bash
git add local-gateway/local_gateway/health.py local-gateway/tests/test_health.py
git commit -m "FEAT: gateway health aggregation with per-model and route status"
```

---

### Task 9: FastAPI server, start script and gateway README

**Files:**
- Create: `local-gateway/local_gateway/server.py`, `local-gateway/start_gateway.bat`, `local-gateway/README.md`
- Modify: `local-gateway/tests/test_health.py` (append `ServerTests`)

**Interfaces:**
- Consumes: Task 4 `load_settings()`/`GatewaySettings`, Task 5 `ComfyUIClient`, Task 8 `build_health_payload`.
- Produces:
  - `server.create_app(settings: GatewaySettings | None = None, client_factory: Callable[[str], ComfyUIClient] | None = None) -> FastAPI`. Defaults: `load_settings()` is called lazily inside the request, never at import/app creation; `default_client_factory(base_url) -> ComfyUIClient(base_url)`. Creating the app performs no network request and reads no real `.env`.
  - Module-level `server.app = create_app()` for `uvicorn local_gateway.server:app`.
  - `GET /health` as a plain `def` (Starlette runs it in a threadpool) returning `JSONResponse(content=payload, status_code=200|503)`.

- [ ] **Step 1: Append the failing server tests**

Append to `local-gateway/tests/test_health.py`, above the `if __name__ == "__main__":` block (add this import at the top):

```python
from unittest import mock  # noqa: E402

from local_gateway import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


class ServerTests(unittest.TestCase):
    def make_app(self, root, client):
        settings = config.load_settings(env={"COMFYUI_MODELS_ROOT": str(root)})
        return server.create_app(settings=settings, client_factory=lambda url: client)

    def test_health_200_when_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root)
            client = FakeClient(object_info=object_info_fixture())
            with TestClient(self.make_app(root, client)) as tc:
                response = tc.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ready"], True)
        self.assertIs(body["models_ready"], True)
        self.assertEqual(set(body["routes"]),
                         {"tts", "image_generate", "image_edit", "video"})
        self.assertTrue(all(not r["implemented"] and not r["available"]
                            for r in body["routes"].values()))
        self.assertEqual(body["errors"], [])

    def test_health_503_with_structured_errors_when_model_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_model_tree(root, include=("tts", "image"))
            client = FakeClient(object_info=object_info_fixture())
            with TestClient(self.make_app(root, client)) as tc:
                response = tc.get("/health")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertIs(body["ready"], False)
        self.assertNotEqual(body["errors"], [])
        for error in body["errors"]:
            self.assertEqual(set(error), {"code", "scope", "message", "action"})

    def test_creating_the_app_makes_no_network_request_and_does_not_read_real_env(self):
        def never(url):
            raise AssertionError("create_app must not contact ComfyUI")

        with mock.patch.object(server, "load_settings") as load:
            app = server.create_app(client_factory=never)
        load.assert_not_called()
        self.assertTrue(app.title)

    def test_default_client_factory_builds_a_real_client(self):
        client = server.default_client_factory("http://127.0.0.1:9188")
        try:
            self.assertEqual(client.base_url, "http://127.0.0.1:9188")
        finally:
            client.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_health.py' -v
```

Expected: ERROR with `ModuleNotFoundError: No module named 'local_gateway.server'`.

- [ ] **Step 3: Implement the server, start script and README**

`local-gateway/local_gateway/server.py`:

```python
"""FastAPI app factory and the GET /health route (P1's only route).

The factory takes the settings and the ComfyUI client factory as optional
arguments so tests inject fakes. With no settings argument, local-gateway/.env
is loaded lazily inside the request, so importing the module or creating an app
never reads the developer's real .env and never opens a socket. Generation
routes are added in P2-P4 and stay implemented=false in /health until then.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .comfyui import ComfyUIClient
from .config import GatewaySettings, load_settings
from .health import build_health_payload

ClientFactory = Callable[[str], ComfyUIClient]


def default_client_factory(base_url: str) -> ComfyUIClient:
    return ComfyUIClient(base_url)


def create_app(settings: GatewaySettings | None = None,
               client_factory: ClientFactory | None = None) -> FastAPI:
    app_settings = settings
    app_client_factory = client_factory if client_factory is not None else default_client_factory

    app = FastAPI(title="ccvt local gateway", version="0.1.0")

    @app.get("/health")
    def health() -> JSONResponse:
        # Plain def on purpose: Starlette runs blocking requests calls in a
        # threadpool, so slow ComfyUI checks never block the event loop.
        active_settings = app_settings if app_settings is not None else load_settings()
        payload, status_code = build_health_payload(active_settings, app_client_factory)
        return JSONResponse(content=payload, status_code=status_code)

    return app


app = create_app()
```

`local-gateway/start_gateway.bat`:

```bat
@echo off
rem Start the ccvt local gateway on loopback 8123.
rem Start ComfyUI (127.0.0.1:9188) first; the gateway only fronts it.
rem %~dp0 is this script's directory, so no cd is needed.
uv run --directory "%~dp0" uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123
```

`local-gateway/README.md`:

````markdown
# local-gateway

A loopback FastAPI gateway in front of a local ComfyUI install. This is the
P1 walking skeleton: `GET /health` only. The four generation routes
(`/tts/generate`, `/image/generate`, `/image/edit`, `/video/generate`) are
not implemented yet, and `/health` reports each of them as
`implemented: false`.

## Start order

1. Start ComfyUI first. It must answer on `127.0.0.1:9188`.
2. Then start the gateway:

   ```bat
   start_gateway.bat
   ```

   The script uses its own directory, so you can run it from anywhere. The
   gateway binds `127.0.0.1:8123`.

## Configure

Copy `.env.example` to `.env` in this directory and edit it:

```
COMFYUI_BASE_URL=http://127.0.0.1:9188
COMFYUI_MODELS_ROOT=E:/path/to/ComfyUI/models
```

Set `COMFYUI_MODELS_ROOT` to the absolute path of the `models` directory of
your ComfyUI install (forward slashes work). If `.env` already exists, edit
it instead of overwriting it. Process environment variables win over `.env`
values.

## Binding and security

- The gateway binds `127.0.0.1:8123` only and does not enable CORS.
- If you do not need LAN access, ComfyUI should also bind `127.0.0.1`
  (`--listen 127.0.0.1`). An install listening on `0.0.0.0` is not a
  recommended default.
- The transport code sets `trust_env=False` on every requests session, which
  disables environment proxies explicitly. `NO_PROXY` is not a substitute
  for this.
- There is no token auth because the service is loopback only. Any future
  non-loopback exposure needs a new auth and access-control design.

## When /health fails

Read `errors[].action` in the response body and follow it. The gateway
returns HTTP 503 with the full check results while anything is missing, and
200 once the infrastructure and the first-release models are in place.
`ready` never means the generation routes work: check each route's
`implemented` flag. The four routes stay `implemented: false` until their
real generation acceptance passes in later phases.

## Tests

```
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

The tests use fakes only: no real ComfyUI, no real `.env`, no real model
directories.
````

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all 53 gateway tests OK (11 config + 20 comfyui + 22 health/server).

- [ ] **Step 5: Run copy lint on the new README**

```bash
uv run tools/copy_lint.py local-gateway/README.md
```

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add local-gateway/local_gateway/server.py local-gateway/start_gateway.bat \
  local-gateway/README.md local-gateway/tests/test_health.py
git commit -m "FEAT: gateway /health FastAPI app, start script and README"
```

---

### Task 10: CI integration, roadmap regression sync, full dual-domain regression

**Files:**
- Modify: `.github/workflows/python-tests.yml`
- Modify: `.github/workflows/lint-python.yml`
- Modify: `docs/local-comfyui-provider-roadmap.md` (the unified regression block)

**Interfaces:**
- Consumes: Tasks 2 through 9 deliverables (both test domains must pass here).
- Produces: CI runs both test domains on `local-gateway/**` changes; ruff covers `local-gateway/`; the roadmap's unified regression block lists both unittest commands.

- [ ] **Step 1: Update `python-tests.yml`**

Replace the full file content with:

```yaml
name: Python tests

# The unit tests in tests/python (video jobs, qc.py, sync_check.py, pick_music.py, audition.py, ltx_extend.py)
# plus the local-gateway tests in local-gateway/tests (its own uv project).
# Lint Python only checks undefined names; this runs the tests themselves.
on:
  pull_request:
    paths:
      - 'tools/**'
      - 'lib/blender/**'
      - 'scripts/**'
      - 'tests/python/**'
      - 'local-gateway/**'
      - 'templates/concept-explainer-short/**'
      - 'kits/**'
      - 'skills/video-toolkit/**'
      - '_internal/toolkit-registry.json'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.github/workflows/python-tests.yml'
  push:
    branches:
      - main
    paths:
      - 'tools/**'
      - 'lib/blender/**'
      - 'scripts/**'
      - 'tests/python/**'
      - 'local-gateway/**'
      - 'templates/concept-explainer-short/**'
      - 'kits/**'
      - 'skills/video-toolkit/**'
      - '_internal/toolkit-registry.json'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.github/workflows/python-tests.yml'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  test:
    name: unittest
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install ffmpeg
        run: sudo apt-get update && sudo apt-get install -y --no-install-recommends ffmpeg

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Run tests
        run: uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v

      - name: Run gateway tests
        run: uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

- [ ] **Step 2: Update `lint-python.yml`**

Two precise edits. First, the comment line near the bottom of the header comment block:

```yaml
# Run the same check locally with:
#   uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
```

Second, the run command of the `Check for undefined names` step:

```yaml
        run: uvx ruff check --select F821,F811,F822,F823 --output-format github scripts/ tools/ local-gateway/
```

Everything else in the file stays as is.

- [ ] **Step 3: Sync the roadmap's unified regression block**

In `docs/local-comfyui-provider-roadmap.md`, find the block under "**P1 起每阶段统一回归**" and insert the gateway test command between the root unittest line and the ruff line, so the block becomes:

```bash
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
```

(Only the middle line is new; the ruff line already listed `local-gateway/`.)

- [ ] **Step 4: Run the full spec 12.3 regression suite locally**

```bash
set -euo pipefail
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py docs/local-comfyui-provider-p0-p1-design.md local-gateway/README.md
```

Expected: all tests OK, ruff reports no findings, copy lint exits 0.

- [ ] **Step 5: Verify nothing unexpected is staged, then commit**

```bash
git status --short
git add .github/workflows/python-tests.yml .github/workflows/lint-python.yml \
  docs/local-comfyui-provider-roadmap.md
git commit -m "FEAT: CI runs local-gateway tests and lints local-gateway code"
```

Expected: `git status` before the add shows only the three modified files.

---

### Task 11: Real-machine /health acceptance, local .env wiring, checkbox close-out

**Files:**
- Modify (local only, never committed): `local-gateway/.env`, repo root `.env`
- Modify: `docs/local-comfyui-provider-roadmap.md` (tick the P1 checkboxes)

**Interfaces:**
- Consumes: everything from Tasks 1 through 10; a running ComfyUI 0.45.0 on `127.0.0.1:9188` with the first-release model files in place.
- Produces: the spec 13 real-machine acceptance passing; the four `LOCAL_*` URLs present in the local root `.env`; P1 roadmap checkboxes ticked; final commits on `feat/local-comfyui-provider`.

Constraints for this task: the only real HTTP call is `GET /health`. Never POST a real `/prompt`. Do not move weights or stop ComfyUI to force failure cases; the 503 paths were already covered by the TestClient tests in Task 8/9.

- [ ] **Step 1: Create the gateway's local `.env` (uncommitted)**

```bash
if test -e local-gateway/.env; then
  echo ".env already exists: edit it instead"
else
  cp local-gateway/.env.example local-gateway/.env
fi
git check-ignore local-gateway/.env
```

Edit `local-gateway/.env`: keep `COMFYUI_BASE_URL=http://127.0.0.1:9188` and set `COMFYUI_MODELS_ROOT` to this machine's real absolute ComfyUI models directory (forward slashes, for example `E:/ugit/comfyui/models`). `git check-ignore` must print the path, proving it stays untracked.

- [ ] **Step 2: Start ComfyUI, then start the gateway**

In one terminal, start ComfyUI so it answers on `127.0.0.1:9188` (the existing local install; do not change its launch arguments). In a second terminal, from the repo root:

```bash
cmd //c "local-gateway\\start_gateway.bat"
```

Expected: uvicorn logs show `Uvicorn running on http://127.0.0.1:8123`. This also validates the start script. (Alternative if cmd wrapping misbehaves: `uv run --directory local-gateway uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123`.)

- [ ] **Step 3: Run the spec 13 acceptance block**

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
print("P1 real-machine /health acceptance: OK")
'
```

Expected: `P1 real-machine /health acceptance: OK`. If an assertion fails, read `errors[].action` in `scratch/local-comfyui/p1/health.json`, fix the environment (never the assertions), and rerun. Do not proceed while any check fails.

- [ ] **Step 4: Run the full regression suite again (spec 12.3)**

```bash
set -euo pipefail
uv run --extra youtube python -m unittest discover -s tests/python -p 'test_*.py' -v
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
uvx ruff check --select F821,F811,F822,F823 scripts/ tools/ local-gateway/
uv run tools/copy_lint.py docs/local-comfyui-provider-p0-p1-design.md local-gateway/README.md
```

Expected: everything green. Stop the gateway with Ctrl+C afterwards.

- [ ] **Step 5: Write the four LOCAL_* URLs into the local root `.env` (uncommitted)**

Open the repo root `.env` in an editor. Update the values if any of the four keys already exist; otherwise append. Leave every other line untouched (spec 6.2; root `.env.example` stays untouched until P5):

```ini
LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate
LOCAL_IMAGE_ENDPOINT_URL=http://127.0.0.1:8123/image/generate
LOCAL_IMAGE_EDIT_ENDPOINT_URL=http://127.0.0.1:8123/image/edit
LOCAL_VIDEO_ENDPOINT_URL=http://127.0.0.1:8123/video/generate
```

These endpoints are not implemented until P2+; configuring them now is expected and calling them would return 404.

```bash
git check-ignore .env
git status --short
```

Expected: `.env` is ignored; `git status` shows no new untracked files (scratch is ignored).

- [ ] **Step 6: Tick the P1 roadmap checkboxes**

In `docs/local-comfyui-provider-roadmap.md`: change every `- [ ]` to `- [x]` inside the section `### P1 链路骨架（walking skeleton）` (12 items). P0 was already checked in Task 1. Touch nothing in P2 through P5.

- [ ] **Step 7: Verify against the spec 14 completion checklist**

Confirm every line is true before committing:

- Real-machine `/health` ran (not only unit tests).
- All four routes still report `implemented=false` / `available=false` in the saved `health.json`.
- Both test domains ran (root and gateway), plus ruff and copy lint.
- The first DOCS commit contains the plan, the roadmap, the design spec and this implementation plan (done in Task 1).
- No `.env`, real model path or generated artifact is staged; `scratch/` stays untracked.

```bash
git status --short
```

Expected: only `docs/local-comfyui-provider-roadmap.md` modified.

- [ ] **Step 8: Commit the checkbox update**

```bash
git add docs/local-comfyui-provider-roadmap.md
git commit -m "DOCS: tick local ComfyUI provider P1 checkboxes after full acceptance"
```

- [ ] **Step 9: Final branch state check**

```bash
git log --oneline upstream/main..HEAD
git status --short --branch
```

Expected commits on `feat/local-comfyui-provider`, in order: the Task 1 `DOCS:` commit, the Task 2/3 `FEAT:` cloud_gpu commits, the Task 4 through 9 gateway `FEAT:` commits, the Task 10 CI `FEAT:` commit, and the Task 11 `DOCS:` checkbox commit. Clean working tree. Pushing and opening a PR are outside this plan's scope.

---

## Self-review record

- Spec coverage: P0 preflight/fork/branch/docs-commit (section 4) is Task 1; module and dependency boundaries (section 5) are the File Structure table and Tasks 4 through 9; config rules (section 6) are Task 4 plus the root `.env` step in Task 11; `_LOCAL_ENV_VARS`, dispatch, request rules and response/progress semantics (section 7) are Tasks 2 and 3; client methods, wait semantics and errors (section 8) are Tasks 5 and 6; registry entries and routes (section 9) are Task 7; health data flow, response structure and error codes (section 10) are Task 8; start script and README requirements (section 11) are Task 9; root and gateway test designs (section 12.1/12.2) are embedded in Tasks 2/3 and 4 through 9; CI and unified regression (section 12.3) are Task 10; real-machine acceptance (section 13) and delivery/close-out order with the not-complete checklist (section 14) are Task 11.
- Placeholder scan: no TBD/TODO/"add tests"/"similar to Task N" steps remain; every code step carries the full code.
- Signature consistency: `_call_local(payload, endpoint_url, tool_name, timeout, progress_label, progress)` matches the dispatch call in Task 3 and the fake harness in Task 3's tests; `load_settings(env, dotenv_path) -> GatewaySettings` matches Tasks 8 and 9 usage; `build_health_payload(settings, client_factory)` matches Task 9; `create_app(settings=None, client_factory=None)` matches the TestClient tests; registry field tuples match the fixture trees and assertions in Task 8.
- Main-agent review corrections: P0 preflight and checkbox timing were made explicit; invalid ports are rejected; single-node `/object_info` envelopes and the 0.45.0 dict input shape are covered by tests; per-node HTTP failures are distinct from missing nodes; app creation no longer reads real `.env`; README uses a four-backtick outer fence; expected totals are 27 root-local tests and 53 gateway tests.
