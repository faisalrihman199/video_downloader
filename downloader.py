#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI:   GET  /download  (serves templates/download.html)
API:  POST /api/start
      POST /api/cookies            (upload cookies.txt -> {cookies_id})
      GET  /api/events/<job_id>    (SSE live progress)
      GET  /api/result/<job_id>    (download file)

Run:
  python downloader.py --host 0.0.0.0 --port 8000

Notes:
- Uses yt-dlp + ffmpeg. Install ffmpeg via your OS package manager.
- If you set DOMAIN in your environment or .env, the UI's API docs will show that base.
"""
import argparse, json, os, sys, tempfile, threading, time, uuid
from queue import Queue, Empty
from datetime import datetime
from typing import Any, Dict, Optional

from flask import (
    Flask, request, jsonify, Response, send_file, render_template, abort, stream_with_context
)

# Optional .env support (won't crash if not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from yt_dlp import YoutubeDL
except ImportError:
    print("Missing yt-dlp. Install: pip install yt-dlp", file=sys.stderr)
    sys.exit(1)

# ---------------- In-memory job + cookies store ----------------
JOBS: Dict[str, Dict[str, Any]] = {}      # job_id -> {status, queue, path, title, start, err, cookies_path}
COOKIES: Dict[str, str] = {}              # cookies_id -> absolute path (temp file)

def ffmpeg_present() -> bool:
    import shutil
    return bool(shutil.which("ffmpeg"))

def human(n: Optional[float]) -> str:
    if not isinstance(n, (int, float)): return "?"
    for unit in ["B","KB","MB","GB","TB","PB","EB"]:
        if n < 1024: return f"{n:0.1f} {unit}"
        n /= 1024
    return f"{n:0.1f} ZB"

def send(job_id: str, event: str, data: Dict[str, Any]):
    job = JOBS.get(job_id)
    if job:
        job["queue"].put({"event": event, "data": data})

# ---------------- yt-dlp option builder ----------------
def make_opts(
    job_id: str,
    outdir: str,
    as_audio: bool = False,
    max_height: int = 1080,
    universal: bool = True,
    cookiefile: Optional[str] = None,
    proxy: Optional[str] = None,
    allow_playlist: bool = False,
    quiet: bool = True,
) -> Dict[str, Any]:

    outtmpl = os.path.join(outdir, "%(title).200B [%(id)s].%(ext)s")

    if as_audio:
        fmt = "bestaudio/best"
    else:
        # Prefer H.264 (avc1) + AAC (m4a) MP4; fallback to MP4; then best
        fmt = (
            f"bestvideo[vcodec^=avc1][height<={max_height}][ext=mp4]+"
            f"bestaudio[acodec^=mp4a][ext=m4a]/best[ext=mp4]/best"
        )

    def progress_hook(d):
        status = d.get("status")
        info = d.get("info_dict") or {}
        title = info.get("title", "video")
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        pct = float(downloaded) / float(total) * 100 if total else None
        speed = d.get("speed")
        eta = d.get("eta")

        if status == "downloading":
            send(job_id, "progress", {
                "stage": "downloading",
                "title": title,
                "percent": round(pct, 2) if pct is not None else None,
                "downloaded": human(downloaded),
                "total": human(total) if total else "?",
                "speed": (human(speed) + "/s") if speed else "?",
                "eta": (f"{int(eta)//60:02d}:{int(eta)%60:02d}" if eta else None),
            })
        elif status == "finished":
            send(job_id, "progress", {
                "stage": "processing",
                "title": title,
                "message": "Download finished. Post-processing...",
            })

    def pp_hook(d):
        action = d.get("status", "")
        pp = d.get("postprocessor", "ffmpeg")
        if action == "started":
            send(job_id, "progress", {"stage": "processing", "message": f"{pp}: started"})
        elif action == "finished":
            send(job_id, "progress", {"stage": "processing", "message": f"{pp}: finished"})

    ydl_opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": "mp4" if not as_audio else None,
        "noplaylist": not allow_playlist,
        "quiet": quiet,
        "no_warnings": quiet,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "concurrent_fragment_downloads": 6,
        "overwrites": True,
        "windowsfilenames": True,
        "final_ext": "mp4" if not as_audio else "mp3",
        # Helps on YouTube to avoid some bot checks / consent screens:
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    if as_audio:
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ]
        ydl_opts["postprocessor_args"] = {"FFmpegExtractAudio": ["-movflags", "+faststart"]}
    else:
        if universal:
            ydl_opts["recodevideo"] = "mp4"
            ydl_opts["postprocessor_args"] = {
                "FFmpegVideoConvertor": [
                    "-c:v","libx264","-pix_fmt","yuv420p","-profile:v","high","-level","4.1",
                    "-preset","medium","-crf","20",
                    "-c:a","aac","-b:a","192k","-ac","2",
                    "-movflags","+faststart"
                ],
                "FFmpegMerger": ["-movflags","+faststart"],
                "FFmpegVideoRemuxer": ["-movflags","+faststart"],
            }
        else:
            ydl_opts["postprocessor_args"] = {
                "FFmpegMerger": ["-movflags","+faststart"],
                "FFmpegVideoRemuxer": ["-movflags","+faststart"],
            }

    if cookiefile: ydl_opts["cookiefile"] = cookiefile
    if proxy: ydl_opts["proxy"] = proxy

    return ydl_opts

def expected_path(info, outdir: str, as_audio: bool) -> str:
    title = info.get("title", "video")
    vid = info.get("id", "")
    ext = "mp3" if as_audio else "mp4"
    return os.path.join(outdir, f"{title} [{vid}].{ext}")

def extract_info(url: str, opts: Dict[str, Any]):
    with YoutubeDL({**opts, "skip_download": True, "quiet": True}) as y:
        return y.extract_info(url, download=False)

def do_download(job_id: str, url: str, outdir: str, as_audio: bool, max_height: int,
                universal: bool, cookies: Optional[str], proxy: Optional[str],
                allow_playlist: bool):
    job = JOBS[job_id]
    job["status"] = "running"
    job["start"] = time.time()
    try:
        if not ffmpeg_present():
            send(job_id, "progress", {"stage":"warning", "message":"ffmpeg not found in PATH; some conversions may fail."})
        opts = make_opts(job_id, outdir, as_audio, max_height, universal, cookies, proxy, allow_playlist, quiet=True)
        info = extract_info(url, opts)
        job["title"] = info.get("title", "video")

        # Playlist or single
        if info.get("_type") == "playlist" and allow_playlist:
            entries = [e for e in info.get("entries", []) if e]
            send(job_id, "progress", {"stage":"info","message":f"Playlist with {len(entries)} items"})
            with YoutubeDL(opts) as y:
                y.download([url])

            last = None
            for e in entries:
                guess = expected_path(e, outdir, as_audio)
                if os.path.exists(guess):
                    last = guess
            path = last or (sorted((os.path.join(outdir,f) for f in os.listdir(outdir)), key=os.path.getmtime)[-1] if os.listdir(outdir) else None)
        else:
            with YoutubeDL(opts) as y:
                y.download([url])

            guess = expected_path(info, outdir, as_audio)
            if os.path.exists(guess):
                path = guess
            else:
                vid = info.get("id","")
                path = None
                for fn in os.listdir(outdir):
                    if vid and vid in fn:
                        path = os.path.join(outdir, fn)
                        break
                if not path:
                    files = sorted((os.path.join(outdir,f) for f in os.listdir(outdir)), key=os.path.getmtime)
                    path = files[-1] if files else None

        if not path or not os.path.exists(path):
            raise RuntimeError("Could not locate output file")

        job["path"] = path
        job["status"] = "done"
        send(job_id, "done", {"download_url": f"/api/result/{job_id}", "filename": os.path.basename(path)})
    except Exception as e:
        job["status"] = "error"
        job["err"] = str(e)
        send(job_id, "error", {"message": str(e)})
    finally:
        # secure cleanup of cookies file (if any)
        cpath = job.get("cookies_path")
        if cpath and os.path.exists(cpath):
            try: os.remove(cpath)
            except Exception: pass

# ---------------- Web server (UI + API) ----------------
app = Flask(__name__, template_folder="templates")
ROOT_TMP = tempfile.mkdtemp(prefix="ydl-")
COOKIES_DIR = tempfile.mkdtemp(prefix="cookies-")  # writable on Render (/tmp)

# Global: stop proxies/CDNs from buffering SSE
@app.after_request
def _no_buffer(resp):
    resp.headers.setdefault("Cache-Control", "no-cache, no-transform")
    resp.headers.setdefault("X-Accel-Buffering", "no")  # Nginx
    return resp

@app.get("/")
def download_ui():
    # If DOMAIN not set, detect current request scheme/host
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host  = request.headers.get("X-Forwarded-Host",  request.host)
    detected = f"{proto}://{host}"
    domain = os.environ.get("DOMAIN", detected).rstrip("/")
    return render_template("download.html", DOMAIN=domain)

# ---- Cookies upload (optional, for sites that need auth like YouTube) ----
@app.post("/api/cookies")
def api_cookies_upload():
    """
    Upload a Netscape cookies.txt file (exported from your browser).
    Returns: {"cookies_id": "..."}  -> pass this in /api/start
    """
    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "empty file"}), 400
    cid = uuid.uuid4().hex[:16]
    cpath = os.path.join(COOKIES_DIR, f"{cid}.txt")
    f.save(cpath)
    COOKIES[cid] = cpath
    return jsonify({"cookies_id": cid})

@app.post("/api/start")
def api_start():
    """Accepts form-data (UI) or JSON (Postman). Returns { job_id }."""
    if request.is_json:
        p = request.get_json(silent=True) or {}
        url = (p.get("url") or "").strip()
        as_audio = (p.get("mode","mp4") == "mp3")
        height = int(p.get("height", 1080))
        universal = bool(p.get("universal", True))
        cookies_id = (p.get("cookies_id") or "").strip() or None
        cookies_path = COOKIES.get(cookies_id) if cookies_id else ((p.get("cookies") or "").strip() or None)
        proxy = (p.get("proxy") or "").strip() or None
        allow_playlist = bool(p.get("playlist", False))
    else:
        url = (request.form.get("url") or "").strip()
        as_audio = (request.form.get("mode","mp4") == "mp3")
        height = int(request.form.get("height","1080"))
        universal = request.form.get("universal") is not None
        cookies_id = (request.form.get("cookies_id") or "").strip() or None
        cookies_path = COOKIES.get(cookies_id) if cookies_id else ((request.form.get("cookies") or "").strip() or None)
        proxy = (request.form.get("proxy") or "").strip() or None
        allow_playlist = request.form.get("playlist") is not None

    if not url:
        return jsonify({"error":"missing url"}), 400

    outdir = os.path.join(ROOT_TMP, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "queued",
        "queue": Queue(),
        "path": None,
        "title": None,
        "start": time.time(),
        "err": None,
        "cookies_path": cookies_path,
    }

    t = threading.Thread(
        target=do_download,
        args=(job_id, url, outdir, as_audio, height, universal, cookies_path, proxy, allow_playlist),
        daemon=True
    )
    t.start()
    return jsonify({"job_id": job_id})

@app.get("/api/events/<job_id>")
def api_events(job_id):
    job = JOBS.get(job_id)
    if not job: return ("not found", 404)

    @stream_with_context
    def gen():
        # Padding to force flush through proxies
        yield ":" + (" " * 2048) + "\n\n"
        yield "event: info\ndata: {}\n\n"
        while True:
            if job["status"] in ("done","error") and job["queue"].empty():
                break
            try:
                msg = job["queue"].get(timeout=1.0)
                yield f"event: {msg['event']}\n"
                yield "data: " + json.dumps(msg["data"]) + "\n\n"
            except Empty:
                yield "event: ping\ndata: {}\n\n"

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp

@app.get("/api/result/<job_id>")
def api_result(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    return send_file(job["path"], as_attachment=True)

# ---------------- Entrypoint ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Downloader with realtime progress (SSE)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8000")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    print(f"UI:   http://{args.host}:{args.port}/download")
    print(f"API:  POST /api/start   |  POST /api/cookies   |  GET /api/events/<job_id>  |  GET /api/result/<job_id>")
    app.run(host=args.host, port=int(args.port), threaded=True)
