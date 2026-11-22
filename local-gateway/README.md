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

   The prefix is a conda environment, so its interpreter is
   `E:\GitHub\_confucius4_tts_venv\python.exe`. The worker only reads the
   checkout: it loads a temporary copy of the upstream inference config whose
   `tokenizer_path` and `w2v_stat` lines point at
   `<CONFUCIUS4_TTS_ROOT>/checkpoints`. It therefore needs no junction and
   no particular working directory, and the launcher passes `-B` so that
   imports write no bytecode into the checkout.

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
