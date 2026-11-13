from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, parse_qs, unquote, urlparse

try:
    import certifi
    CERTIFI_CA = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", CERTIFI_CA)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CERTIFI_CA)
except ImportError:
    CERTIFI_CA = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

HOST = os.getenv("DRIFT_HOST", "127.0.0.1")
PORT = int(os.getenv("DRIFT_PORT", "5000"))
MAX_URL_LENGTH = 4096
MAX_DOWNLOAD_BYTES = int(os.getenv("DRIFT_MAX_DOWNLOAD_MB", "500")) * 1024 * 1024
JOB_TTL_SECONDS = int(os.getenv("DRIFT_JOB_TTL_SECONDS", str(30 * 60)))
MAX_ACTIVE_JOBS = int(os.getenv("DRIFT_MAX_ACTIVE_JOBS", "2"))
DOWNLOAD_ROOT = Path(os.getenv("DRIFT_DOWNLOAD_ROOT", str(Path(tempfile.gettempdir()) / "drift-downloads")))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
FRONTEND_ROOT = Path(__file__).resolve().parent.parent / "frontend"
DB_PATH = Path(os.getenv("DRIFT_DB_PATH", str(DOWNLOAD_ROOT / "drift.sqlite3")))
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_QUALITIES = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
KNOWN_HOSTS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com",
}

DB_LOCK = threading.RLock()
EVENT_LOCK = threading.RLock()
CANCEL_EVENTS: dict[str, threading.Event] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_ACTIVE_JOBS, thread_name_prefix="drift-download")
RATE_LOCK = threading.Lock()
RATE_WINDOWS: dict[str, list[float]] = {}


def now() -> float:
    return time.time()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with DB_LOCK, db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress REAL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                speed REAL,
                eta REAL,
                url TEXT NOT NULL,
                quality TEXT NOT NULL,
                format TEXT NOT NULL,
                platform TEXT NOT NULL,
                title TEXT,
                thumbnail TEXT,
                filename TEXT,
                file_size INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                job_dir TEXT,
                file_path TEXT,
                duration TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        # Jobs cannot safely remain mid-network operation after a hard process restart.
        conn.execute("""
            UPDATE jobs
            SET status='failed', phase='Failed', error='Backend restarted while this download was running.',
                message='The backend restarted before this download completed.', updated_at=?
            WHERE status IN ('queued','downloading','processing')
        """, (now(),))
        conn.commit()


def row_to_job(row: sqlite3.Row) -> dict:
    job = dict(row)
    return job


def get_job(job_id: str) -> dict | None:
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def update_job(job_id: str, **updates) -> None:
    if not updates:
        return
    updates["updated_at"] = now()
    columns = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [job_id]
    with DB_LOCK, db() as conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", values)
        conn.commit()


def insert_job(job: dict) -> None:
    columns = list(job.keys())
    placeholders = ",".join("?" for _ in columns)
    with DB_LOCK, db() as conn:
        conn.execute(f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})", [job[c] for c in columns])
        conn.commit()


def validate_public_url(raw_url: str):
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > MAX_URL_LENGTH:
        return False, "Invalid URL."
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
            return False, "Only HTTP(S) URLs are supported."
        host = parsed.hostname.lower().rstrip(".")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, "Private or internal addresses are not allowed."
        except ValueError:
            pass
        if host == "localhost" or host.endswith(".localhost"):
            return False, "Local addresses are not allowed."
        if not any(host == allowed or host.endswith("." + allowed) for allowed in KNOWN_HOSTS):
            return False, "This source is not supported. Use a public YouTube, TikTok, Instagram, Facebook, or X URL."
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return False, "Private or internal addresses are not allowed."
        except socket.gaierror:
            return False, "The hostname could not be resolved."
        return True, parsed
    except (ValueError, UnicodeError):
        return False, "Invalid URL."


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "instagram." in host: return "Instagram"
    if "tiktok." in host: return "TikTok"
    if "youtube." in host or host == "youtu.be" or host.endswith(".youtu.be"): return "YouTube"
    if "facebook." in host or host == "fb.watch" or host.endswith(".fb.watch"): return "Facebook"
    if host == "x.com" or host.endswith(".x.com") or "twitter." in host: return "X"
    return "Public media"


def format_selector(quality: str) -> str:
    if quality == "best":
        return "bestvideo+bestaudio/best"
    height = ALLOWED_QUALITIES.get(quality, 720)
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"


