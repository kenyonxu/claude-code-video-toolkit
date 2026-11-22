@echo off
rem Start the Confucius4-TTS worker on loopback 8765 in its isolated env.
rem E:\GitHub\_confucius4_tts_venv is a conda prefix cloned from the validated
rem confuciustts env and holds torch 2.7.0+cu128/transformers; its python.exe
rem sits at the prefix root. The gateway process itself never imports them.
rem The worker only reads the checkout: it loads a temporary copy of the
rem upstream inference config with absolute checkpoint paths, so it needs no
rem junction and no particular working directory. -B keeps imports from
rem writing __pycache__ into the checkout.
cd /d "%~dp0.."
if not defined CONFUCIUS4_TTS_ROOT (
  echo Error: set CONFUCIUS4_TTS_ROOT to your local Confucius4-TTS checkout. 1>&2
  exit /b 2
)
rem The checkout path is process-local configuration, never a committed default.
"E:\GitHub\_confucius4_tts_venv\python.exe" -B "%~dp0local_gateway\confucius_worker.py" --root "%CONFUCIUS4_TTS_ROOT%" --host 127.0.0.1 --port 8765
