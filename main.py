import os
import uuid
import tempfile
import shutil
import subprocess
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────
app = FastAPI(title="Sangeet Mix Maker")
executor = ThreadPoolExecutor(max_workers=2)

@app.on_event("startup")
def load_env_cookies():
    """Load cookies securely from Railway Environment Variables so they aren't pushed to GitHub."""
    cookies_content = os.environ.get("YOUTUBE_COOKIES")
    if cookies_content:
        with open("cookies.txt", "w") as f:
            # Fix escaped newlines if passed via some env managers
            content = cookies_content.replace("\\n", "\n")
            f.write(content)
        print("✅ Successfully loaded YOUTUBE_COOKIES from environment variables.")

OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

STATIC_DIR = Path("static")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def parse_time_ms(t: str) -> int:
    """Parse MM:SS, HH:MM:SS, or plain seconds → milliseconds."""
    t = (t or "").strip()
    if not t:
        return 0
    parts = t.split(":")
    try:
        if len(parts) == 1:
            return int(float(parts[0]) * 1000)
        if len(parts) == 2:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
    except (ValueError, IndexError):
        return 0
    return 0


def _yt_download(url: str, out_template: str) -> None:
    """Blocking: download audio from YouTube via yt-dlp."""
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--no-playlist",
        "--no-warnings",
        "--retries", "3",
    ]
    
    # If the user provides a cookies.txt file, use it to bypass the "Sign in" block
    if os.path.exists("cookies.txt"):
        cmd.extend(["--cookies", "cookies.txt"])
        
    cmd.extend([
        "-o", out_template,
        url,
    ])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        # Surface the most useful part of the error
        stderr = result.stderr.strip()
        last_lines = "\n".join(stderr.splitlines()[-5:])
        raise RuntimeError(f"yt-dlp failed:\n{last_lines}")


def _build_mix(infos: list, fade_out_ms: int, fade_in_ms: int, out_path: str) -> None:
    """Blocking: cut clips, apply fades, merge, export MP3."""
    from pydub import AudioSegment

    clips = []
    for info in infos:
        seg = AudioSegment.from_file(info["path"])
        s = info["start_ms"]
        e = info["end_ms"] if info["end_ms"] > 0 else len(seg)
        e = min(e, len(seg))
        if s >= e:
            raise ValueError(
                f"Start time ({s}ms) must be before end time ({e}ms) for {info['path']}"
            )
        clips.append(seg[s:e])

    if not clips:
        raise RuntimeError("No audio clips to process")

    if len(clips) == 1:
        final = clips[0]
    else:
        processed = []
        n = len(clips)
        for i, c in enumerate(clips):
            if i > 0:
                c = c.fade_in(fade_in_ms)
            if i < n - 1:
                c = c.fade_out(fade_out_ms)
            processed.append(c)
        final = processed[0]
        for c in processed[1:]:
            final = final + c

    final.export(out_path, format="mp3", bitrate="192k")


def _find_downloaded_file(work_dir: str, prefix: str) -> Optional[str]:
    """Find a file in work_dir that starts with the given prefix."""
    for f in os.listdir(work_dir):
        if f.startswith(prefix + "."):
            return os.path.join(work_dir, f)
    return None


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process")
async def process_songs(
    background_tasks: BackgroundTasks,
    # ── Song 1 (required) ──────────────────────
    s1_type: str = Form(...),
    s1_url: Optional[str] = Form(None),
    s1_start: str = Form("0:00"),
    s1_end: str = Form(""),
    s1_file: Optional[UploadFile] = File(None),
    # ── Song 2 (optional) ─────────────────────
    s2_type: Optional[str] = Form(None),
    s2_url: Optional[str] = Form(None),
    s2_start: str = Form("0:00"),
    s2_end: str = Form(""),
    s2_file: Optional[UploadFile] = File(None),
    # ── Song 3 (optional) ─────────────────────
    s3_type: Optional[str] = Form(None),
    s3_url: Optional[str] = Form(None),
    s3_start: str = Form("0:00"),
    s3_end: str = Form(""),
    s3_file: Optional[UploadFile] = File(None),
    # ── Settings ──────────────────────────────
    fade_out: int = Form(3000),
    fade_in: int = Form(2000),
    output_name: str = Form("sangeet_mix"),
):
    work_dir = tempfile.mkdtemp()

    try:
        raw_songs = [
            {"n": 1, "type": s1_type, "url": s1_url, "start": s1_start, "end": s1_end, "file": s1_file},
            {"n": 2, "type": s2_type, "url": s2_url, "start": s2_start, "end": s2_end, "file": s2_file},
            {"n": 3, "type": s3_type, "url": s3_url, "start": s3_start, "end": s3_end, "file": s3_file},
        ]

        # ── 1. Read uploaded files first (must be done in async context) ──
        uploads: dict[int, tuple[bytes, str]] = {}
        for s in raw_songs:
            uf = s["file"]
            if s["type"] in ("upload", "up") and uf and uf.filename:
                content = await uf.read()
                if content:
                    uploads[s["n"]] = (content, uf.filename)

        loop = asyncio.get_event_loop()

        # ── 2. Prepare each song (download or save upload) ────────────────
        infos: list[dict] = []
        for s in raw_songs:
            n = s["n"]
            stype = (s["type"] or "").strip()

            if not stype:
                continue

            if stype in ("youtube", "yt"):
                url = (s["url"] or "").strip()
                if not url:
                    continue
                tmpl = os.path.join(work_dir, f"s{n}.%(ext)s")
                try:
                    await loop.run_in_executor(executor, _yt_download, url, tmpl)
                except RuntimeError as e:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Song {n} download failed — try uploading the audio file instead.\n\n{e}",
                    )
                audio_path = _find_downloaded_file(work_dir, f"s{n}")
                if not audio_path:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Song {n}: yt-dlp ran but produced no output file.",
                    )

            elif stype in ("upload", "up"):
                if n not in uploads:
                    continue
                content, fname = uploads[n]
                ext = Path(fname).suffix or ".mp3"
                audio_path = os.path.join(work_dir, f"s{n}{ext}")
                with open(audio_path, "wb") as f:
                    f.write(content)

            else:
                continue

            infos.append(
                {
                    "path": audio_path,
                    "start_ms": parse_time_ms(s["start"]),
                    "end_ms": parse_time_ms(s["end"]),
                }
            )

        if not infos:
            raise HTTPException(status_code=400, detail="No valid songs were provided.")

        # ── 3. Mix in thread pool ─────────────────────────────────────────
        uid = str(uuid.uuid4())[:8]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in output_name)
        out_filename = f"{safe_name}_{uid}.mp3"
        out_path = str(OUTPUT_DIR / out_filename)

        try:
            await loop.run_in_executor(
                executor, _build_mix, infos, fade_out, fade_in, out_path
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=422, detail=str(e))

        background_tasks.add_task(shutil.rmtree, work_dir, True)

        return FileResponse(
            out_path,
            media_type="audio/mpeg",
            filename=out_filename,
        )

    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))
