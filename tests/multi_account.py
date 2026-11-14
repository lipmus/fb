"""Bukti: N akun = N proses Chrome terpisah, tiap akun context sendiri (cookie terisolasi)."""
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
TMP = Path(tempfile.mkdtemp(prefix="faw-multi-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")

import psutil  # noqa: E402

from app.crypto import save as sess_save  # noqa: E402
from app.db import DB  # noqa: E402
from app.runner import Worker, launch, launch_browser, session_path, data_dir  # noqa: E402


def main():
    db = DB(TMP / "t.db")
    accs = []
    for n in ("Akun1", "Akun2"):
        aid = db.add_account(n, "")
        db.update_account(aid, status="active")
        sess_save(session_path(data_dir(), aid),
                  json.dumps({"cookies": [{"name": "who", "value": n, "domain": ".127.0.0.1",
                                           "path": "/", "expires": -1, "httpOnly": False,
                                           "secure": False, "sameSite": "Lax"}],
                              "origins": []}))
        accs.append(db.account(aid))

    s = db.settings()
    s["headless"] = 1
    s["mute"] = 1
    print(f"akun: {[a['name'] for a in accs]}")

    # worker palsu: jalankan run() sungguhan tapi _loop dihentikan setelah siap
    class Stop(Exception):
        pass

    class O:
        stopping = False

        def __init__(self, db):
            self.db = db

        def is_running(self):
            return False          # _loop langsung idle
        def is_drained(self):
            return True
        def on_worker_ready(self, w):
            w.send("stop")
        def on_worker_exit(self, w):
            pass

    ws = []
    for a in accs:
        w = Worker(O(db), a, s)
        w.start()
        ws.append(w)
    me = psutil.Process()
    chromes = []
    for _ in range(60):          # ambil snapshot saat browser masih hidup
        found = []
        for p in me.children(recursive=True):
            try:
                if "chrome" not in p.name().lower():
                    continue
                cmd = p.cmdline()
            except Exception:
                continue
            if not any(c.startswith("--remote-debugging") for c in cmd):
                continue
            udd = next((c.split("=", 1)[1] for c in cmd if c.startswith("--user-data-dir=")), "?")
            found.append({"pid": p.pid, "udd": udd})
        if len(found) > len(chromes):
            chromes = found
        if len(chromes) >= 2:
            break
        time.sleep(0.5)

    udds = {c["udd"] for c in chromes}
    print(f"\nproses Chrome milik aplikasi: {len(chromes)}")
    for c in chromes:
        print(f"  pid={c['pid']} user-data-dir={c['udd']}")
    print(f"user-data-dir unik: {len(udds)} dari {len(chromes)} proses")
    assert len(chromes) >= 2, "harus ada >=2 proses Chrome"
    assert len(udds) == len(chromes), "tiap browser harus profil sendiri"

    # isolasi cookie: baca cookie 'who' dari context masing-masing lewat halaman kosong
    prova = TMP / "p.html"
    prova.write_text("<html><body>ok</body></html>", encoding="utf-8")

    port = _free_port()

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(TMP), **k)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    seen = {}
    for a in accs:
        state = json.loads(__import__("app.crypto", fromlist=["load"]).load(
            session_path(data_dir(), a["id"])))
        # context di browser sendiri, seperti worker sungguhan
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b, chan = launch_browser(pw, s)
        ctx = launch(b, a, s, state)
        pg = ctx.new_page()
        pg.goto(f"http://127.0.0.1:{port}/p.html", wait_until="domcontentloaded")
        seen[a["name"]] = pg.evaluate("document.cookie")
        print(f"  {a['name']}: cookie di browser-nya = {seen[a['name']]!r}")
        ctx.close()
        b.close()
        pw.stop()

    assert seen["Akun1"] == "who=Akun1", seen
    assert seen["Akun2"] == "who=Akun2", seen
    print("\nOK: cookie akun tidak bocor antar browser")
    assert seen["Akun1"] != seen["Akun2"]

    for w in ws:
        w.send("stop")
    for w in ws:
        w.join(timeout=10)
    httpd.shutdown()
    print("OK: tiap akun = 1 proses Chrome, 1 profil, 1 cookie jar sendiri")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


if __name__ == "__main__":
    sys.exit(main())
