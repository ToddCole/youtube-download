import asyncio
import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

import yt_dlp
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

jobs: dict = {}

STATIC_DIR = Path(__file__).parent / "static"
OUTPUT_DIR = Path.home() / "Downloads" / "youtube"


def make_progress_hook(job_id: str):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = round(downloaded / total * 100, 1) if total else 0
            jobs[job_id].update(
                {
                    "status": "downloading",
                    "percent": percent,
                    "speed": d.get("_speed_str", "").strip(),
                    "eta": d.get("_eta_str", "").strip(),
                }
            )
        elif d["status"] == "finished":
            jobs[job_id].update({"status": "processing", "percent": 100})

    return hook


def make_split_hook(job_id: str, phase: str, offset: float):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = round(offset + (downloaded / total * 50), 1) if total else offset
            jobs[job_id].update(
                {
                    "status": "downloading",
                    "percent": pct,
                    "phase": phase,
                    "speed": d.get("_speed_str", "").strip(),
                    "eta": d.get("_eta_str", "").strip(),
                }
            )
        elif d["status"] == "finished":
            jobs[job_id].update({"status": "processing", "percent": offset + 50})

    return hook


def run_download(job_id: str, url: str, format_type: str, quality: str):
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        base_opts = {
            "outtmpl": str(OUTPUT_DIR / "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }

        if format_type == "split":
            video_fmt = (
                f"bestvideo[height<={quality}][ext=mp4]/bestvideo[height<={quality}]"
                if quality and quality != "best"
                else "bestvideo[ext=mp4]/bestvideo"
            )
            video_opts = {
                **base_opts,
                "format": video_fmt,
                "progress_hooks": [make_split_hook(job_id, "video", 0)],
                "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
            }
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_filename = str(Path(ydl.prepare_filename(info)).with_suffix(".mp4"))

            audio_opts = {
                **base_opts,
                "format": "bestaudio/best",
                "progress_hooks": [make_split_hook(job_id, "audio", 50)],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                audio_filename = str(Path(ydl.prepare_filename(info)).with_suffix(".mp3"))

            jobs[job_id].update(
                {
                    "status": "done",
                    "filename": Path(video_filename).name,
                    "filename2": Path(audio_filename).name,
                }
            )
            return

        if format_type == "mp3":
            ydl_opts = {
                **base_opts,
                "format": "bestaudio/best",
                "progress_hooks": [make_progress_hook(job_id)],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        else:
            fmt = (
                f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
                if quality and quality != "best"
                else "bestvideo+bestaudio/best"
            )
            ydl_opts = {
                **base_opts,
                "format": fmt,
                "progress_hooks": [make_progress_hook(job_id)],
                "merge_output_format": "mp4",
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if format_type == "mp3":
                filename = str(Path(filename).with_suffix(".mp3"))

        jobs[job_id].update({"status": "done", "filename": Path(filename).name})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


@app.get("/api/info")
def get_info(url: str):
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    heights = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") != "none":
            heights.add(h)

    standard = {360, 480, 720, 1080, 1440, 2160}
    available = sorted([h for h in heights if h in standard], reverse=True)

    duration = info.get("duration", 0)
    m, s = divmod(int(duration), 60)

    return {
        "title": info.get("title", "Unknown"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": f"{m}:{s:02d}",
        "uploader": info.get("uploader", ""),
        "qualities": available or [1080, 720, 480, 360],
    }


class DownloadRequest(BaseModel):
    url: str
    format_type: str
    quality: Optional[str] = "720"


@app.post("/api/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid4())
    jobs[job_id] = {"status": "pending", "percent": 0, "speed": "", "eta": ""}
    background_tasks.add_task(
        run_download, job_id, req.url, req.format_type, req.quality
    )
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
async def get_progress(job_id: str):
    async def stream():
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f'data: {json.dumps({"error": "Job not found"})}\n\n'
                break
            yield f"data: {json.dumps(job)}\n\n"
            if job["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
