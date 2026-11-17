@echo off
rem Start the ccvt local gateway on loopback 8123.
rem Start ComfyUI (127.0.0.1:9188) first; the gateway only fronts it.
rem %~dp0 is this script's directory, so no cd is needed.
uv run --directory "%~dp0." uvicorn local_gateway.server:app --host 127.0.0.1 --port 8123
