param(
  [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path $Python)) {
  Write-Host "Python executable not found: $Python"
  Write-Host "Run .\scripts\run.ps1 first, or pass -Python with the venv Python path."
  exit 1
}

@'
import os
import sys
from pathlib import Path

from nicheflow_studio.core.env import load_dotenv
from nicheflow_studio.processing.smart_drafts import (
    DEFAULT_GROQ_MODEL,
    DEFAULT_GROQ_VISION_MODEL,
    can_generate_smart_drafts,
    _groq_vision_enabled,
    _resolve_provider_order,
)

dotenv_path = Path.cwd() / ".env"
load_dotenv(dotenv_path)

providers = _resolve_provider_order(model=None, api_key=None)

print("NicheFlow AI setup")
print(f"- Python: {sys.version.split()[0]} ({sys.executable})")
print(f"- .env: {'found' if dotenv_path.exists() else 'missing'}")
print(f"- GROQ_API_KEY: {'configured' if os.environ.get('GROQ_API_KEY') else 'missing'}")
print(f"- GROQ_MODEL: {os.environ.get('GROQ_MODEL') or DEFAULT_GROQ_MODEL}")
print(f"- GROQ_ENABLE_VISION: {'enabled' if _groq_vision_enabled() else 'disabled'}")
print(f"- GROQ_VISION_MODEL: {os.environ.get('GROQ_VISION_MODEL') or DEFAULT_GROQ_VISION_MODEL}")
print(f"- OLLAMA_DISABLED: {os.environ.get('OLLAMA_DISABLED') or '(not set)'}")
print(f"- Smart drafts available by config: {can_generate_smart_drafts()}")

if providers:
    print("- Provider order:")
    for provider, model, api_key in providers:
        key_state = "key configured" if api_key else "no key needed"
        print(f"  - {provider}: {model} ({key_state})")
else:
    print("- Provider order: none")

if not os.environ.get("GROQ_API_KEY"):
    print("")
    print("Next step: copy .env.example to .env and set GROQ_API_KEY.")
if "OLLAMA_DISABLED" not in os.environ:
    print("")
    print("Note: Ollama fallback is enabled by default. Set OLLAMA_DISABLED=1 if you are only using Groq.")
'@ | & $Python -
