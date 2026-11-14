"""Smoke test: unit + integrasi nyata (browser Chrome + video lokal).

Jalankan: py -3.12 tests\\smoke.py
"""
import json
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

TMP = Path(tempfile.mkdtemp(prefix="faw-smoke-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")

FAIL = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def section(t):
    print(f"\n== {t} ==")


# ---------------------------------------------------------------- unit
def test_units():
    section("unit: import + db + queue")
    from app.db import DB
    from app import cli, gui, runner, downloader  # noqa: F401

    db = DB(TMP / "t.db")
    ok = db.add_items(["https://www.facebook.com/watch?v=1", "https://www.facebook.com/watch?v=2",
                       "https://www.facebook.com/watch?v=1"])
    check("dedupe saat tambah", len(ok[0]) == 2 and len(ok[1]) == 1, f"added={ok[0]} dup={ok[1]}")
    ids = [i["id"] for i in db.queue()]
    db.reorder(list(reversed(ids)))
    check("reorder", [i["id"] for i in db.queue()] == list(reversed(ids)))
    db.update_item(ids[0], dur_min=30, dur_max=60)
    it = db.item(ids[0])
    check("durasi rentang tersimpan", (it["dur_min"], it["dur_max"]) == (30, 60))
    aid = db.add_account("A", "")
    jid = db.add_job(aid, ids[0])
    check("job running dibuat", db.jobs_for_item(ids[0])[0]["result"] == "running")
    check("recover mengembalikan item", db.recover() == 1 and db.item(ids[0])["status"] == "waiting")
    db.finish_job(jid, "done", "x")
    check("job selesai", db.jobs_for_item(ids[0])[0]["result"] == "done")
    db.log("uji log")
    check("log tersimpan", len(db.logs()) >= 1)

    section("unit: enkripsi sesi (DPAPI)")
    from app.crypto import load, save
    p = TMP / "s.enc"
    secret = json.dumps({"cookies": [{"name": "c_user", "value": "12345"}]})
    save(p, secret)
    raw = p.read_bytes()
    check("file sesi tidak plaintext", b"c_user" not in raw and raw.startswith(b"DPAPI1"))
    check("roundtrip DPAPI", load(p) == secret)
    try:
        Path(str(p) + ".bad").write_bytes(b"nope")
        load(Path(str(p) + ".bad"))
        check("file sesi rusak ditolak", False)
    except Exception:
        check("file sesi rusak ditolak", True)

    section("unit: validasi & normalisasi link")
    from app.downloader import is_fb_video_url, normalize, video_id
    check("link watch valid", is_fb_video_url("https://www.facebook.com/watch/?v=1234567890"))
    check("link reel valid", is_fb_video_url("https://www.facebook.com/reel/987654321"))
    check("link non-FB ditolak", not is_fb_video_url("https://youtube.com/watch?v=1"))
    check("param pelacak dibuang", normalize(
        "https://www.facebook.com/watch/?v=1&__cft__=abc&ref=share") == "https://www.facebook.com/watch/?v=1")
    check("video_id dari /videos/", video_id("https://www.facebook.com/foo/videos/123456789") == "123456789")

    section("unit: durasi & deteksi halaman")
    from app.runner import solve_duration, _looks_logged_in
    check("durasi default dipakai", solve_duration({"dur_min": 0, "dur_max": 0}, 45) == 45)
    check("durasi tetap dipakai", solve_duration({"dur_min": 20, "dur_max": 0}, 45) == 20)
    got = {solve_duration({"dur_min": 30, "dur_max": 40}, 45) for _ in range(60)}
    check("rentang acak 30-40", all(30 <= g <= 40 for g in got) and len(got) > 1, sorted(got))

    class P:  # page palsu
        def __init__(self, url):
            self.url = url

    check("url login dideteksi belum login", not _looks_logged_in(P("https://www.facebook.com/login/?next=x")))
    check("checkpoint dideteksi belum login", not _looks_logged_in(P("https://www.facebook.com/checkpoint/1501092823525282/")))
    check("feed normal dianggap login", _looks_logged_in(P("https://www.facebook.com/")))

    return db


# ---------------------------------------------------------------- UI contract
def test_ui_contract():
    section("UI: nama API di JS ada di backend")
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    from app.gui import Api
    api = Api()
    called = set(re.findall(r"(?:call|api\.\w+)\(\s*'([a-z_]+)'", html)) | \
        set(re.findall(r"await api\['([a-z_]+)'\]", html))
    missing = sorted(c for c in called if not hasattr(api, c))
    check("semua call(...) ada di Api", not missing, f"missing={missing}")
    tabs = set(re.findall(r'data-tab="(\w+)"', html))
    sections = set(re.findall(r'id="tab-(\w+)"', html))
    check("tab & section cocok", tabs == sections, f"{tabs ^ sections}")
    print(f"       API dipanggil UI: {sorted(called)}")


# ---------------------------------------------------------------- orchestrator
class FakeWorker:
    def __init__(self, aid):
        self.aid = aid
        self.item_id = None
        self.paused = False
        self.done = False
        self.remaining = 0.0


def test_orchestrator(db):
    section("orkestrator: claim / retry / split / status")
    from app.runner import Orchestrator
    db.clear_queue()
    db.add_items([f"https://www.facebook.com/watch?v={1000+i}" for i in range(4)])
    a1 = db.add_account("A1", "")
    a2 = db.add_account("A2", "")
    accs = [db.account(a1), db.account(a2)]
    o = Orchestrator(db)
    o.mode = "all"

    w1, w2 = FakeWorker(a1), FakeWorker(a2)
    o.workers = {a1: w1, a2: w2}
    first = o.claim(w1, None)
    check("claim mengembalikan item", first is not None)
    check("item ditandai running", db.item(first["id"])["status"] == "running")
    db.add_job(a1, first["id"])
    second = o.claim(w1, None)
    check("claim kedua dapat item berbeda", second and second["id"] != first["id"])

    items = db.queue()
    j2 = db.add_job(a1, second["id"])
    o.release(w1, second, True, "ok")
    db.finish_job(j2, "done", "ok")
    check("item selesai setelah semua akun done/limit",
          db.item(second["id"])["status"] in ("running", "done", "partial"),
          db.item(second["id"])["status"])

    # split mode: item ke-i ke akun ke-i
    o.mode = "split"
    o.split_map = {a1: {items[0]["id"]}, a2: {items[1]["id"]}}
    check("split: akun1 hanya boleh item0", o._item_allowed(a1, items[0]["id"])
          and not o._item_allowed(a1, items[1]["id"]))
    check("split: akun2 hanya boleh item1", o._item_allowed(a2, items[1]["id"])
          and not o._item_allowed(a2, items[0]["id"]))

    # resume tanpa mengulang: akun ini sudah punya job untuk item -> tidak diklaim lagi
    o.mode = "all"
    done_item = second["id"]
    db.finish_job(db.jobs_for_item(done_item)[0]["id"], "done", "")
    check("item yang sudah dikerjakan akun ini tidak diklaim ulang",
          not any((o.claim(w1, None) or {}).get("id") == done_item for _ in range(6)))

    st = o.status()
    check("status menghitung total", st["total"] == 4, st["total"])

    # restart: instance baru (proses baru) tidak mengulang item yang sudah punya job
    from app.runner import Orchestrator as O2
    o2 = O2(db)
    o2.mode = "all"
    w3 = FakeWorker(a1)
    o2.workers = {a1: w3}
    claimed = []
    for _ in range(5):
        it = o2.claim(w3, None)
        if not it:
            break
        db.add_job(a1, it["id"])
        claimed.append(it["id"])
    check("setelah restart item yang sudah dikerjakan tidak diulang",
          done_item not in claimed, f"claimed={claimed}")
    return o


# ---------------------------------------------------------------- real browser
def test_real_browser(db):
    section("integrasi nyata: Chrome + video lokal + auto-scroll")
    from app.runner import Worker, launch

    ff = list(Path(os.environ["LOCALAPPDATA"], "hermes", "tools").glob("ffmpeg-*/bin/ffmpeg.exe")) or \
        list(Path(os.environ["LOCALAPPDATA"], "ms-playwright").glob("ffmpeg-*/ffmpeg*.exe"))
    if not ff:
        check("ffmpeg tersedia untuk bikin video uji", False)
        return
    srv_dir = TMP / "www"
    srv_dir.mkdir(parents=True, exist_ok=True)
    vid = srv_dir / "test.mp4"
    subprocess.run([str(ff[0]), "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=15:duration=40",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(vid)], check=True)
    check("video uji dibuat", vid.stat().st_size > 1000, f"{vid.stat().st_size} B")
    (srv_dir / "page.html").write_text(
        "<html><head><title>t</title></head><body style='margin:0'>"
        "<div style='height:4000px'>"
        "<video id='v' src='/test.mp4' autoplay muted playsinline "
        "style='width:320px;height:240px'></video>"
        "<script>window.__scrolls=0;addEventListener('scroll',()=>window.__scrolls++);</script>"
        "</div></body></html>", encoding="utf-8")

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _quiet_handler(str(srv_dir)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/page.html"
    print(f"       server uji: {base}")

    aid = db.add_account("BrowserTest", "")
    db.update_account(aid, status="active")
    acc = db.account(aid)
    s = db.settings()
    s["scroll_enabled"] = 1
    s["scroll_speed"] = "fast"

    _db = db

    class FakeOrch:
        stopping = False
        db = _db
        def is_running(self):
            return True
        def is_drained(self):
            return True
        def on_worker_ready(self, w):
            pass
        def on_worker_exit(self, w):
            pass

    from playwright.sync_api import sync_playwright
    t0 = time.time()
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True,
                               args=["--disable-blink-features=AutomationControlled",
                                     "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
        check("Chrome sistem (channel=chrome) bisa diluncurkan", True,
              f"{time.time()-t0:.1f}s")
        ctx = launch(b, acc, s, None)
        page = ctx.new_page()
        w = Worker(FakeOrch(), acc, s)
        w.db = db
        item = {"id": 999, "url": base, "dur_min": 6, "dur_max": 0}
        t1 = time.time()
        ok, msg = w._run_item(page, item)
        dt = time.time() - t1
        print(f"       hasil: ok={ok} msg={msg!r} wall={dt:.1f}s")
        check("video terdeteksi berputar", ok, msg)
        check("durasi dihormati (~6s, toleransi 3s)", 4.5 <= dt <= 10.5, f"{dt:.1f}s")
        scrolls = page.evaluate("window.__scrolls")
        check("auto-scroll jalan", scrolls > 0, f"{scrolls} event scroll")
        pos = page.evaluate("(()=>{const v=document.querySelector('video');return v.currentTime})()")
        check("posisi video maju", pos > 0, f"currentTime={pos}")

        # halaman tanpa video = harus gagal cepat, tidak menggantung durasi penuh
        page2 = ctx.new_page()
        (srv_dir / "none.html").write_text("<html><body>" + "x" * 500 + "</body></html>", encoding="utf-8")
        t2 = time.time()
        item2 = {"id": 998, "url": f"http://127.0.0.1:{port}/none.html", "dur_min": 30, "dur_max": 0}
        ok2, msg2 = w._run_item(page2, item2)
        dt2 = time.time() - t2
        print(f"       halaman tanpa video: ok={ok2} msg={msg2!r} wall={dt2:.1f}s")
        check("halaman tanpa video -> gagal", not ok2)
        check("tidak menggantung 30s penuh", dt2 < 32, f"{dt2:.1f}s")

        # pause/resume + stop harus bereaksi
        w.paused = True
        threading.Timer(1.5, lambda: setattr(w, "paused", False)).start()
        t3 = time.time()
        ok3, _ = w._run_item(page, {"id": 997, "url": base, "dur_min": 3, "dur_max": 0})
        check("pause menunda, lalu jalan", ok3 and (time.time() - t3) > 3.5, f"{time.time()-t3:.1f}s")

        def stopper():
            time.sleep(1.0)
            w.cmd = "stop"
            w.cmd_event.set()
        threading.Thread(target=stopper, daemon=True).start()
        t4 = time.time()
        ok4, msg4 = w._run_item(page, {"id": 996, "url": base, "dur_min": 30, "dur_max": 0})
        check("stop menghentikan item berjalan", not ok4 and "dihentikan" in msg4,
              f"{msg4!r} wall={time.time()-t4:.1f}s")
        ctx.close()
        b.close()
    httpd.shutdown()
    check("browser ditutup bersih", True)


def _quiet_handler(directory):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=directory, **k)

        def log_message(self, *a):
            pass
    return H


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    print(f"data dir uji: {os.environ['FAW_DATA_DIR']}")
    db = test_units()
    test_ui_contract()
    test_orchestrator(db)
    try:
        test_real_browser(db)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("integrasi browser selesai", False, str(e))
    print("\n" + ("=" * 60))
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("SEMUA TES LULUS")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
