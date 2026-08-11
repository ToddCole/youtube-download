import asyncio
import json
import re
import sqlite3
import subprocess
import sys
from html import unescape
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

jobs: dict = {}

STATIC_DIR = Path(__file__).parent / "static"
OUTPUT_DIR = Path.home() / "Downloads" / "youtube"
SCANNER_DIR = Path(__file__).parent / "scanner"
SCANNER_DB = SCANNER_DIR / "scanner.db"
CREATOR_JSON = SCANNER_DIR / "reports" / "latest.json"
NEWS_JSON = SCANNER_DIR / "reports" / "news-latest.json"
RESEARCH_JSON = SCANNER_DIR / "reports" / "research-latest.json"
SUPERVISOR_PROMPT = SCANNER_DIR / "editorial_supervisor_prompt.md"
SUPERVISOR_SCHEMA = SCANNER_DIR / "editorial_supervisor_response.schema.json"
STALE_HOURS = 14
VERY_STALE_HOURS = 24

editorial_jobs: dict = {}


def find_downloaded_subtitle(output_dir: Path, base: str, lang: str) -> Path:
    transcript_prefix = f"{base}.{lang}."
    matches = sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file() and path.name.startswith(transcript_prefix)
    )
    subtitle_path = next(
        (path for path in matches if path.suffix == ".srt"),
        matches[0] if matches else None,
    )
    if subtitle_path is None or not subtitle_path.exists():
        raise FileNotFoundError(f"No transcript was downloaded for language '{lang}'.")
    return subtitle_path


def clean_caption_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{\\.*?\}", "", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def srt_to_plain_text(srt_text: str) -> str:
    entries = []
    seen = set()
    blocks = re.split(r"\n\s*\n", srt_text.replace("\r\n", "\n").replace("\r", "\n"))

    for block in blocks:
        caption_lines = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.isdigit():
                continue
            if "-->" in stripped:
                continue
            if stripped.upper() == "WEBVTT":
                continue
            caption_lines.append(stripped)

        caption = clean_caption_text(" ".join(caption_lines))
        normalized = caption.lower()
        if caption and normalized not in seen:
            seen.add(normalized)
            entries.append(caption)

    return "\n".join(entries)


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_upload_date(raw_date: Optional[str]) -> str:
    if not raw_date:
        return ""
    if len(raw_date) == 8 and raw_date.isdigit():
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    return raw_date


def build_mfo_markdown(info: dict, url: str, transcript: str) -> str:
    title = info.get("title") or "Unknown"
    channel = info.get("channel") or info.get("uploader") or ""
    description = (info.get("description") or "").strip()
    video_url = info.get("webpage_url") or url

    return "\n".join(
        [
            f"# {title}",
            "",
            "## Source Metadata",
            f"- Video URL: {video_url}",
            f"- Title: {title}",
            f"- Channel: {channel}",
            f"- Upload Date: {format_upload_date(info.get('upload_date'))}",
            f"- Duration: {format_duration(info.get('duration'))}",
            f"- Description: {description}",
            f"- Video ID: {info.get('id') or ''}",
            "",
            "## Transcript",
            "",
            transcript,
            "",
        ]
    )


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


