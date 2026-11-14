"""Cek UI tanpa GUI: buka index.html di Chrome, pastikan tidak ada error JS.

Kelas bug yang ditangkap: JS menyentuh elemen yang tidak ada di HTML -> TypeError ->
seluruh handler setelah baris itu tidak pernah terpasang dan UI jadi mati tanpa pesan.
Ini pernah kejadian (tombol b-export dihapus dari HTML tapi masih dirujuk JS).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HTML = ROOT / "app" / "static" / "index.html"

FAIL = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def main():
    src = HTML.read_text(encoding="utf-8")
    head, _, js = src.partition("<script>")

    ids = set(re.findall(r'id="([^"]+)"', head))
    refs = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
    missing = sorted(r for r in refs if r not in ids)
    # b-resume memang opsional (dipakai dengan pola $() && ...)
    missing = [m for m in missing if m != "b-resume"]
    check("semua elemen yang dirujuk JS ada di HTML", not missing, str(missing))

    called = set(re.findall(r"(?:call|api\.\w+)\(\s*'([a-z_]+)'", js))
    from app.gui import Api
    api = Api()
    absent = sorted(c for c in called if not hasattr(api, c))
    check("semua call(...) ada di Api backend", not absent, str(absent))

    tabs = set(re.findall(r'data-tab="(\w+)"', head))
    sections = set(re.findall(r'id="tab-(\w+)"', head))
    check("tab & section cocok", tabs == sections, str(tabs ^ sections))

    from playwright.sync_api import sync_playwright
    errs = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        page = b.new_page()
        page.on("pageerror", lambda e: errs.append(f"PAGEERROR {e}"))
        page.on("console", lambda m: errs.append(f"CONSOLE {m.text}")
                if m.type == "error" else None)
        page.goto(HTML.as_uri())
        page.wait_for_timeout(2500)
        b.close()
    # tanpa bridge, boot() memang menampilkan toast 'Bridge UI tidak tersedia' - itu wajar
    real = [e for e in errs if "Bridge UI" not in e]
    check("tidak ada error JS saat halaman dimuat", not real, str(real[:3]))

    print("\n" + ("=" * 45))
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("TES UI JS LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
