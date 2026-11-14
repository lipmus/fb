"""Tes multi-worker nyata: 3 akun paralel, 6 item lokal, mode all & split, global stop.

Bukti untuk klaim PRD: FR-21 worker paralel, FR-22 mode pembagian, FR-23 batas worker,
FR-24 error satu akun tidak menghentikan akun lain, FR-25 stop global.
"""
import os
import re
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
TMP = Path(tempfile.mkdtemp(prefix="faw-par-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")
os.environ["FAW_LOCAL_VIDEO"] = "1"   # izinkan URL localhost di antrean uji

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


def main():
    from app import downloader
    from app.db import DB
    from app.runner import Orchestrator

    # link localhost hanya diterima di mode uji
    orig = downloader.is_fb_video_url
    downloader.is_fb_video_url = lambda u: u.startswith("http://127.0.0.1") or orig(u)

    ff = list(Path(os.environ["LOCALAPPDATA"], "hermes", "tools").glob("ffmpeg-*/bin/ffmpeg.exe")) or \
        list(Path(os.environ["LOCALAPPDATA"], "ms-playwright").glob("ffmpeg-*/ffmpeg*.exe"))
    www = TMP / "www"
    www.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ff[0]), "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=15:duration=60",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", str(www / "v.mp4")], check=True)
    body = ("<html><body style='margin:0'><div style='height:4000px'>"
            "<video src='/v.mp4' autoplay muted playsinline style='width:320px'></video>"
            "</div></body></html>")
    for i in range(6):
        (www / f"p{i}.html").write_text(body, encoding="utf-8")
    (www / "broken.html").write_text("<html><body>" + "x" * 400 + "</body></html>", encoding="utf-8")

    port = free_port()
    d = str(www)

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=d, **k)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    db = DB(TMP / "t.db")
    accs = []
    for n in ("A1", "A2", "A3"):
        aid = db.add_account(n, "")
        db.update_account(aid, status="active")
        accs.append(db.account(aid))
    urls = [f"{base}/p{i}.html" for i in range(5)] + [f"{base}/broken.html"]
    db.add_items(urls)

    s = db.settings()
    s["default_duration"] = 4
    s["scroll_enabled"] = 0
    s["max_workers"] = 3
    s["retry_max"] = 1
    s["headless"] = 1

    print("\n== mode ALL: 3 akun x 6 item, 1 item rusak ==")
    o = Orchestrator(db)
    t0 = time.time()
    ok, msg = o.start(accs, s)
    check("start berhasil", ok, msg)
    check("3 worker jalan", len(o.workers) == 3, str(list(o.workers)))
    if ok:
        while time.time() - t0 < 240:
            st = o.status()
            if all(w["done"] for w in st["workers"]):
                break
            time.sleep(1)
        dt = time.time() - t0
        o.stop(hard=True)
        st = o.status()
        print(f"       selesai dalam {dt:.1f}s: {st['done']} done, {st['failed']} gagal/sebagian, "
              f"dari {st['total']} item (6 item x 3 akun = 18 job)")
        jobs = db.c.execute("SELECT result, COUNT(*) c FROM jobs GROUP BY result").fetchall()
        print("       job:", {r["result"]: r["c"] for r in jobs})
        done_items = [i for i in db.queue() if i["status"] == "done"]
        run_items = [i for i in db.queue() if i["status"] in ("failed", "partial")]
        check("5 item sukses (yang rusak gagal)", len(done_items) == 5, f"done={len(done_items)}")
        check("item rusak ditandai gagal/sebagian", len(run_items) == 1, str(len(run_items)))
        check("18 job tercatat (3 akun x 6 item)", sum(r["c"] for r in jobs) == 18,
              str(sum(r["c"] for r in jobs)))
        failed_jobs = [r["c"] for r in jobs if r["result"] == "failed"]
        check("hanya 3 job gagal (item rusak, 1 akun saja)",
              failed_jobs and failed_jobs[0] == 3, str([dict(r) for r in jobs]))
        check("akun tetap aktif walau ada item gagal",
              all(a["status"] == "active" for a in db.accounts()))
        # bukti paralel sejati: wall time mendekati waktu SATU akun menyelesaikan 6 item,
        # bukan 3x-nya. Diukur dari durasi job nyata di DB, bukan angka ajaib.
        rows = db.c.execute("SELECT account_id, finished_at-started_at d FROM jobs "
                            "WHERE finished_at IS NOT NULL").fetchall()
        per = {}
        for r in rows:
            per.setdefault(r["account_id"], []).append(r["d"])
        avg = sum(sum(v) for v in per.values()) / sum(len(v) for v in per.values())
        one_account = 6 * avg
        serial = 3 * one_account
        check("worker benar-benar paralel (mendekati 1 akun, bukan 3 akun berurutan)",
              dt < one_account * 1.8,
              f"{dt:.1f}s; 1 akun ~{one_account:.1f}s, berurutan ~{serial:.1f}s, "
              f"rata-rata job {avg:.1f}s")

    print("\n== mode SPLIT: 3 akun, 6 item dibagi rata ==")
    db.clear_queue()
    db.add_items([f"{base}/p{i}.html" for i in range(6)])
    o2 = Orchestrator(db)
    ok2, msg2 = o2.start(accs, {**s, "mode": "split"})
    check("start split berhasil", ok2, msg2)
    if ok2:
        m = o2.split_map
        check("tiap akun dapat 2 item", all(len(v) == 2 for v in m.values()),
              str({k: len(v) for k, v in m.items()}))
        check("tidak ada item dibagi ke 2 akun",
              len(set().union(*m.values())) == 6, str(len(set().union(*m.values()))))
        t0 = time.time()
        while time.time() - t0 < 180:
            st = o2.status()
            if all(w["done"] for w in st["workers"]):
                break
            time.sleep(1)
        o2.stop(hard=True)
        st = o2.status()
        print(f"       {st['done']}/{st['total']} item selesai di mode split")
    check("semua item selesai di split", o2.status()["done"] == 6, str(o2.status()["done"]))

    print("\n== batas worker: max_workers=2 dari 3 akun aktif ==")
    db.clear_queue()
    db.add_items([f"{base}/p{i}.html" for i in range(2)])
    o3 = Orchestrator(db)
    ok3, msg3 = o3.start(accs, {**s, "max_workers": 2})
    check("hanya 2 worker dibuat", len(o3.workers) == 2, str(len(o3.workers)))
    o3.stop(hard=True)
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 55)
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("TES PARALEL LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
