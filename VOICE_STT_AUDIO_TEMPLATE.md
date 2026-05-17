# Voice Recording and Audio Prompt Template

This guide provides a practical CLI-first setup for:

1. Recording voice from microphone
2. Clipping audio segments
3. Converting speech to text (STT)
4. Sending audio clips to audio-enabled LLM APIs

The examples prioritize widely used APIs and payload schemas.

## 1) Recommended Stack

- Recording and clipping: `ffmpeg`
- Python audio helpers: `sounddevice`, `soundfile`
- Local STT (offline): `faster-whisper`
- Cloud STT / audio LLM:
  - OpenAI audio transcription and Responses API
  - Google Gemini multimodal API
  - Deepgram STT API

## 2) Install (Windows PowerShell)

```powershell
pip install sounddevice soundfile numpy requests faster-whisper
```

Install `ffmpeg` and ensure it is on PATH.

Check:

```powershell
ffmpeg -version
```

## 3) Common Audio Constraints

- Preferred format for upload: `wav` or `mp3`
- Keep clips short for low latency: 5-30 seconds
- Typical safe microphone settings:
  - sample rate: `16000` or `24000`
  - mono: 1 channel
  - PCM 16-bit WAV for best compatibility

## 4) API Schemas (Popular First)

### A) OpenAI Speech to Text (Transcription)

Endpoint: `POST /v1/audio/transcriptions` (multipart form)

Required fields:
- `file`: binary audio file
- `model`: e.g. `gpt-4o-mini-transcribe` or `whisper-1`

Example (Python requests):

```python
files = {
    "file": open("clip.wav", "rb"),
}
data = {
    "model": "gpt-4o-mini-transcribe",
}
headers = {"Authorization": f"Bearer {api_key}"}
r = requests.post("https://api.openai.com/v1/audio/transcriptions", headers=headers, files=files, data=data, timeout=60)
print(r.json())
```

### B) OpenAI Responses API with Audio Input

Endpoint: `POST /v1/responses`

JSON shape (base64 inline audio):

```json
{
  "model": "gpt-4o-mini",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_audio",
          "input_audio": {
            "data": "<BASE64_AUDIO>",
            "format": "wav"
          }
        },
        {
          "type": "input_text",
          "text": "Summarize this voice note in 3 bullets"
        }
      ]
    }
  ]
}
```

### C) OpenAI Responses API with Audio Output

```json
{
  "model": "gpt-4o-audio-preview",
  "modalities": ["text", "audio"],
  "audio": {
    "voice": "alloy",
    "format": "wav"
  },
  "input": "Read this response out loud in a calm tone."
}
```

### D) Gemini Multimodal Input (Audio Inline)

Endpoint pattern: `POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent?key=<API_KEY>`

```json
{
  "contents": [
    {
      "parts": [
        {"text": "Transcribe and summarize this clip"},
        {
          "inline_data": {
            "mime_type": "audio/wav",
            "data": "<BASE64_AUDIO>"
          }
        }
      ]
    }
  ]
}
```

### E) Deepgram STT (Pre-recorded)

Endpoint: `POST /v1/listen`

Headers:
- `Authorization: Token <DEEPGRAM_API_KEY>`
- `Content-Type: audio/wav` (or your mime)

Body: raw audio bytes

## 5) CLI Audio Workflow Template

Use this single-file Python template for recording, clipping, and either local or API transcription.

```python
import argparse
import base64
import os
import subprocess
import tempfile
from pathlib import Path

import requests
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel


def record_wav(path: Path, seconds: float, sample_rate: int = 16000, channels: int = 1) -> None:
    frames = int(seconds * sample_rate)
    audio = sd.rec(frames, samplerate=sample_rate, channels=channels, dtype="float32")
    sd.wait()
    sf.write(str(path), audio, sample_rate)


def clip_audio_ffmpeg(input_path: Path, output_path: Path, start_sec: float, duration_sec: float) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start_sec),
        "-i",
        str(input_path),
        "-t",
        str(duration_sec),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def transcribe_local_whisper(audio_path: Path, model_name: str = "small") -> str:
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


def transcribe_openai(audio_path: Path, api_key: str, model: str = "gpt-4o-mini-transcribe") -> str:
    headers = {"Authorization": f"Bearer {api_key}"}
    with open(audio_path, "rb") as f:
        files = {"file": f}
        data = {"model": model}
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
            timeout=90,
        )
    r.raise_for_status()
    payload = r.json()
    return payload.get("text", "")


def send_audio_to_openai_responses(audio_path: Path, api_key: str, prompt: str, model: str = "gpt-4o-mini") -> dict:
    b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": "wav"},
                    },
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=body, timeout=90)
    r.raise_for_status()
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Record, clip, transcribe, and send audio prompts.")
    parser.add_argument("--record-seconds", type=float, default=8.0)
    parser.add_argument("--clip-start", type=float, default=0.0)
    parser.add_argument("--clip-duration", type=float, default=8.0)
    parser.add_argument("--mode", choices=["local-stt", "openai-stt", "openai-audio-prompt"], default="local-stt")
    parser.add_argument("--prompt", default="Transcribe and summarize this audio")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--openai-model", default="gpt-4o-mini")
    parser.add_argument("--transcribe-model", default="gpt-4o-mini-transcribe")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_path = out_dir / "recording_raw.wav"
    clip_path = out_dir / "recording_clip.wav"

    print(f"Recording {args.record_seconds}s to {raw_path}...")
    record_wav(raw_path, args.record_seconds)

    print(f"Clipping audio: start={args.clip_start}, duration={args.clip_duration}...")
    clip_audio_ffmpeg(raw_path, clip_path, args.clip_start, args.clip_duration)

    if args.mode == "local-stt":
        text = transcribe_local_whisper(clip_path)
        print("\n[TRANSCRIPT]\n" + text)
        return 0

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    if args.mode == "openai-stt":
        text = transcribe_openai(clip_path, api_key, model=args.transcribe_model)
        print("\n[TRANSCRIPT]\n" + text)
        return 0

    response = send_audio_to_openai_responses(
        clip_path,
        api_key,
        prompt=args.prompt,
        model=args.openai_model,
    )
    print("\n[OPENAI RESPONSES PAYLOAD]\n", response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

## 6) Example CLI Commands

Local STT:

```powershell
python audio_cli_template.py --mode local-stt --record-seconds 10 --clip-start 2 --clip-duration 6 --out-dir .\audio_out
```

OpenAI transcription:

```powershell
$env:OPENAI_API_KEY="your_key_here"
python audio_cli_template.py --mode openai-stt --record-seconds 8 --out-dir .\audio_out
```

Audio clip to audio-enabled LLM:

```powershell
$env:OPENAI_API_KEY="your_key_here"
python audio_cli_template.py --mode openai-audio-prompt --openai-model gpt-4o-mini --prompt "Summarize action items" --out-dir .\audio_out
```

## 7) Integration Tip for LLMind

If you integrate this into LLMind hooks, keep the contract narrow:

- `record_audio` hook: writes a WAV artifact path
- `clip_audio` hook: takes source path, start, duration; returns clipped path
- `transcribe_audio` hook: returns plain text only
- `send_audio_prompt` hook: returns model response text and optional audio artifact reference

This keeps token usage low and avoids pushing large base64 blobs through model context unless required.
