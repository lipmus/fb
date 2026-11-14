"""Tes looping + impor/ekspor Excel.

Looping: buktikan putaran berjalan berulang, nilai 'round' tercatat per job, batas putaran
dihormati, dan putaran 2 tidak dimulai sebelum SEMUA akun menuntaskan putaran 1.
Excel: tulis genome -> baca ulang -> impor, tanpa menyentuh jaringan.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="faw-loop-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")
os.environ["FAW_IO_DIR"] = str(TMP / "io")

FAIL = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_site():
    ff = list(Path(os.environ["LOCALAPPDATA"], "hermes", "tools").glob("ffmpeg-*/bin/ffmpeg.exe")) or \
        list(Path(os.environ["LOCALAPPDATA"], "ms-playwright").glob("ffmpeg-*/ffmpeg*.exe"))
    www = TMP / "www"
    www.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ff[0]), "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=15:duration=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", str(www / "v.mp4")], check=True)
    body = ("<html><body style='margin:0'><video src='/v.mp4' autoplay muted playsinline "
            "style='width:320px'></video></body></html>")
    (www / "feed").write_text("<html><body>beranda</body></html>", encoding="utf-8")
    for i in range(3):
        (www / f"p{i}").write_text(body, encoding="utf-8")
    return www


def main():
    from app import crypto, runner as R
    from app.db import DB
    from app.gui import Api
    from app.io_paths import io_dir

    www = make_site()
    port = free_port()
    d = str(www)

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=d, **k)

        def guess_type(self, path):
            t = super().guess_type(path)
            # server uji: path tanpa ekstensi tetap HTML (Chrome akan mengunduh kalau octet-stream)
            return "text/html" if t == "application/octet-stream" else t

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    R.FB_HOSTS = R.FB_HOSTS + ("127.0.0.1",)
    R.FB_HOME = f"{base}/feed"

    db = DB(TMP / "t.db")
    accs = []
    for n in ("L1", "L2"):
        aid = db.add_account(n, "")
        db.update_account(aid, status="active")
        accs.append(db.account(aid))
    db.add_items([f"{base}/p0", f"{base}/p1"])
    db.set_setting("loop_enabled", 1)      # test mengaktifkan looping eksplisit
    db.set_setting("loop_count", 2)

    s = db.settings()
    s["default_duration"] = 2
    s["scroll_enabled"] = 0
    s["max_workers"] = 2
    s["retry_max"] = 1
    s["headless"] = 1
    s["loop_enabled"] = 1
    s["loop_count"] = 2
    s["loop_pause"] = 0

    print("\n== loop 2 putaran: 2 akun x 2 item x 2 putaran = 8 job ==")
    o = R.Orchestrator(db)
    ok, msg = o.start(accs, s)
    check("start berhasil", ok, msg)
    t0 = time.time()
    while time.time() - t0 < 120:
        st = o.status()
        if not st["running"] or all(w["done"] for w in st["workers"]):
            break
        time.sleep(1)
    dt = time.time() - t0
    o.stop(hard=True)

    rows = db.c.execute("SELECT round, result, COUNT(*) c FROM jobs GROUP BY round, result "
                        "ORDER BY round").fetchall()
    per_round = {}
    for r in rows:
        per_round.setdefault(r["round"], {})[r["result"]] = r["c"]
    print(f"       {dt:.1f}s -> job per putaran: {per_round}")
    check("8 job tercatat (2 putaran)", sum(r["c"] for r in rows) == 8,
          str(sum(r["c"] for r in rows)))
    check("putaran 1 punya 4 job", sum(per_round.get(1, {}).values()) == 4, str(per_round.get(1)))
    check("putaran 2 punya 4 job", sum(per_round.get(2, {}).values()) == 4, str(per_round.get(2)))
    check("semua job sukses", all(r["result"] == "done" for r in rows), str([dict(r) for r in rows]))
    check("berhenti sendiri setelah batas 2 putaran", all(w["done"] for w in o.status()["workers"])
          or not o.running)
    check("orkestrator tidak lagi running", not o.running)

    print("\n== urutan putaran: putaran 2 hanya mulai setelah putaran 1 tuntas ==")
    q = db.c.execute("SELECT round, MIN(started_at) a, MAX(finished_at) b FROM jobs "
                     "WHERE round IS NOT NULL GROUP BY round ORDER BY round").fetchall()
    if len(q) == 2:
        gap = q[1]["a"] - q[0]["b"]
        check("job putaran 2 mulai setelah job terakhir putaran 1 selesai", gap >= -1.0,
              f"selisih {gap:.2f}s")
    else:
        check("ada 2 putaran tercatat", False, str([dict(r) for r in q]))

    print("\n== looping mati: hanya 1 putaran ==")
    db.clear_queue()
    db.add_items([f"{base}/p0"])
    o2 = R.Orchestrator(db)
    ok2, _ = o2.start(accs, {**s, "loop_enabled": 0, "loop_count": 0})
    t0 = time.time()
    while time.time() - t0 < 90:
        st = o2.status()
        if not st["running"] or all(w["done"] for w in st["workers"]):
            break
        time.sleep(1)
    o2.stop(hard=True)
    n_rounds = len(db.c.execute("SELECT DISTINCT round FROM jobs WHERE round IS NOT NULL "
                                "AND result!='running'").fetchall())
    check("hanya 1 putaran dijalankan", n_rounds == 1, f"{n_rounds} putaran")

    print("\n== Excel: tulis -> baca -> impor ==")
    api = Api()
    api.db.clear_queue()
    r = api.refresh_xlsx("both")
    check("file Excel ditulis", r.get("ok") and Path(r["queue"]).exists(),
          str(r.get("queue")))
    check("folder io ada di folder project / override", io_dir().exists(), str(io_dir()))
    check("nama file antrean.xlsx", Path(r["queue"]).name == "antrean.xlsx",
          Path(r["queue"]).name)
    check("nama file akun.xlsx", Path(r["accounts"]).name == "akun.xlsx",
          Path(r["accounts"]).name)

    # isi workbook seperti yang akan dilakukan user di Excel
    from openpyxl import load_workbook
    wb = load_workbook(r["queue"])
    ws = wb.active
    check("header kolom benar", [c.value for c in ws[1]] == ["url", "durasi_detik", "status"],
          str([c.value for c in ws[1]]))
    ws.delete_rows(2, ws.max_row)
    ws.append(["https://www.facebook.com/watch/?v=111", 45, ""])
    ws.append(["https://www.facebook.com/reel/222", "", ""])
    ws.append(["https://youtube.com/watch?v=999", 10, ""])   # harus ditolak
    ws.append(["https://www.facebook.com/watch/?v=111", 10, ""])  # duplikat
    wb.save(r["queue"])

    res = api.import_queue_xlsx()
    print(f"       {res}")
    check("impor melaporkan sukses", res.get("ok"), str(res))
    items = api.db.queue()
    check("2 link valid masuk", len(items) == 2, str([i["url"] for i in items]))
    check("1 link non-FB ditolak", res.get("invalid") == 1, str(res.get("invalid")))
    check("durasi per link terbaca dari Excel",
          any(i["dur_min"] == 45 for i in items), str([(i["url"], i["dur_min"]) for i in items]))
    check("duplikat tidak dobel", len({i["url"] for i in items}) == len(items))

    print("\n== Excel: akun (tambah, duplikat dilewati) ==")
    wb = load_workbook(r["accounts"])
    ws = wb.active
    before = len(api.db.accounts())
    ws.append(["AkunExcel1", "ya", "", "http://user:pass@127.0.0.1:8080", ""])
    ws.append(["AkunExcel1", "ya", "", "", ""])          # duplikat, harus dilewati
    ws.append(["AkunExcel2", "tidak", "", "", ""])
    wb.save(r["accounts"])
    res2 = api.import_accounts_xlsx()
    print(f"       {res2}")
    accs_after = api.db.accounts()
    check("2 akun baru diimpor", res2.get("added") == 2, str(res2.get("added")))
    check("duplikat akun dilewati", len(accs_after) == before + 2,
          f"{before} -> {len(accs_after)}")
    a1 = [a for a in accs_after if a["name"] == "AkunExcel1"][0]
    a2 = [a for a in accs_after if a["name"] == "AkunExcel2"][0]
    check("proxy terbaca", a1["proxy"].endswith("127.0.0.1:8080"), a1["proxy"])
    check("kolom aktif 'tidak' -> akun nonaktif", a2["enabled"] == 0, str(a2["enabled"]))

    print("\n== isi file Excel ikut ter-update saat antrean berubah ==")
    api.add_links("https://www.facebook.com/watch/?v=333")
    wb = load_workbook(r["queue"])
    urls = [row[0] for row in wb.active.iter_rows(min_row=2, values_only=True)]
    check("link baru muncul di file Excel", any("333" in str(u) for u in urls), str(urls))

    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 55)
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("TES LOOP + EXCEL LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
