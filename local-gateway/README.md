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
