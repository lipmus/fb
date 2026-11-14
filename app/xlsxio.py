"""Impor/ekspor Excel (xlsx) untuk antrean & daftar akun.

File ditaruh di folder project (lihat io_paths), jadi tidak berpindah-pindah tempat.
Baca: openpyxl. Tulis: openpyxl (sudah jadi dependensi; tidak tambah pustaka baru).
"""
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(color="FFFFFF", bold=True)
QUEUE_COLS = ["url", "durasi_detik", "status"]
ACC_COLS = ["nama", "aktif", "status_sesi", "proxy", "catatan"]


def _sheet(path, cols, rows, widths=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([r.get(k) for k in cols])
    for i, c in enumerate(cols, start=1):
        w = (widths or {}).get(c) or max(12, min(60, len(c) + 4))
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return Path(path)


def write_queue(path, items):
    """items: list dict dengan kunci url, durasi_detik, status."""
    rows = [{"url": i["url"],
             "durasi_detik": (i.get("dur_min") or i.get("dur_max") or ""),
             "status": i.get("status", "")} for i in items]
    # kolom url dilebarkan; file excel butuh lebar eksplisit, bukan auto-fit
    return _sheet(path, QUEUE_COLS, rows, {"url": 70, "durasi_detik": 14, "status": 14})


def write_accounts(path, accounts):
    rows = [{"nama": a["name"],
             "aktif": "ya" if a["enabled"] else "tidak",
             "status_sesi": a["status"],
             "proxy": a.get("proxy", ""),
             "catatan": a.get("last_error", "")} for a in accounts]
    return _sheet(path, ACC_COLS, rows, {"nama": 24, "aktif": 8, "status_sesi": 18,
                                         "proxy": 26, "catatan": 60})


def read_rows(path, needed):
    """Baca sheet pertama jadi list dict. Kolom dicocokkan longgar (huruf kecil, tanpa spasi)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"file tidak ada: {p}")
    wb = load_workbook(str(p), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    def norm(s):
        return str(s or "").strip().lower().replace(" ", "_")
    head = [norm(c) for c in rows[0]]
    idx = {}
    for want in needed:
        for i, h in enumerate(head):
            if h == want or h.startswith(want[:5]):
                idx[want] = i
                break
    out = []
    for r in rows[1:]:
        if r is None or all(c is None or str(c).strip() == "" for c in r):
            continue
        out.append({k: (r[i] if i < len(r) else None) for k, i in idx.items()})
    return out
