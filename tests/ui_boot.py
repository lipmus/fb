"""Tes UI end-to-end: jalankan window pywebview sungguhan, tunggu JS boot memanggil
ui_ready, lalu tutup sendiri. Membuktikan bridge pywebview <-> JS benar-benar hidup."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="faw-ui-"))
env = dict(os.environ)
env["FAW_DATA_DIR"] = str(TMP / "data")
env["FAW_SELFTEST_SECONDS"] = "12"
env["PYTHONPATH"] = str(ROOT)

print(f"menjalankan GUI (data dir {env['FAW_DATA_DIR']}) ...")
p = subprocess.Popen([sys.executable, "-c", "from app.gui import run; run()"],
                     cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True)
flag = Path(env["FAW_DATA_DIR"]) / "ui_ready.json"
seen = False
t0 = time.time()
while time.time() - t0 < 30:
    if flag.exists():
        seen = True
        break
    if p.poll() is not None:
        break
    time.sleep(0.5)

out = ""
if p.poll() is None:
    try:
        p.wait(timeout=25)
    except subprocess.TimeoutExpired:
        p.kill()
try:
    out = p.stdout.read() if p.stdout else ""
except Exception:
    pass

if seen and p.returncode == 0:
    print("OK: JS->API bridge hidup (ui_ready.json dibuat)")
else:
    print(f"GAGAL: ui_ready={seen} exit={p.returncode}\n--- output ---\n{out[-3000:]}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if (seen and p.returncode == 0) else 1)
