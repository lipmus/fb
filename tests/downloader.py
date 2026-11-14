"""Tes pengunduh: jalankan server lokal berisi video, tuduh Api.download_links,
verifikasi file hasil unduhan benar (ukuran + nama). Tidak butuh login Facebook."""
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
TMP = Path(tempfile.mkdtemp(prefix="faw-dl-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")

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
    from app.crypto import save as sess_save
    from app.downloader import dest_path
    from app.gui import Api
    from app.paths import data_dir, session_path

    ff = list(Path(os.environ["LOCALAPPDATA"], "hermes", "tools").glob("ffmpeg-*/bin/ffmpeg.exe")) or \
        list(Path(os.environ["LOCALAPPDATA"], "ms-playwright").glob("ffmpeg-*/ffmpeg*.exe"))
    if not ff:
        print("ffmpeg tidak ditemukan, tes dilewati")
        return 0

    www = TMP / "www"
    www.mkdir(parents=True, exist_ok=True)
    vid = www / "clip.mp4"
    subprocess.run([str(ff[0]), "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=15:duration=5",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", str(vid)], check=True)
    size = vid.stat().st_size
    check("video uji dibuat", size > 1000, f"{size} B")

    port = free_port()
    d = str(www)

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=d, **k)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    api = Api()
    aid = api.db.add_account("DlTest", "")
    api.db.update_account(aid, status="active")
    sess_save(session_path(data_dir(), aid), json.dumps({"cookies": [], "origins": []}))

    url = f"http://127.0.0.1:{port}/clip.mp4"
    outdir = TMP / "out"
    r = api.download_links(url, str(outdir), aid, "uji", True)
    check("download_links menerima link", r.get("ok"), str(r)[:120])
    if not r.get("ok"):
        return 1
    key = r["key"]
    t0 = time.time()
    while time.time() - t0 < 120:
        p = api.download_progress(key)
        if p.get("finished") or p.get("error"):
            break
        time.sleep(0.5)
    p = api.download_progress(key)
    print(f"       progress: {json.dumps({k: v for k, v in p.items() if k != 'items'})}")
    for it in p.get("items", []):
        print(f"       {it['status']}: {it.get('path') or it['message']}")
    check("tidak ada error pengunduh", not p.get("error"), str(p.get("error")))
    check("1 item diproses", p.get("done") == 1, str(p.get("done")))
    item = (p.get("items") or [{}])[0]
    check("status berhasil", item.get("status") == "berhasil", item.get("message", ""))
    dest = Path(item.get("path") or "")
    check("file ada di disk", dest.exists() and dest.stat().st_size == size,
          f"{dest.name if dest.name else dest} {dest.stat().st_size if dest.exists() else 0} B vs {size} B")
    check("nama file sesuai pola id_label", dest.name.startswith("clip_uji") or "uji" in dest.name, dest.name)
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + ("=" * 50))
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("TES PENGUNDUH LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
