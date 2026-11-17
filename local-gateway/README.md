# local-gateway

A loopback FastAPI gateway in front of a local ComfyUI install. P1 provides
`GET /health`; P2 adds the first generation route, `POST /tts/generate`,
which supports voice clone only (mode `clone`, `mp3` or `wav`, reference
audio inline as `ref_audio_base64`, results inline as `audio_base64`).
Custom voice and voice design are not implemented on this gateway. The
image and video routes stay `implemented: false` until later phases.

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

## Tests

```
uv run --directory local-gateway python -m unittest discover -s tests -p 'test_*.py' -v
```

The tests use fakes only: no real ComfyUI, no real `.env`, no real model
directories.
