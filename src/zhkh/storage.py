from __future__ import annotations

from datetime import datetime
from pathlib import Path


def receipts_dir(site: str, base: Path | None = None, when: datetime | None = None) -> Path:
    base = base or Path.cwd() / "receipts"
    when = when or datetime.now()
    out = base / when.strftime("%Y-%m") / site
    out.mkdir(parents=True, exist_ok=True)
    return out


def debug_dir(site: str, base: Path | None = None, when: datetime | None = None) -> Path:
    base = base or Path.cwd() / "debug"
    when = when or datetime.now()
    out = base / when.strftime("%Y-%m-%d_%H%M%S") / site
    out.mkdir(parents=True, exist_ok=True)
    return out
