"""FB video downloader - pakai tab Chrome yang sudah login (cookie jar), jadi
tidak perlu mengekstrak cookie atau tanda tangan URL sendiri."""
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def video_id(url: str):
    """/watch?v=ID, /reel/ID, /videos/.../ID -> ID"""
    u = urlparse(url)
    if u.path.rstrip("/").endswith("/watch") or "watch" in u.path:
        v = parse_qs(u.query).get("v")
        if v:
            return v[0]
    m = re.search(r"/(?:reel|videos|video|watch)/(?:[^/]+/)?(\d{6,})", u.path)
    if m:
        return m.group(1)
    m = re.search(r"(\d{10,})", url)
    return m.group(1) if m else None


def is_fb_video_url(url: str) -> bool:
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return False
    host = (u.netloc or "").lower()
    if not any(host.endswith(h) for h in ("facebook.com", "fb.watch", "fb.com", "m.facebook.com")):
        return False
    if "fb.watch" in host:
        return True
    return bool(re.search(r"/(reel|videos|video|watch|story|photo|permalink)", u.path)) or "watch" in u.path


def normalize(url: str) -> str:
    """Buang parameter pelacak; jadikan kanonik supaya duplikat terdeteksi."""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("www.facebook.com"):
        url = "https://" + url
    u = parse_qs(urlparse(url).query)
    drop = {"__cft__", "__tn__", "eid", "paipv", "ref", "refsrc", "notif_id",
            "notif_t", "acontext", "ftentidentifier", "mibextid", "rdid", "sfnsn", "extid"}
    keep = {k: v for k, v in u.items() if k not in drop}
    base = url.split("?")[0]
    if keep:
        from urllib.parse import urlencode
        base += "?" + urlencode({k: v[0] for k, v in sorted(keep.items())})
    return base


def duration_from_url(url: str):
    """Kalau link tidak punya video id (feed/home), ini None -> buka di tab aktif apa pun."""
    return video_id(url)


def dest_path(outdir: Path, url: str, label: str = "", ext="mp4"):
    vid = video_id(url) or "video"
    tag = SAFE.sub("_", label)[:24].strip("_")
    name = f"{vid}_{tag}.{ext}" if tag else f"{vid}.{ext}"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir / name
