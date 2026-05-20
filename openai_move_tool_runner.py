import json
import os
import subprocess
import sys
from pathlib import Path

import requests

from hooks.provider_adapters import render_openai_tools

def _resolve_llmind_cache_file() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "LLMind" / "api_cache.json"
    return Path.home() / ".config" / "LLMind" / "api_cache.json"


def _load_openai_key_from_cache() -> str:
    cache_file = _resolve_llmind_cache_file()
    if not cache_file.exists():
        return ""

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return ""

    # Legacy single-key format: {"api_key": "..."}
    legacy = payload.get("api_key")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()

    # Multi-key format: {"api_keys": [{"label": "...", "key": "..."}, ...]}
    api_keys = payload.get("api_keys")
    if not isinstance(api_keys, list):
        return ""

    # Prefer a record labeled for OpenAI when present.
    for record in api_keys:
        if not isinstance(record, dict):
            continue
        label = str(record.get("label", "")).lower()
        key = record.get("key")
        if "openai" in label and isinstance(key, str) and key.strip():
            return key.strip()

    # Fallback: first non-empty key.
    for record in api_keys:
        if not isinstance(record, dict):
            continue
        key = record.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()

    return ""

# ---------- Config ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    OPENAI_API_KEY = _load_openai_key_from_cache()
    if OPENAI_API_KEY:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
MODEL = "gpt-4o-mini"  # OpenAI tool-compatible model
DRY_RUN = False         # Keep True to avoid real file move
RUN_LLMIND = True      # Set True to launch LLMind.py after model call

LLMIND_BASE_DIR = Path(os.environ.get("LLMIND_BASE_DIR", str(_resolve_llmind_cache_file().parent)))
SOURCE_FILE = str(Path(os.environ.get("LLMIND_SOURCE_FILE", str(LLMIND_BASE_DIR / "test" / "move.txt"))))
DEST_DIR = str(Path(os.environ.get("LLMIND_DEST_DIR", str(LLMIND_BASE_DIR))))
LLMIND_ENTRY = str(Path("main") / "LLMind.py")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set.")

# ---------- Tool schema ----------
tools = render_openai_tools()

# ---------- Prompt ----------
user_prompt = (
    f'Move the file "move.txt" inside {Path(SOURCE_FILE).parent} '
    f'to {DEST_DIR} directory.'
)

# ---------- OpenAI call ----------
payload = {
    "model": MODEL,
    "messages": [
        {
            "role": "system",
            "content": (
                "You are a strict file-operation planner. "
                "When file move intent is present, call move_file exactly once."
            ),
        },
        {"role": "user", "content": user_prompt},
    ],
    "tools": tools,
    "tool_choice": {"type": "function", "function": {"name": "move_file"}},
}

headers = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json",
}

resp = requests.post(
    "https://api.openai.com/v1/chat/completions",
    headers=headers,
    json=payload,
    timeout=60,
)
resp.raise_for_status()
data = resp.json()

print("Model raw response:")
print(json.dumps(data, indent=2))

# ---------- Extract tool call ----------
tool_calls = (
    data.get("choices", [{}])[0]
    .get("message", {})
    .get("tool_calls", [])
)

if not tool_calls:
    raise RuntimeError("No tool call returned.")

fn = tool_calls[0].get("function", {})
if fn.get("name") != "move_file":
    raise RuntimeError(f"Unexpected tool name: {fn.get('name')}")

args = json.loads(fn.get("arguments", "{}"))
source = args.get("source", SOURCE_FILE)
destination = args.get("destination", DEST_DIR)

print("\nParsed tool call:")
print(json.dumps({"source": source, "destination": destination}, indent=2))

# ---------- Safe execution block ----------
src_path = Path(source)
dst_dir = Path(destination)
dst_path = dst_dir / src_path.name

if DRY_RUN:
    print("\nDRY_RUN=True: no file operation performed.")
    print(f"Would move: {src_path} -> {dst_path}")
else:
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_path.replace(dst_path)
    print(f"\nMoved: {src_path} -> {dst_path}")

# ---------- Launch LLMind ----------
if RUN_LLMIND:
    print("\nLaunching LLMind...")
    subprocess.run([sys.executable, LLMIND_ENTRY], check=False)
