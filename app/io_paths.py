"""Folder kerja project: tempat file impor/ekspor Excel ditulis & dibaca.

Default: folder project ini sendiri (root repo). Kalau tidak bisa ditulis (mis. app
dipasang read-only), jatuh ke folder data aplikasi supaya tetap jalan.
Set FAW_IO_DIR untuk memaksa lokasi lain.
"""
import os
import tempfile
from pathlib import Path

from .paths import data_dir

ROOT = Path(__file__).resolve().parent.parent


def io_dir() -> Path:
    override = os.environ.get("FAW_IO_DIR")
    if override:
        p = Path(override)
    elif os.environ.get("FAW_DATA_DIR"):
        # sesi tes: jangan tulis ke repo
        p = data_dir() / "io"
    else:
        p = ROOT / "io"
        try:
            p.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=p, prefix=".w", delete=True):
                pass
        except Exception:
            p = data_dir() / "io"
    p.mkdir(parents=True, exist_ok=True)
    return p


QUEUE_XLSX = "antrean.xlsx"
ACCOUNTS_XLSX = "akun.xlsx"


def queue_path() -> Path:
    return io_dir() / QUEUE_XLSX


def accounts_path() -> Path:
    return io_dir() / ACCOUNTS_XLSX
