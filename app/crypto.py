"""Enkripsi sesi (cookies) di disk pakai DPAPI Windows.

Password tidak pernah disimpan - hanya storage_state Playwright (cookies+localStorage),
dienkripsi dengan kunci milik user Windows (CryptProtectData).
"""
import ctypes
import ctypes.wintypes as wt
from pathlib import Path

MAGIC = b"DPAPI1\n"


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(data: bytes, unprotect: bool) -> bytes:
    if not hasattr(ctypes, "windll"):
        raise RuntimeError("Enkripsi sesi hanya didukung di Windows (DPAPI).")
    cin, _keep = _blob(data)
    cout = _BLOB()
    fn = ctypes.windll.crypt32.CryptUnprotectData if unprotect else ctypes.windll.crypt32.CryptProtectData
    ok = fn(ctypes.byref(cin), None, None, None, None, 0, ctypes.byref(cout))
    if not ok:
        raise OSError(f"CryptProtectData/Unprotect gagal (err {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(cout.pbData, cout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(cout.pbData)


def save(path: Path, plaintext: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MAGIC + _crypt(plaintext.encode("utf-8"), unprotect=False))


def load(path: Path):
    path = Path(path)
    if not path.exists():
        return None
    raw = path.read_bytes()
    if not raw.startswith(MAGIC):
        raise ValueError(f"{path.name}: format sesi tidak dikenali")
    return _crypt(raw[len(MAGIC):], unprotect=True).decode("utf-8")


def wipe(path: Path):
    p = Path(path)
    if p.exists():
        p.write_bytes(b"")
        p.unlink()
