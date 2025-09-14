# Universal Video Downloader (Realtime + Max Compatibility)

Downloads from YouTube, Facebook, TikTok & many more using **yt-dlp**, with:
- **Real-time progress** to the frontend via **SSE** (percent, speed, ETA, processing steps).
- **Plays anywhere**: MP4 (H.264 + AAC, `yuv420p`, `+faststart`). Optional `--universal` re-encode.

> Use only where permitted (your content, with permission, or public domain). Do not bypass DRM or violate site TOS.

---

## Tech
- Python 3.9+
- Flask (web server & SSE)
- yt-dlp (download)
- ffmpeg in PATH (merge/re-encode)

---

## Install
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Install ffmpeg via OS package manager (brew/apt/choco/winget) if you don't already have it.
