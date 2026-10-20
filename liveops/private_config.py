"""Explicit opt-in loading of local credentials kept outside the repository."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


SETTINGS_PATH = Path.home() / ".config/liveops-harness/deepseek.env"
ALLOWED = {"LIVEOPS_LLM_BASE_URL", "LIVEOPS_LLM_API_KEY", "LIVEOPS_LLM_MODEL",
           "LIVEOPS_SPECIALIST_MODEL", "LIVEOPS_LLM_THINKING"}


def load_private_settings(path: Path = SETTINGS_PATH):
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Private settings must not be accessible to group or other users")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in ALLOWED:
            raise ValueError("Unsupported private setting")
        os.environ.setdefault(key, value)


def request_options():
    thinking = os.getenv("LIVEOPS_LLM_THINKING", "")
    if thinking and thinking not in {"enabled", "disabled"}:
        raise ValueError("LIVEOPS_LLM_THINKING must be enabled or disabled")
    return {"thinking": {"type": thinking}} if thinking else {}


if __name__ == "__main__":
    load_private_settings()
    if not sys.argv[1:]:
        raise SystemExit("Provide Python arguments, for example -m uvicorn liveops.api:app")
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
