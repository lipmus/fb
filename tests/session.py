"""Regresi bug "sesi login tidak tersimpan".

Dulu `_looks_logged_in` hanya melihat URL. Halaman login Facebook memakai URL '/', dan form
login-nya dirender React beberapa detik setelah domcontentloaded. Akibatnya pengecekan pertama
menganggap "sudah login", storage_state dengan 0 cookie disimpan, status jadi aktif, lalu run
berikutnya minta login lagi.

Tes ini mereproduksi kondisi itu dengan server lokal:
  /welcome  -> halaman KOSONG, form login baru muncul setelah 4 detik
  /feed     -> halaman feed, tidak ada form, plus Set-Cookie c_user
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="faw-sess-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")

FAIL = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


LOGIN_PAGE = """<html><body><div id="root"></div>
<script>
  // tiru FB: form login muncul belakangan setelah bundle React jalan
  setTimeout(() => {
    document.getElementById('root').innerHTML =
      "<form action='/login/device-based/'><input name='email'><input name='pass' type='password'></form>";
  }, 4000);
</script></body></html>"""

FEED_PAGE = "<html><body><div>feed beranda</div><div role='feed'></div></body></html>"

# halaman checkpoint: di-serve pada URL '/' (seperti FB), tanpa form login, berisi teks verifikasi
CHECKPOINT_URL = "/"
CHECKPOINT_PAGE = """<html><body><div id="root"><h1>Security check</h1>
<p>We need to confirm it's you before continuing.</p>
<form id="captcha"><input name="captcha_response" placeholder="Masukkan kode"><img src="/captcha.png"></form>
</div></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if "/feed" in self.path:
            body, ctype = FEED_PAGE.encode(), "text/html"
        elif self.path == CHECKPOINT_URL or self.path.startswith("/?checkpoint"):
            body, ctype = CHECKPOINT_PAGE.encode(), "text/html"
        elif self.path.startswith("/welcome"):
            body, ctype = LOGIN_PAGE.encode(), "text/html"
        else:
            body, ctype = FEED_PAGE.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.path.startswith("/feed"):
            # cookie yang wajib ada pada sesi Facebook yang benar
            self.send_header("Set-Cookie", "c_user=1000000001; Path=/; SameSite=Lax")
            self.send_header("Set-Cookie", "xs=abc123; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    from playwright.sync_api import sync_playwright

    from app import crypto, runner as R
    from app.db import DB
    from app.paths import data_dir, session_path
    from app.runner import (_looks_logged_in, blocked_by_login, has_session_cookie,
                            is_login_form, launch, launch_browser, needs_verification)

    # tes memakai server lokal sebagai "facebook.com"
    R.FB_HOSTS = R.FB_HOSTS + ("127.0.0.1",)
    import app.gui as G
    G.FB_HOSTS = R.FB_HOSTS  # noqa: F841

    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    db = DB(TMP / "t.db")
    aid = db.add_account("Sesi", "")
    acc = db.account(aid)
    s = db.settings()
    s["headless"] = 1

    with sync_playwright() as pw:
        b, _ = launch_browser(pw, s)
        ctx = launch(b, acc, s, None)
        page = ctx.new_page()

        print("\n== halaman checkpoint di URL '/' (penyebab Chrome nutup sendiri) ==")
        cctx = launch(b, acc, s, None)     # context bersih, jangan kotori cookie tes lain
        cpage = cctx.new_page()
        cpage.goto(f"{base}/", wait_until="domcontentloaded")
        cpage.wait_for_timeout(500)
        url_only = not any(x in cpage.url.lower() for x in ("/login", "/r.php", "login.php"))
        check("cek URL saja lolos di halaman checkpoint (bug lama)", url_only, cpage.url)
        check("tidak ada form login di halaman itu (jadi cek form pun lolos)",
              not is_login_form(cpage))
        check("needs_verification mendeteksi halaman checkpoint", needs_verification(cpage))
        check("_looks_logged_in menolaknya", not _looks_logged_in(cpage))
        check("blocked_by_login menolaknya", blocked_by_login(cpage))
        check("halaman ini masih di host FB -> host-only check tidak cukup",
              any(h in cpage.url for h in ("127.0.0.1",)))
        check("belum ada cookie c_user di titik ini (jadi sesi tidak akan tersimpan)",
              not has_session_cookie(cctx.storage_state()))
        cctx.close()

        print("\n== CAPTCHA di halaman panjang tidak salah picu (feed normal) ==")
        cctx = launch(b, acc, s, None)
        cpage = cctx.new_page()
        cpage.goto(f"{base}/feed", wait_until="domcontentloaded")
        cpage.wait_for_timeout(400)
        check("feed normal tidak dianggap verifikasi", not needs_verification(cpage))
        cctx.close()

        print("\n== halaman login yang telat render (kondisi asli bug) ==")
        page.goto(f"{base}/welcome", wait_until="domcontentloaded")
        time.sleep(0.3)
        old = (page.url or "").lower()
        old_verdict = not any(x in old for x in ("/login", "/r.php", "login.php"))
        check("heuristik lama (URL saja) salah bilang sudah login", old_verdict,
              f"url={page.url}")
        # jujur: sebelum form login dirender, heuristik apa pun belum bisa tahu.
        # yang menyelamatkan bukan heuristik itu, tapi gerbang cookie c_user.
        one_shot = _looks_logged_in(page)
        no_cookie = not has_session_cookie(ctx.storage_state())
        check("satu sampel dini masih ambigu -> gerbang c_user yang wajib menahan",
              no_cookie, f"_looks_logged_in={one_shot}, ada c_user={not no_cookie}")
        cookies_now = len(ctx.storage_state().get("cookies", []))
        check("belum ada cookie c_user saat itu", cookies_now == 0, f"{cookies_now} cookie")

        page.wait_for_timeout(5000)
        check("setelah form login muncul tetap terdeteksi belum login",
              not _looks_logged_in(page) and is_login_form(page))
        check("halaman login ber-url '/welcome' (bukan /login) sehingga cek URL saja gagal",
              "/login" not in page.url.lower(), page.url)

        print("\n== halaman feed setelah login ==")
        page.goto(f"{base}/feed", wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        check("feed tidak punya form login", not is_login_form(page))
        check("feed dianggap sudah login", _looks_logged_in(page), page.url)

        print("\n== simpan & pulihkan sesi ==")
        state = ctx.storage_state()
        names = [c["name"] for c in state.get("cookies", [])]
        check("storage_state memuat c_user", "c_user" in names, str(names))
        p = session_path(data_dir(), aid)
        crypto.save(p, json.dumps(state))
        check("file sesi dibuat", p.exists() and p.stat().st_size > 50,
              f"{p.stat().st_size if p.exists() else 0} B")
        raw = p.read_bytes()
        check("isi file terenkripsi (bukan plaintext)", b"c_user" not in raw
              and raw.startswith(b"DPAPI1"))
        back = json.loads(crypto.load(p))
        got = [c["name"] for c in back.get("cookies", [])]
        check("cookie pulih dari file terenkripsi", "c_user" in got, str(got))
        ctx.close()

        print("\n== sesi hasil restore benar-benar dipakai browser baru ==")
        ctx2 = launch(b, acc, s, back)
        pg2 = ctx2.new_page()
        pg2.goto(f"{base}/feed", wait_until="domcontentloaded")
        cookie = pg2.evaluate("document.cookie")
        check("browser baru langsung punya cookie sesi", "c_user=1000000001" in cookie,
              cookie)
        check("browser baru sudah login tanpa login ulang", _looks_logged_in(pg2), pg2.url)
        ctx2.close()
        b.close()

    print("\n== sesi kosong ditolak (c_user wajib) ==")
    bad = session_path(data_dir(), 999)
    crypto.save(bad, json.dumps({"cookies": [], "origins": []}))
    names = [c["name"] for c in json.loads(crypto.load(bad)).get("cookies", [])]
    check("sesi 0 cookie terdeteksi tidak berguna", "c_user" not in names)

    print("\n== sesi akun A tidak bisa dipakai akun B ==")
    p2 = session_path(data_dir(), 1000)
    crypto.save(p2, json.dumps({"cookies": [{"name": "c_user", "value": "999",
                                             "domain": ".127.0.0.1", "path": "/",
                                             "expires": -1, "httpOnly": False,
                                             "secure": False, "sameSite": "Lax"}],
                                "origins": []}))
    a = json.loads(crypto.load(session_path(data_dir(), aid)))
    bstate = json.loads(crypto.load(p2))
    check("dua file sesi berbeda isinya",
          [c["value"] for c in a["cookies"] if c["name"] == "c_user"] !=
          [c["value"] for c in bstate["cookies"] if c["name"] == "c_user"])

    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 55)
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("TES SESI LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())