def clean_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    low = message.lower()
    if "certificate_verify_failed" in low or "unable to get local issuer" in low:
        return "TLS certificate verification failed. Update certifi and yt-dlp."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg is required for this media. Install FFmpeg and restart Drift."
    if "requested format is not available" in low:
        return "That quality is not available for this source. Choose another quality and try again."
    if "private video" in low or "login required" in low or "sign in" in low:
        return "This media is not publicly accessible without authentication."
    if "unsupported url" in low:
        return "This URL is not supported by Drift."
    return message[:600] or "Unknown downloader error."


def client_key(handler: BaseHTTPRequestHandler) -> str:
    # Never trust X-Forwarded-For unless an explicit trusted-proxy setup is added.
    return handler.client_address[0] or "unknown"


def rate_limit(handler: BaseHTTPRequestHandler, bucket: str, limit: int, window_seconds: int) -> bool:
    key = f"{bucket}:{client_key(handler)}"
    cutoff = now() - window_seconds
    with RATE_LOCK:
        timestamps = [t for t in RATE_WINDOWS.get(key, []) if t > cutoff]
        if len(timestamps) >= limit:
            RATE_WINDOWS[key] = timestamps
            return False
        timestamps.append(now())
        RATE_WINDOWS[key] = timestamps
        return True


def safe_filename(name: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", name or "Drift video")
    name = re.sub(r"\s+", " ", name).strip(" .")[:160] or "Drift video"
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip(" .") or "Drift video"
    return name + ".mp4"


def progress_hook(job_id: str, job_dir: Path):
    def hook(data: dict):
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            raise DownloadCancelled()
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            if downloaded > MAX_DOWNLOAD_BYTES:
                raise DownloadTooLarge("The download exceeded Drift's 500 MB limit.")
            progress = round(downloaded / total * 100, 1) if total else None
            update_job(job_id, status="downloading", phase="Downloading", progress=progress,
                       downloaded_bytes=downloaded, total_bytes=total,
                       speed=data.get("speed"), eta=data.get("eta"), message="Downloading media…")
        elif status == "finished":
            update_job(job_id, status="processing", phase="Processing", progress=100,
                       downloaded_bytes=int(data.get("downloaded_bytes") or 0), speed=None, eta=None,
                       message="Download received. Finalizing the MP4…")
    return hook


class DownloadCancelled(Exception): pass
class DownloadTooLarge(Exception): pass


def find_media(job_dir: Path) -> Path | None:
    candidates = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def run_download(job_id: str, url: str, quality: str) -> None:
    job_dir = Path(tempfile.mkdtemp(prefix=f"drift-{job_id}-", dir=DOWNLOAD_ROOT))
    update_job(job_id, job_dir=str(job_dir), status="downloading", phase="Downloading", progress=0,
               message="Starting media transfer…")
    try:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -r requirements.txt")
        options = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": format_selector(quality), "merge_output_format": "mp4",
            "outtmpl": str(job_dir / "%(title).180s.%(ext)s"),
            "restrictfilenames": True, "windowsfilenames": True,
            "max_filesize": MAX_DOWNLOAD_BYTES, "progress_hooks": [progress_hook(job_id, job_dir)],
            "continuedl": True, "retries": 3, "fragment_retries": 3, "socket_timeout": 20,
            "quiet": True,
        }
        if CERTIFI_CA: options["ca_certs"] = CERTIFI_CA
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        media = find_media(job_dir)
        if media is None: raise RuntimeError("The media file was not created.")
        size = media.stat().st_size
        if size > MAX_DOWNLOAD_BYTES: raise DownloadTooLarge("The final media file exceeded Drift's 500 MB limit.")
        title = info.get("title") or "Drift video"
        duration = info.get("duration_string")
        final_name = safe_filename(title)
        final_path = job_dir / final_name
        if media.resolve() != final_path.resolve():
            media.replace(final_path)
        update_job(job_id, status="ready", phase="Ready", progress=100, title=title,
                   duration=duration, filename=final_name, file_path=str(final_path), file_size=size,
                   message="Your MP4 is ready to save.", completed_at=now())
    except DownloadCancelled:
        update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
        shutil.rmtree(job_dir, ignore_errors=True)
    except DownloadTooLarge as exc:
        update_job(job_id, status="failed", phase="Failed", error=str(exc), message=str(exc))
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:
        message = clean_error(exc)
        update_job(job_id, status="failed", phase="Failed", error=message, message=message)
        shutil.rmtree(job_dir, ignore_errors=True)
    finally:
        CANCEL_EVENTS.pop(job_id, None)


