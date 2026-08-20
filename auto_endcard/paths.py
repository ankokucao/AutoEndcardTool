from __future__ import annotations

import shutil
import sys
from pathlib import Path


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def find_binary(name: str) -> str | None:
    executable_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    bundled = application_dir() / "tools" / executable_name
    if bundled.is_file():
        return str(bundled)
    return shutil.which(name) or shutil.which(executable_name)
