"""Lokasi data aplikasi (sesi, log, db)."""
import os
from pathlib import Path

APP_NAME = "FacebookAutoWatch"


def data_dir() -> Path:
    override = os.environ.get("FAW_DATA_DIR")
    if override:
        p = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
        p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    (p / "sessions").mkdir(exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "app.db"


def session_path(dd, account_id) -> Path:
    return Path(dd) / "sessions" / f"account_{account_id}.enc"
