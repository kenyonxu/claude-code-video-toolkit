@echo off
rem Start the Confucius4-TTS worker on loopback 8765 in its isolated env.
rem E:\GitHub\_confucius4_tts_venv is cloned from the validated confuciustts
rem conda env and holds torch 2.7.0+cu128/transformers;
rem the gateway process itself never imports them. The worker resolves
rem ./checkpoints relative to the working directory, so launch it from the
rem repo root (the launcher picks the cwd once; the worker never chdirs).
cd /d "%~dp0.."
if not defined CONFUCIUS4_TTS_ROOT (
  echo Error: set CONFUCIUS4_TTS_ROOT to your local Confucius4-TTS checkout. 1>&2
  exit /b 2
)
rem The checkout path is process-local configuration, never a committed default.
"E:\GitHub\_confucius4_tts_venv\python.exe" "%~dp0local_gateway\confucius_worker.py" --root "%CONFUCIUS4_TTS_ROOT%" --host 127.0.0.1 --port 8765