def cleanup_once() -> None:
    cutoff = now() - JOB_TTL_SECONDS
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT id, job_dir, status, updated_at FROM jobs WHERE updated_at < ?", (cutoff,)).fetchall()
        for row in rows:
            if row[2] in {"queued", "downloading", "processing"}: continue
            conn.execute("DELETE FROM jobs WHERE id=?", (row[0],))
        conn.commit()
    for directory in DOWNLOAD_ROOT.iterdir():
        try:
            if directory.is_dir() and directory.name.startswith("drift-") and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        except FileNotFoundError: pass


def cleanup_loop() -> None:
    while True:
        time.sleep(60)
        try: cleanup_once()
        except Exception: pass


def json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Drift/2.0"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {fmt % args}")

    def send_json(self, data: dict, status=200, headers=None):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors()
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_cors(self):
        origin = self.headers.get("Origin")
        allowed = os.getenv("DRIFT_ALLOWED_ORIGIN", "*")
        if allowed == "*" or origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed if allowed != "*" else "*")
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", os.getenv("DRIFT_ALLOWED_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024: raise ValueError("Request body is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def serve_static(self, path: str):
        requested = unquote(path or "/")
        if requested == "/": requested = "/index.html"
        relative = Path(requested.lstrip("/"))
        candidate = (FRONTEND_ROOT / relative).resolve()
        try:
            candidate.relative_to(FRONTEND_ROOT.resolve())
        except ValueError:
            return self.send_json({"success": False, "message": "Not found."}, 404)
        if not candidate.is_file():
            return self.send_json({"success": False, "message": "Not found."}, 404)
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            requested = unquote(path or "/")
            if requested == "/": requested = "/index.html"
            candidate = (FRONTEND_ROOT / Path(requested.lstrip("/"))).resolve()
            try:
                candidate.relative_to(FRONTEND_ROOT.resolve())
            except ValueError:
                self.send_response(404); self.end_headers(); return
            if not candidate.is_file():
                self.send_response(404); self.end_headers(); return
            data_len = candidate.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(data_len))
            self.end_headers()
            return
        if path == "/api/health":
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); return
        self.send_response(404); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            with DB_LOCK, db() as conn:
                active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0]
            return self.send_json({"status":"ok","service":"drift-backend","downloader":yt_dlp is not None,
                                   "ffmpeg":shutil.which("ffmpeg") is not None,"active_jobs":active,
                                   "max_active_jobs":MAX_ACTIVE_JOBS})
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)", path)
        if m:
            job = get_job(m.group(1))
            return self.send_json({"success":True,"job":job} if job else {"success":False,"message":"Download job not found or expired."}, 200 if job else 404)
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/file", path)
        if m: return self.stream_file(m.group(1))
        if not path.startswith("/api/"):
            return self.serve_static(path)
        self.send_json({"success":False,"message":"Not found."}, 404)

    def stream_file(self, job_id):
        job = get_job(job_id)
        if not job: return self.send_json({"success":False,"message":"Download job not found or expired."},404)
        if job["status"] != "ready" or not job.get("file_path"):
            return self.send_json({"success":False,"message":"The file is not ready yet."},409)
        path = Path(job["file_path"])
        if not path.is_file():
            update_job(job_id, status="failed", phase="Failed", error="The temporary file is no longer available.", message="The file is no longer available.")
            return self.send_json({"success":False,"message":"The temporary file is no longer available."},410)
        size = path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES: return self.send_json({"success":False,"message":"The file exceeds Drift's size limit."},413)
        range_header = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m:
                if m.group(1): start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    length = int(m.group(2)); start = max(0, size-length); end = size-1
                if start > end or start >= size:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
                end = min(end, size-1); status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        filename = safe_filename(job.get("title") or "Drift video")
        quoted = filename.replace("\"", "")
        self.send_header("Content-Disposition", f'attachment; filename="{quoted}"; filename*=UTF-8\'\'{quote(filename)}')
        self.send_header("Cache-Control", "no-store")
        self.send_cors(); self.end_headers()
        with path.open("rb") as f:
            f.seek(start); remaining = length
            while remaining:
                chunk = f.read(min(1024*1024, remaining))
                if not chunk: break
                self.wfile.write(chunk); remaining -= len(chunk)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/analyze": return self.analyze()
        if path == "/api/downloads": return self.create_download()
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/cancel", path)
        if m: return self.cancel(m.group(1))
        if path == "/api/download": return self.send_json({"success":False,"message":"This endpoint was replaced by the live download job API. Refresh the Drift frontend."},410)
        self.send_json({"success":False,"message":"Not found."},404)

    def analyze(self):
        if not rate_limit(self,"analyze",20,60): return self.send_json({"success":False,"message":"Too many analysis requests. Please wait a moment."},429)
        if yt_dlp is None: return self.send_json({"success":False,"message":"yt-dlp is not installed. Run: python -m pip install -r requirements.txt"},500)
        try: body=self.read_json()
        except Exception as exc: return self.send_json({"success":False,"message":str(exc)},400)
        url=str(body.get("url","")).strip(); valid, result=validate_public_url(url)
        if not valid: return self.send_json({"success":False,"message":result},400)
        try:
            opts={"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True,"socket_timeout":20}
            if CERTIFI_CA: opts["ca_certs"]=CERTIFI_CA
            with yt_dlp.YoutubeDL(opts) as ydl: info=ydl.extract_info(url,download=False)
            heights=sorted({int(f["height"]) for f in info.get("formats",[]) if f.get("height") and f.get("vcodec") not in (None,"none")},reverse=True)
            heights=[h for h in heights if 144 <= h <= 2160]
            formats=[{"quality":f"{h}p","height":h} for h in heights[:8]]
            formats.insert(0,{"quality":"Best available","height":None})
            return self.send_json({"success":True,"platform":detect_platform(url),"title":info.get("title") or "Public video",
                                   "duration":info.get("duration_string"),"thumbnail":info.get("thumbnail"),"formats":formats,
                                   "ffmpeg":shutil.which("ffmpeg") is not None})
        except Exception as exc:
            return self.send_json({"success":False,"message":clean_error(exc)},422)

    def create_download(self):
        if not rate_limit(self,"download",5,60): return self.send_json({"success":False,"message":"Download limit reached. Please wait a moment."},429)
        if yt_dlp is None: return self.send_json({"success":False,"message":"yt-dlp is not installed. Run: python -m pip install -r requirements.txt"},500)
        try: body=self.read_json()
        except Exception as exc: return self.send_json({"success":False,"message":str(exc)},400)
        url=str(body.get("url","")).strip(); quality=str(body.get("quality","best"))
        valid, result=validate_public_url(url)
        if not valid: return self.send_json({"success":False,"message":result},400)
        if quality not in ALLOWED_QUALITIES and quality != "best": quality="best"
        with DB_LOCK, db() as conn:
            active=conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0]
        if active >= MAX_ACTIVE_JOBS + 4:
            return self.send_json({"success":False,"message":"The download queue is full. Please wait for an active download to finish."},429)
        job_id=uuid.uuid4().hex
        event=threading.Event(); CANCEL_EVENTS[job_id]=event
        t=now()
        job={"id":job_id,"status":"queued","phase":"Queued","progress":0,"downloaded_bytes":0,"total_bytes":0,
             "speed":None,"eta":None,"url":url,"quality":quality,"format":"MP4","platform":detect_platform(url),
             "title":str(body.get("title") or "Preparing media…")[:300],"thumbnail":str(body.get("thumbnail") or "")[:2000],
             "filename":None,"file_size":0,"message":"Your download is queued.","error":None,"created_at":t,"updated_at":t,
             "completed_at":None,"job_dir":None,"file_path":None,"duration":str(body.get("duration") or "")[:100]}
        insert_job(job)
        EXECUTOR.submit(run_download,job_id,url,quality)
        return self.send_json({"success":True,"job":job},202)

    def cancel(self, job_id):
        job=get_job(job_id)
        if not job: return self.send_json({"success":False,"message":"Download job not found or expired."},404)
        if job["status"] in {"ready","failed","cancelled"}: return self.send_json({"success":True,"job":job})
        event=CANCEL_EVENTS.setdefault(job_id,threading.Event()); event.set()
        update_job(job_id,status="cancelled",phase="Cancelled",message="Cancelling download…")
        return self.send_json({"success":True,"job":get_job(job_id)})


def main():
    init_db()
    threading.Thread(target=cleanup_loop, daemon=True, name="drift-cleanup").start()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    print(f"Drift backend listening on http://{HOST}:{PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        EXECUTOR.shutdown(wait=False,cancel_futures=True); server.server_close()


if __name__ == "__main__": main()
