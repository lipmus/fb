"""CLI: jalankan tanpa GUI, plus perintah login & diagnosa."""

import argparse
import json
import sys
import time

from .paths import data_dir, db_path, session_path
from .crypto import load as sess_load, save as sess_save
from .db import DB
from .downloader import is_fb_video_url, normalize


def _db():
    return DB(db_path())


def cmd_accounts(a):
    db = _db()
    for acc in db.accounts():
        sp = session_path(data_dir(), acc["id"])
        print(f"#{acc['id']:<3} {acc['name']:<20} status={acc['status']:<12} "
              f"enabled={acc['enabled']} sesi={'ada' if sp.exists() else 'kosong'} proxy={acc['proxy'] or '-'}")
        if acc["last_error"]:
            print(f"      error: {acc['last_error']}")


def cmd_add(a):
    db = _db()
    aid = db.add_account(a.name, "")
    db.log(f"CLI: akun '{a.name}' ditambahkan", aid)
    print(f"akun #{aid} ditambahkan")


def cmd_login(a):
    from playwright.sync_api import sync_playwright

    from .gui import Api
    from .runner import launch
    db = _db()
    acc = db.account(a.id)
    if not acc:
        print(f"akun #{a.id} tidak ada", file=sys.stderr)
        return 2
    s = db.settings()
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome" if s["channel"] != "chromium" else None,
                               headless=False,
                               args=["--disable-blink-features=AutomationControlled",
                                     "--no-first-run", "--no-default-browser-check"])
        st = None
        p = session_path(data_dir(), a.id)
        if p.exists() and not a.force:
            st = json.loads(sess_load(p))
        ctx = launch(b, acc, s, st)
        page = ctx.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
        print("Login manual di jendela Chrome. Menunggu status aktif (maks "
              f"{a.timeout} detik)... Ctrl+C untuk batal.")
        ok = False
        end = time.time() + a.timeout
        while time.time() < end:
            if page.is_closed():
                break
            if Api._is_logged_in(page):
                ok = True
                break
            time.sleep(2)
        if ok:
            sess_save(p, json.dumps(ctx.storage_state()))
            db.update_account(a.id, status="active", last_error="")
            db.log("CLI: login berhasil, sesi tersimpan", a.id)
            print(f"OK: sesi akun #{a.id} tersimpan -> {p}")
        else:
            db.update_account(a.id, status="needs_login", last_error="login tidak terdeteksi")
            print("GAGAL: login tidak terdeteksi", file=sys.stderr)
        ctx.close()
        b.close()
    return 0 if ok else 1


def cmd_addlinks(a):
    db = _db()
    urls, bad = [], []
    src = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    for line in src.splitlines():
        u = normalize(line)
        if not u:
            continue
        (urls if is_fb_video_url(u) else bad).append(u)
    ids, dup = db.add_items(urls)
    print(json.dumps({"ditambahkan": len(ids), "duplikat": len(dup), "ditolak": len(bad)},
                     indent=2))
    return 0


def cmd_durations(a):
    db = _db()
    n = 0
    for it in db.queue():
        db.update_item(it["id"], dur_min=a.seconds, dur_max=0)
        n += 1
    db.log(f"CLI: durasi {n} item diset {a.seconds}s")
    print(f"{n} item diset {a.seconds} detik")
    return 0


def cmd_check(a):
    from playwright.sync_api import sync_playwright

    from .runner import check_session, launch
    db = _db()
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True,
                               args=["--disable-blink-features=AutomationControlled"])
        for acc in db.accounts():
            if a.id and acc["id"] != a.id:
                continue
            st = None
            p = session_path(data_dir(), acc["id"])
            if p.exists():
                try:
                    st = json.loads(sess_load(p))
                except Exception as e:
                    print(f"#{acc['id']} {acc['name']}: sesi korup ({e})")
                    continue
            ctx = launch(b, acc, db.settings(), st)
            page = ctx.new_page()
            ok = check_session(page, db, acc["id"])
            print(f"#{acc['id']} {acc['name']}: {'AKTIF' if ok else 'butuh login'}")
            ctx.close()
        b.close()
    return 0


def cmd_run(a):
    """Jalan headless tanpa GUI. Ctrl+C = berhenti bersih."""
    from .runner import Orchestrator
    db = _db()
    accs = [x for x in db.accounts() if x["enabled"]]
    if a.id:
        accs = [x for x in accs if x["id"] == a.id]
    if not accs:
        print("tidak ada akun aktif", file=sys.stderr)
        return 2
    missing = [x["name"] for x in accs if not session_path(data_dir(), x["id"]).exists()]
    if missing:
        print(f"akun belum login: {', '.join(missing)}", file=sys.stderr)
        return 2
    s = db.settings()
    if a.duration:
        s["default_duration"] = a.duration
    orch = Orchestrator(db)
    ok, msg = orch.start(accs, s)
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(f"jalan: {msg}. Log di {data_dir()}")
    try:
        while orch.running:
            st = orch.status()
            sys.stdout.write(f"\r  item {st['done']}/{st['total']} selesai, "
                             f"{st['failed']} gagal, {len(st['workers'])} worker   ")
            sys.stdout.flush()
            if all(w["done"] for w in st["workers"]) and st["workers"]:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nstop...")
    orch.stop(hard=True)
    st = orch.status()
    print(f"\nselesai: {st['done']}/{st['total']} sukses, {st['failed']} gagal/sebagian")
    return 0


def cmd_download(a):
    from .gui import Api
    api = Api()
    r = api.download_links(" ".join(a.links) if a.links else sys.stdin.read(), a.outdir,
                           a.account, a.label, a.any)
    if not r.get("ok"):
        print(r.get("error"), file=sys.stderr)
        return 2
    key = r["key"]
    while True:
        p = api.download_progress(key)
        print(f"  {p['done']}/{p['total']}")
        if p.get("finished") or p.get("error"):
            break
        time.sleep(1)
    for it in p["items"]:
        print(f"{it['status']:<8} {it['url']} {it.get('path','')} {it['message']}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="fawatch",
                                description="Facebook Auto Watch Manager - CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("gui", help="buka aplikasi desktop")
    sp.set_defaults(fn=lambda a: (__import__("app.gui", fromlist=["run"]).run() or 0))

    sp = sub.add_parser("accounts", help="daftar akun")
    sp.set_defaults(fn=cmd_accounts)

    sp = sub.add_parser("add-account", help="tambah akun")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("login", help="login manual di browser terisolasi")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--force", action="store_true", help="abaikan sesi tersimpan")
    sp.set_defaults(fn=cmd_login)

    sp = sub.add_parser("check", help="cek status sesi semua akun")
    sp.add_argument("--id", type=int)
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("add-links", help="tambah link dari file atau stdin")
    sp.add_argument("file", help="path file, atau - untuk stdin")
    sp.set_defaults(fn=cmd_addlinks)

    sp = sub.add_parser("set-duration", help="set durasi semua item (detik)")
    sp.add_argument("seconds", type=int)
    sp.set_defaults(fn=cmd_durations)

    sp = sub.add_parser("run", help="jalankan antrean tanpa GUI")
    sp.add_argument("--id", type=int, help="hanya akun ini")
    sp.add_argument("--duration", type=int, help="override durasi default")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("download", help="unduh video pakai sesi login")
    sp.add_argument("links", nargs="*")
    sp.add_argument("--account", type=int)
    sp.add_argument("--outdir", default=None)
    sp.add_argument("--label", default="")
    sp.add_argument("--any", action="store_true",
                    help="izinkan URL media langsung (non-Facebook, jangan normalisasi query)")
    sp.set_defaults(fn=cmd_download)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