def run_download(job_id: str, url: str, format_type: str, quality: str, lang: str = "en"):
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        base_opts = {
            "outtmpl": str(OUTPUT_DIR / "%(title)s [%(id)s].%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }

        if format_type == "mfo_pack":
            jobs[job_id].update(
                {"status": "downloading", "percent": 50, "phase": "mfo_pack"}
            )
            ydl_opts = {
                **base_opts,
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [lang],
                "subtitlesformat": "srt/best",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                base = Path(ydl.prepare_filename(info)).stem

            subtitle_path = find_downloaded_subtitle(OUTPUT_DIR, base, lang)
            srt_text = subtitle_path.read_text(encoding="utf-8", errors="replace")
            transcript = srt_to_plain_text(srt_text)

            transcript_path = OUTPUT_DIR / f"{base}.transcript.txt"
            markdown_path = OUTPUT_DIR / f"{base}.mfo-pack.md"
            transcript_path.write_text(transcript + "\n", encoding="utf-8")
            markdown_path.write_text(
                build_mfo_markdown(info, url, transcript), encoding="utf-8"
            )

            jobs[job_id].update(
                {
                    "status": "done",
                    "percent": 100,
                    "filename": subtitle_path.name,
                    "filename2": transcript_path.name,
                    "filename3": markdown_path.name,
                }
            )
            return

        if format_type == "transcript":
            jobs[job_id].update(
                {"status": "downloading", "percent": 50, "phase": "transcript"}
            )
            ydl_opts = {
                **base_opts,
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [lang],
                "subtitlesformat": "srt/best",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                base = Path(ydl.prepare_filename(info)).stem
            transcript_prefix = f"{base}.{lang}."
            matches = sorted(
                path
                for path in OUTPUT_DIR.iterdir()
                if path.is_file() and path.name.startswith(transcript_prefix)
            )
            subtitle_path = next(
                (path for path in matches if path.suffix == ".srt"),
                matches[0] if matches else None,
            )
            if subtitle_path is None or not subtitle_path.exists():
                raise FileNotFoundError(
                    f"No transcript was downloaded for language '{lang}'."
                )
            jobs[job_id].update(
                {"status": "done", "percent": 100, "filename": subtitle_path.name}
            )
            return

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
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    heights = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") != "none":
            heights.add(h)

    standard = {360, 480, 720, 1080, 1440, 2160}
    available = sorted([h for h in heights if h in standard], reverse=True)

    duration = info.get("duration", 0)
    m, s = divmod(int(duration), 60)

    manual_langs = set(info.get("subtitles", {}).keys())
    auto_langs = set(info.get("automatic_captions", {}).keys())
    all_langs = sorted(manual_langs | auto_langs, key=lambda x: (x != "en", x))

    return {
        "title": info.get("title", "Unknown"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": f"{m}:{s:02d}",
        "uploader": info.get("uploader", ""),
        "qualities": available or [1080, 720, 480, 360],
        "transcript_langs": all_langs,
    }


class DownloadRequest(BaseModel):
    url: str
    format_type: Literal["mp4", "mp3", "split", "transcript", "mfo_pack"]
    quality: Optional[str] = "720"
    lang: Optional[str] = "en"


class EditorialDecisionRequest(BaseModel):
    lead_id: str
    decision: Literal["commission", "hold", "reject"]
    note: Optional[str] = ""


class AgentReviewImportRequest(BaseModel):
    response: Any


class ReviewPacketRequest(BaseModel):
    manual_stories: Optional[list[dict[str, Any]]] = None


@app.post("/api/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid4())
    jobs[job_id] = {"status": "pending", "percent": 0, "speed": "", "eta": ""}
    background_tasks.add_task(
        run_download, job_id, req.url, req.format_type, req.quality, req.lang
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


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def ensure_editorial_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS editorial_decisions (
            lead_id TEXT PRIMARY KEY,
            decision TEXT NOT NULL CHECK(decision IN ('commission', 'hold', 'reject')),
            note TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_reviews (
            id TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            validation_status TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scanner_ui_runs (
            id TEXT PRIMARY KEY,
            scanner_type TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT
        )
        """
    )
    conn.commit()


def editorial_conn() -> sqlite3.Connection:
    SCANNER_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SCANNER_DB)
    conn.row_factory = sqlite3.Row
    ensure_editorial_tables(conn)
    return conn


def report_age_status(generated_at: Optional[str]) -> dict:
    if not generated_at:
        return {
            "age_hours": None,
            "is_stale": True,
            "is_very_stale": True,
            "warning": "No report has been generated yet.",
        }
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600
    except ValueError:
        return {
            "age_hours": None,
            "is_stale": True,
            "is_very_stale": True,
            "warning": "Report timestamp could not be read.",
        }
    if age_hours >= VERY_STALE_HOURS:
        warning = f"Report is very stale ({age_hours:.1f} hours old)."
    elif age_hours >= STALE_HOURS:
        warning = f"Report is stale ({age_hours:.1f} hours old)."
    else:
        warning = ""
    return {
        "age_hours": round(age_hours, 2),
        "is_stale": age_hours >= STALE_HOURS,
        "is_very_stale": age_hours >= VERY_STALE_HOURS,
        "warning": warning,
    }


def latest_ui_run(scanner_type: str) -> Optional[dict]:
    if not SCANNER_DB.exists():
        return None
    with editorial_conn() as conn:
        row = conn.execute(
            """
            SELECT scanner_type, status, started_at, finished_at, error
            FROM scanner_ui_runs
            WHERE scanner_type = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (scanner_type,),
        ).fetchone()
    return dict(row) if row else None


def scanner_summary(scanner_type: str, report_path: Path) -> dict:
    payload = load_json_file(report_path, {})
    current_job = next(
        (
            {"job_id": job_id, **job}
            for job_id, job in editorial_jobs.items()
            if job.get("scanner_type") in {scanner_type, "both"}
            and job.get("status") in {"queued", "running"}
        ),
        None,
    )
    generated_at = payload.get("generated_at")
    run = latest_ui_run(scanner_type)
    state = current_job.get("status") if current_job else (run or {}).get("status", "idle")
    error = current_job.get("error") if current_job else (run or {}).get("error")
    age = report_age_status(generated_at)
    return {
        "scanner_type": scanner_type,
        "state": state,
        "last_successful_run": generated_at,
        "lead_count": payload.get("lead_count", 0),
        "viable_count": payload.get("viable_count", 0),
        "report_timestamp": generated_at,
        "report_path": str(report_path),
        "stale": age,
        "error": error,
        "job": current_job,
    }


def concise_scanner_error(stdout: str, stderr: str) -> str:
    text = "\n".join(part for part in [stderr, stdout] if part).strip()
    if not text:
        return "Scanner failed."
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    error_lines = [
        line for line in lines
        if line.startswith("ERROR:") or line.startswith("WARNING:") or "Error:" in line
    ]
    if error_lines:
        return error_lines[-1][-500:]
    terminal = lines[-1]
    if terminal == "KeyboardInterrupt":
        return "Scanner was interrupted before completion."
    return terminal[-500:]


def get_decisions() -> dict:
    if not SCANNER_DB.exists():
        return {}
    with editorial_conn() as conn:
        rows = conn.execute(
            "SELECT lead_id, decision, note, updated_at FROM editorial_decisions"
        ).fetchall()
    return {row["lead_id"]: dict(row) for row in rows}


def run_scanner_job(job_id: str, scanner_type: str) -> None:
    started_at = utc_iso()
    editorial_jobs[job_id].update({"status": "running", "started_at": started_at})
    with editorial_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO scanner_ui_runs
            (id, scanner_type, status, started_at, finished_at, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, scanner_type, "running", started_at, None, None),
        )
        conn.commit()

    args = [sys.executable, "scanner.py", "--no-open-reports"]
    if scanner_type == "creator":
        args.extend(["--skip-news", "--skip-research"])
    elif scanner_type == "news":
        args.extend(["--skip-creator", "--skip-research"])
    elif scanner_type == "research":
        args.extend(["--skip-creator", "--skip-news"])

    try:
        result = subprocess.run(
            args,
            cwd=str(SCANNER_DIR),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        finished_at = utc_iso()
        if result.returncode == 0:
            status = "completed"
            error = ""
        else:
            status = "failed"
            error = concise_scanner_error(result.stdout, result.stderr)
        editorial_jobs[job_id].update(
            {
                "status": status,
                "finished_at": finished_at,
                "returncode": result.returncode,
                "error": error,
            }
        )
    except subprocess.TimeoutExpired:
        finished_at = utc_iso()
        status = "failed"
        error = "Scanner timed out after 30 minutes."
        editorial_jobs[job_id].update({"status": status, "finished_at": finished_at, "error": error})

    with editorial_conn() as conn:
        conn.execute(
            """
            UPDATE scanner_ui_runs
            SET status = ?, finished_at = ?, error = ?
            WHERE id = ?
            """,
            (status, finished_at, error, job_id),
        )
        conn.commit()


def viable_leads(payload: dict) -> list[dict]:
    if payload.get("scanner_type") == "creator":
        allowed = {"new_lead", "follow_up"}
    elif payload.get("scanner_type") == "research":
        allowed = {"viable"}
    else:
        allowed = {"ranked"}
    return [lead for lead in payload.get("leads", []) if lead.get("status") in allowed]


def review_candidate_statuses(scanner_type: str) -> set[str]:
    if scanner_type == "creator":
        return {"new_lead", "follow_up", "rejected"}
    if scanner_type == "news":
        return {"ranked", "skipped"}
    if scanner_type == "research":
        return {"viable", "hold"}
    return {"manual"}


def hard_excluded_statuses(scanner_type: str) -> set[str]:
    if scanner_type == "creator":
        return {"already_covered"}
    if scanner_type == "news":
        return {"already_covered"}
    if scanner_type == "research":
        return {"rejected"}
    return set()


def lead_score_value(lead: dict) -> float:
    value = lead.get("scanner_score", lead.get("score", 0))
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def packet_leads(payload: dict, limit: int = 10) -> tuple[list[dict], list[dict]]:
    scanner_type = payload.get("scanner_type") or ""
    candidates: list[dict] = []
    excluded: list[dict] = []
    candidate_statuses = review_candidate_statuses(scanner_type)
    excluded_statuses = hard_excluded_statuses(scanner_type)
    for index, lead in enumerate(payload.get("leads", []) if isinstance(payload.get("leads"), list) else [], 1):
        if not isinstance(lead, dict):
            continue
        item = {**lead, "raw_scanner_rank": index}
        status = str(item.get("status") or "")
        if status in excluded_statuses:
            excluded.append(item)
        elif status in candidate_statuses:
            candidates.append(item)
        else:
            excluded.append(item)
    candidates = sorted(candidates, key=lambda lead: (lead_score_value(lead), -int(lead.get("raw_scanner_rank", 9999))), reverse=True)
    return candidates[:limit], excluded


def manual_story_candidates(manual_stories: list[dict[str, Any]] | None) -> list[dict]:
    candidates = []
    for index, story in enumerate(manual_stories or [], 1):
        if not isinstance(story, dict):
            continue
        text = str(story.get("text") or "").strip()
        if not text:
            continue
        url_match = re.search(r"https?://\S+", text)
        candidates.append(
            {
                "lead_id": f"manual:{index}",
                "scanner_type": "manual",
                "source_name": "Editor supplied",
                "source_category": "manual",
                "title": text[:140],
                "source_url": url_match.group(0).rstrip(").,") if url_match else "",
                "published_at": None,
                "discovered_at": utc_iso(),
                "scanner_score": None,
                "raw_scanner_rank": index,
                "status": "manual",
                "likely_mfo_angle": "Editor-supplied lead. Verify from primary sources before commissioning.",
                "weakness_or_rejection_reason": "Not discovered by scanner; needs manual validation.",
                "archive_overlap": None,
                "evidence_links": [url_match.group(0).rstrip(").,")] if url_match else [],
            }
        )
    return candidates


def build_review_packet(manual_stories: list[dict[str, Any]] | None = None) -> dict:
    refresh_mfo_index_for_packet()
    creator = load_json_file(CREATOR_JSON, {"scanner_type": "creator", "leads": []})
    news = load_json_file(NEWS_JSON, {"scanner_type": "news", "leads": []})
    research = load_json_file(RESEARCH_JSON, {"scanner_type": "research", "leads": []})
    prompt = SUPERVISOR_PROMPT.read_text(encoding="utf-8") if SUPERVISOR_PROMPT.exists() else ""
    schema = load_json_file(SUPERVISOR_SCHEMA, {})
    editorial_sources = load_json_file(SCANNER_DIR / "editorial_sources.json", {})
    mfo_index = load_json_file(SCANNER_DIR / "mfo_index.json", {})

    creator_candidates, creator_excluded = packet_leads(creator, 10)
    news_candidates, news_excluded = packet_leads(news, 10)
    research_candidates, research_excluded = packet_leads(research, 10)
    manual_candidates = manual_story_candidates(manual_stories)
    packet = {
        "packet_schema_version": 2,
        "generated_at": utc_iso(),
        "purpose": "Manual MFO Editorial Supervisor review packet.",
        "scanner_metadata": {
            "creator": {
                "generated_at": creator.get("generated_at"),
                "lead_count": creator.get("lead_count", 0),
                "candidate_count": len(creator_candidates),
                "excluded_count": len(creator_excluded),
                "report_path": creator.get("report_path"),
            },
            "news": {
                "generated_at": news.get("generated_at"),
                "lead_count": news.get("lead_count", 0),
                "candidate_count": len(news_candidates),
                "excluded_count": len(news_excluded),
                "report_path": news.get("report_path"),
            },
            "research": {
                "generated_at": research.get("generated_at"),
                "lead_count": research.get("lead_count", 0),
                "candidate_count": len(research_candidates),
                "excluded_count": len(research_excluded),
                "report_path": research.get("report_path"),
            },
            "manual": {
                "generated_at": utc_iso(),
                "lead_count": len(manual_candidates),
                "candidate_count": len(manual_candidates),
                "excluded_count": 0,
            },
        },
        "editorial_supervisor_prompt": prompt,
        "required_response_schema": schema,
        "review_candidates": {
            "creator": creator_candidates,
            "news": news_candidates,
            "research": research_candidates,
            "manual": manual_candidates,
        },
        "excluded_candidates": {
            "creator": creator_excluded,
            "news": news_excluded,
            "research": research_excluded,
            "manual": [],
        },
        "scanner_results": {
            "creator": {"metadata": creator.get("metadata", {}), "leads": creator_candidates},
            "news": {"metadata": news.get("metadata", {}), "leads": news_candidates},
            "research": {"metadata": research.get("metadata", {}), "leads": research_candidates},
            "manual": {"metadata": {}, "leads": manual_candidates},
        },
        "archive_overlap_information": {
            "index_path": str(SCANNER_DIR / "mfo_index.json"),
            "refreshed_at": mfo_index.get("refreshed_at") or mfo_index.get("generated_at"),
            "page_count": len(mfo_index.get("pages", [])) if isinstance(mfo_index.get("pages"), list) else None,
        },
        "editorial_source_information": editorial_sources,
        "instructions": "Assess every lead in review_candidates. Return valid JSON with reviewed_candidates for every supplied lead and recommended_ids separately. Do not wrap it in Markdown fences.",
    }
    markdown = render_review_packet_markdown(packet)
    return {"packet": packet, "markdown": markdown}


def refresh_mfo_index_for_packet() -> None:
    scanner = SCANNER_DIR / "scanner.py"
    if not scanner.exists():
        return
    try:
        subprocess.run(
            [
                sys.executable,
                "scanner.py",
                "--refresh-mfo-index",
                "--skip-creator",
                "--skip-news",
                "--skip-research",
                "--no-open-reports",
            ],
            cwd=str(SCANNER_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception:
        return


def render_review_packet_markdown(packet: dict) -> str:
    lines = [
        "# MFO Editorial Supervisor Review Packet",
        "",
        f"Generated: {packet['generated_at']}",
        "",
        "## Prompt",
        "",
        packet.get("editorial_supervisor_prompt", ""),
        "",
        "## Required Response Schema",
        "",
        "```json",
        json.dumps(packet.get("required_response_schema", {}), indent=2),
        "```",
        "",
        "## Scanner Data",
        "",
        "```json",
        json.dumps(
            {
                "scanner_metadata": packet.get("scanner_metadata"),
                "review_candidates": packet.get("review_candidates"),
                "excluded_candidates": packet.get("excluded_candidates"),
                "archive_overlap_information": packet.get("archive_overlap_information"),
                "editorial_source_information": packet.get("editorial_source_information"),
            },
            indent=2,
        ),
        "```",
        "",
    ]
    return "\n".join(lines)


REVIEWED_CANDIDATE_REQUIRED_FIELDS = {
    "lead_id",
    "scanner_type",
    "agent_rating",
    "concise_reason",
    "mfo_angle",
    "evidence_risk",
    "archive_overlap_warning",
    "editorial_rank",
    "why_editorial_ranking_differs",
    "recommended_action",
}


def validate_agent_review_payload(payload: Any) -> tuple[bool, list[str]]:
    errors = []
    if not isinstance(payload, dict):
        return False, ["Agent response must be a JSON object."]
    reviewed = payload.get("reviewed_candidates")
    recommended_ids = payload.get("recommended_ids")
    if not isinstance(recommended_ids, list):
        errors.append("recommended_ids must be a list.")
    elif len(recommended_ids) > 6:
        errors.append("recommended_ids must contain no more than six items.")
    else:
        for index, item in enumerate(recommended_ids, 1):
            if not isinstance(item, str) or not item:
                errors.append(f"recommended_ids[{index}] must be a non-empty string.")
    if not isinstance(reviewed, list):
        errors.append("reviewed_candidates must be a list.")
    else:
        seen_ids = set()
        for index, item in enumerate(reviewed, 1):
            if not isinstance(item, dict):
                errors.append(f"reviewed_candidates[{index}] must be an object.")
                continue
            missing = sorted(REVIEWED_CANDIDATE_REQUIRED_FIELDS - set(item))
            if missing:
                errors.append(f"reviewed_candidates[{index}] is missing: {', '.join(missing)}.")
            lead_id = item.get("lead_id")
            if isinstance(lead_id, str):
                seen_ids.add(lead_id)
            if item.get("agent_rating") not in {"Strong", "Possible", "Weak"}:
                errors.append(f"reviewed_candidates[{index}].agent_rating must be Strong, Possible or Weak.")
            if item.get("recommended_action") not in {"commission", "hold", "reject"}:
                errors.append(f"reviewed_candidates[{index}].recommended_action must be commission, hold or reject.")
        if isinstance(recommended_ids, list):
            missing_recommended = [lead_id for lead_id in recommended_ids if isinstance(lead_id, str) and lead_id not in seen_ids]
            if missing_recommended:
                errors.append(f"recommended_ids not found in reviewed_candidates: {', '.join(missing_recommended)}.")
    return not errors, errors


@app.post("/api/editorial/scans/{scanner_type}")
def start_editorial_scan(scanner_type: Literal["creator", "news", "research", "both"], background_tasks: BackgroundTasks):
    if not (SCANNER_DIR / "scanner.py").exists():
        raise HTTPException(status_code=500, detail="scanner.py was not found.")
    running = [
        job for job in editorial_jobs.values()
        if job.get("status") in {"queued", "running"}
    ]
    if running:
        raise HTTPException(status_code=409, detail="A scanner is already running.")
    job_id = str(uuid4())
    editorial_jobs[job_id] = {
        "scanner_type": scanner_type,
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "error": "",
    }
    background_tasks.add_task(run_scanner_job, job_id, scanner_type)
    return {"job_id": job_id, "scanner_type": scanner_type, "status": "queued"}


@app.get("/api/editorial/scans/status")
def get_editorial_scan_status():
    return {
        "creator": scanner_summary("creator", CREATOR_JSON),
        "news": scanner_summary("news", NEWS_JSON),
        "research": scanner_summary("research", RESEARCH_JSON),
        "jobs": editorial_jobs,
    }


@app.get("/api/editorial/results")
def get_editorial_results():
    return {
        "status": get_editorial_scan_status(),
        "creator": load_json_file(CREATOR_JSON, {"scanner_type": "creator", "leads": []}),
        "news": load_json_file(NEWS_JSON, {"scanner_type": "news", "leads": []}),
        "research": load_json_file(RESEARCH_JSON, {"scanner_type": "research", "leads": []}),
        "decisions": get_decisions(),
    }


@app.post("/api/editorial/review-packet")
def prepare_editorial_review_packet(req: Optional[ReviewPacketRequest] = None):
    return build_review_packet((req.manual_stories if req else None) or [])


@app.post("/api/editorial/agent-review")
def import_agent_review(req: AgentReviewImportRequest):
    valid, errors = validate_agent_review_payload(req.response)
    if not valid:
        raise HTTPException(status_code=400, detail={"message": "Malformed supervisor response.", "errors": errors})
    review_id = str(uuid4())
    with editorial_conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_reviews (id, imported_at, raw_json, validation_status)
            VALUES (?, ?, ?, ?)
            """,
            (review_id, utc_iso(), json.dumps(req.response), "valid"),
        )
        conn.commit()
    return {"review_id": review_id, "response": req.response}


@app.post("/api/editorial/decisions")
def save_editorial_decision(req: EditorialDecisionRequest):
    now = utc_iso()
    with editorial_conn() as conn:
        conn.execute(
            """
            INSERT INTO editorial_decisions (lead_id, decision, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lead_id) DO UPDATE SET
                decision = excluded.decision,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (req.lead_id, req.decision, req.note or "", now),
        )
        conn.commit()
    return {"lead_id": req.lead_id, "decision": req.decision, "note": req.note or "", "updated_at": now}


@app.get("/api/editorial/decisions")
def list_editorial_decisions():
    return {"decisions": get_decisions()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
