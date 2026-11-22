#!/usr/bin/env python3
"""
Generate speech using Confucius4-TTS on the local gateway (voice clone only).

The gateway route is clone-only: a reference clip plus a language code.
There is no ref_text, no built-in speaker, no instruct, no sampling knobs
and no R2 -- the reference is sent inline as base64 and the result comes
back inline as base64.

Usage:
    uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \
      --lang zh --text "这段声音由本地 Confucius4-TTS 克隆生成。" \
      --ref-audio C:/Users/kenyo/Downloads/Music/voice_02.mp3 \
      --format mp3 --output out.mp3

Requires the local stack: the Confucius worker (8765) and the gateway
(8123), plus LOCAL_TTS_ENDPOINT_URL=http://127.0.0.1:8123/tts/generate in
the root .env.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

MODEL = "confucius4-tts"
SUPPORTED_LANGUAGES = ["zh", "en", "ja", "ko", "de", "fr", "es", "id",
                       "it", "ms", "pt", "ru", "th", "vi"]
MAX_TEXT_CHARS = 1024
MAX_BATCH_ITEMS = 8


def get_audio_duration(file_path: str) -> float | None:
    """Audio duration in seconds via ffprobe, or None."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, shell=False)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, ValueError):
        pass
    return None


def generate_audio(
    text,                       # str OR list[str] for batch
    output_path,                # str OR list[str] (same shape as text)
    lang: str = "zh",
    ref_audio: str | None = None,
    output_format: str = "mp3",
    timeout: int = 300,
    model: str | None = MODEL,
    cloud: str = "local",
    verbose: bool = True,
    max_wpm: float | None = None,
    progress=None,
) -> dict:
    """Voice-clone one text (or a <=8 item batch) on the local gateway.

    Returns dict with:
      single call: {success, output, duration_seconds, duration_frames_30fps, script_chars, wpm, pacing}
      batch call:  {success, outputs: [{output, duration_seconds, ...}, ...], mode: clone}
    """
    is_batch = isinstance(text, list)
    texts = text if is_batch else [text]
    out_paths = output_path if is_batch else [output_path]
    if len(out_paths) != len(texts):
        return {"success": False, "error": "text and output_path must have matching lengths"}

    if cloud != "local":
        return {"success": False, "error": (
            "confucius4_tts runs on the local gateway only: pass"
            f" --cloud local (requested cloud: {cloud})")}
    if model is not None and model != MODEL:
        return {"success": False, "error": (
            f"unknown model {model!r}; this tool only accepts {MODEL}")}
    if not ref_audio:
        return {"success": False, "error": (
            "--ref-audio is required: the local route is voice clone only"
            " (no built-in speakers, no ref_text)")}
    ref_path = Path(ref_audio)
    if not ref_path.exists():
        return {"success": False, "error": f"Reference audio not found: {ref_audio}"}
    if lang not in SUPPORTED_LANGUAGES:
        return {"success": False, "error": (
            f"--lang must be one of {', '.join(SUPPORTED_LANGUAGES)}; got {lang!r}")}
    for i, item in enumerate(texts):
        if not isinstance(item, str) or not item.strip():
            return {"success": False, "error": f"text[{i}] must be a non-empty string"}
        if len(item) > MAX_TEXT_CHARS:
            return {"success": False, "error": (
                f"text[{i}] exceeds {MAX_TEXT_CHARS} characters (got {len(item)})")}
    if not 1 <= len(texts) <= MAX_BATCH_ITEMS:
        return {"success": False, "error": (
            f"batch must have 1..{MAX_BATCH_ITEMS} items, got {len(texts)}")}
    if output_format not in ("mp3", "wav"):
        return {"success": False, "error": (
            f"--format must be mp3 or wav, got {output_format!r}")}

    if verbose:
        print(f"Cloud provider: {cloud} (Confucius4-TTS voice clone,"
              f" {len(texts)} utterance{'s' if len(texts) > 1 else ''})",
              file=sys.stderr)

    payload = {
        "input": {
            "text": texts if is_batch else texts[0],
            "mode": "clone",
            "language": lang,
            "output_format": output_format,
            "ref_audio_base64": base64.b64encode(
                ref_path.read_bytes()).decode("ascii"),
        }
    }
    if model is not None:
        payload["input"]["model"] = model

    from cloud_gpu import call_cloud_endpoint

    effective_timeout = timeout * max(1, len(texts) // 3 + 1)
    output, _elapsed = call_cloud_endpoint(
        provider=cloud,
        payload=payload,
        tool_name="confucius4_tts",
        timeout=effective_timeout,
        poll_interval=3,
        progress_label="Cloning voice locally",
        verbose=verbose,
        progress=progress,
    )

    if isinstance(output, dict) and output.get("error"):
        return {"success": False, "error": output["error"]}

    outputs_list = None
    if isinstance(output, dict) and isinstance(output.get("outputs"), list):
        outputs_list = output["outputs"]
    if not outputs_list or len(outputs_list) != len(texts):
        return {"success": False, "error": (
            f"Expected {len(texts)} output(s), got"
            f" {len(outputs_list) if outputs_list else 0}:"
            f" {list(output.keys()) if isinstance(output, dict) else output}")}

    downloaded_items = []
    for i, (item, dest_path) in enumerate(zip(outputs_list, out_paths)):
        # Local results must be base64-only; any external reference is a
        # contract violation, not a download source.
        if item.get("r2_key") or item.get("audio_url"):
            return {"success": False, "error": (
                f"local gateway returned an external reference"
                f" (r2_key/audio_url) for output[{i}]; local results must"
                " be base64-only")}
        audio_base64 = item.get("audio_base64")
        if not audio_base64:
            return {"success": False, "error": (
                f"No audio_base64 in local output[{i}]:"
                f" {list(item.keys())}")}
        destination = Path(dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(audio_base64))
        if verbose:
            size_kb = destination.stat().st_size // 1024
            print(f"  Decoded from base64: {dest_path} ({size_kb}KB)",
                  file=sys.stderr)
        duration = get_audio_duration(dest_path)
        item_result = {
            "output": dest_path,
            "duration_seconds": round(duration, 2) if duration else None,
            "duration_frames_30fps": int(duration * 30) if duration else None,
            "script_chars": len(texts[i]),
        }
        from pacing import clamp_pace, pace_label
        if max_wpm:
            clamp = clamp_pace(dest_path, texts[i], max_wpm, verbose=verbose)
            if clamp.get("applied"):
                new_dur = clamp["duration_seconds"]
                item_result["duration_seconds"] = new_dur
                item_result["duration_frames_30fps"] = (
                    int(new_dur * 30) if new_dur else None)
                item_result["pace_adjusted"] = {
                    "original_wpm": clamp["original_wpm"],
                    "atempo": clamp["atempo"],
                    "floored": clamp["floored"],
                }
            elif clamp.get("error") and verbose:
                print(f"  Pace clamp skipped: {clamp['error']}", file=sys.stderr)
        wpm, label = pace_label(texts[i], item_result["duration_seconds"])
        item_result["wpm"] = wpm
        item_result["pacing"] = label
        downloaded_items.append(item_result)

    if is_batch:
        return {"success": True, "outputs": downloaded_items, "mode": "clone"}
    return {"success": True, **downloaded_items[0], "mode": "clone"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate speech with Confucius4-TTS on the local"
                    " gateway (voice clone only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run tools/confucius4_tts.py --cloud local --model confucius4-tts \\
    --lang zh --text "你好，世界" --ref-audio ref.mp3 --output out.mp3
  uv run tools/confucius4_tts.py --lang en --format wav --ref-audio ref.mp3 \\
    --text "Hello" --output out.wav --timeout 420
        """,
    )
    parser.add_argument("--text", "-t", type=str, required=True,
                        help="Text to synthesize")
    parser.add_argument("--output", "-o", type=str, required=True,
                        help="Output audio file path (.mp3 or .wav)")
    parser.add_argument("--cloud", type=str, default="local",
                        choices=["local"],
                        help="Local backend (default and only: local)")
    parser.add_argument("--model", type=str, default=MODEL,
                        help=f"Model name (only {MODEL} is accepted)")
    parser.add_argument("--lang", type=str, default="zh",
                        choices=SUPPORTED_LANGUAGES,
                        help="Language code (default: zh)")
    parser.add_argument("--ref-audio", type=str,
                        help="Reference audio file for voice cloning"
                             " (required; no ref_text needed)")
    parser.add_argument("--format", type=str, default="mp3",
                        choices=["mp3", "wav"],
                        help="Output format (default: mp3)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Job timeout in seconds (default: 300;"
                             " batches scale it by count)")
    parser.add_argument("--max-wpm", type=float, default=None,
                        help="Pace clamp: slow takes exceeding this"
                             " words-per-minute (see tools/pacing.py)")
    parser.add_argument("--min-tempo", type=float, default=None,
                        help="Stretch floor for --max-wpm (default 0.85)")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON")
    return parser.parse_args()


def main():
    args = parse_args()
    verbose = not args.json
    if args.min_tempo is not None:
        import pacing
        try:
            pacing.set_min_tempo(args.min_tempo)
        except ValueError as e:
            print(f"Error: --min-tempo: {e}", file=sys.stderr)
            sys.exit(1)

    if args.model != MODEL:
        print(f"Error: unknown model {args.model!r}; this tool only"
              f" accepts {MODEL}", file=sys.stderr)
        sys.exit(1)
    if not args.ref_audio:
        print("Error: --ref-audio is required (voice clone only)",
              file=sys.stderr)
        sys.exit(1)
    if not Path(args.ref_audio).exists():
        print(f"Error: Reference audio not found: {args.ref_audio}",
              file=sys.stderr)
        sys.exit(1)

    progress = None
    if not args.json:
        from cloud_gpu import ProgressReporter
        progress = ProgressReporter(mode="human")

    result = generate_audio(
        text=args.text,
        output_path=args.output,
        lang=args.lang,
        ref_audio=args.ref_audio,
        output_format=args.format,
        timeout=args.timeout,
        model=args.model,
        cloud=args.cloud,
        verbose=verbose,
        max_wpm=args.max_wpm,
        progress=progress,
    )

    if not result.get("success"):
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Error: {result.get('error', 'Unknown error')}",
                  file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        duration = result.get("duration_seconds", 0)
        print(f"Generated: {result['output']}")
        if duration:
            print(f"  Duration: {duration:.1f}s ({int(duration * 30)}"
                  " frames @ 30fps)")


if __name__ == "__main__":
    main()
