import asyncio
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
from html import unescape
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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


class ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


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


class WritingPacketRequest(BaseModel):
    lead: Optional[dict[str, Any]] = None
    assessment: Optional[dict[str, Any]] = None


class ArticleImportRequest(BaseModel):
    article: Any
    lead: Optional[dict[str, Any]] = None
    assessment: Optional[dict[str, Any]] = None


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_queue (
            lead_id TEXT PRIMARY KEY,
            source_lead_json TEXT,
            assessment_json TEXT,
            status TEXT NOT NULL CHECK(status IN (
                'commissioned',
                'writing_packet_prepared',
                'article_imported',
                'wp_draft_created',
                'wp_draft_failed'
            )),
            writing_packet_json TEXT,
            writing_packet_markdown TEXT,
            article_json TEXT,
            article_html TEXT,
            headline TEXT,
            slug TEXT,
            excerpt TEXT,
            seo_title TEXT,
            meta_description TEXT,
            focus_keyphrase TEXT,
            related_keyphrases_json TEXT,
            tags_json TEXT,
            category_suggestion TEXT,
            internal_link_notes_json TEXT,
            image_video_notes_json TEXT,
            wp_draft_id INTEGER,
            wp_draft_url TEXT,
            wp_edit_url TEXT,
            wp_yoast_status TEXT,
            wp_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def editorial_conn() -> sqlite3.Connection:
    SCANNER_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SCANNER_DB, factory=ClosingSQLiteConnection)
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


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads_maybe(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def list_production_queue() -> dict[str, dict[str, Any]]:
    if not SCANNER_DB.exists():
        return {}
    with editorial_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM production_queue
            ORDER BY updated_at DESC
            """
        ).fetchall()
    queue: dict[str, dict[str, Any]] = {}
    json_fields = {
        "source_lead_json": "source_lead",
        "assessment_json": "assessment",
        "writing_packet_json": "writing_packet",
        "article_json": "article",
        "related_keyphrases_json": "related_keyphrases",
        "tags_json": "tags",
        "internal_link_notes_json": "internal_link_notes",
        "image_video_notes_json": "image_video_notes",
    }
    list_json_fields = {
        "related_keyphrases_json",
        "tags_json",
        "internal_link_notes_json",
        "image_video_notes_json",
    }
    for row in rows:
        item = dict(row)
        for db_field, api_field in json_fields.items():
            item[api_field] = json_loads_maybe(
                item.pop(db_field, None),
                [] if db_field in list_json_fields else None,
            )
        queue[item["lead_id"]] = item
    return queue


def upsert_production_queue_item(
    conn: sqlite3.Connection,
    lead_id: str,
    status: str = "commissioned",
    lead: dict[str, Any] | None = None,
    assessment: dict[str, Any] | None = None,
) -> None:
    now = utc_iso()
    conn.execute(
        """
        INSERT INTO production_queue
        (lead_id, source_lead_json, assessment_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(lead_id) DO UPDATE SET
            source_lead_json = COALESCE(excluded.source_lead_json, production_queue.source_lead_json),
            assessment_json = COALESCE(excluded.assessment_json, production_queue.assessment_json),
            status = CASE
                WHEN production_queue.status IN ('writing_packet_prepared', 'article_imported', 'wp_draft_created', 'wp_draft_failed')
                THEN production_queue.status
                ELSE excluded.status
            END,
            updated_at = excluded.updated_at
        """,
        (
            lead_id,
            json_dumps(lead) if lead else None,
            json_dumps(assessment) if assessment else None,
            status,
            now,
            now,
        ),
    )


ARTICLE_REQUIRED_FIELDS = {
    "headline",
    "slug",
    "excerpt",
    "article_html",
    "seo_title",
    "meta_description",
    "focus_keyphrase",
    "tags",
    "source_attribution",
    "facts_checked",
    "risks_disclosures",
    "internal_links",
    "embed_media_notes",
}


ARTICLE_SCHEMA = {
    "type": "object",
    "required": sorted(ARTICLE_REQUIRED_FIELDS),
    "properties": {
        "headline": {"type": "string"},
        "slug": {"type": "string"},
        "excerpt": {"type": "string"},
        "article_html": {"type": "string"},
        "seo_title": {"type": "string"},
        "meta_description": {"type": "string"},
        "focus_keyphrase": {"type": "string"},
        "related_keyphrases": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "category_suggestion": {"type": "string"},
        "source_attribution": {"type": "array"},
        "facts_checked": {"type": "array"},
        "risks_disclosures": {"type": "array"},
        "internal_links": {"type": "array"},
        "embed_media_notes": {"type": "array"},
    },
}


def validate_article_payload(payload: Any) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["Article import must be a JSON object."]
    missing = sorted(ARTICLE_REQUIRED_FIELDS - set(payload))
    if missing:
        errors.append(f"Article is missing required fields: {', '.join(missing)}.")
    for field in ["headline", "slug", "excerpt", "article_html", "seo_title", "meta_description", "focus_keyphrase"]:
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            errors.append(f"{field} must be a non-empty string.")
    if "slug" in payload and isinstance(payload["slug"], str) and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", payload["slug"]):
        errors.append("slug must use lowercase letters, numbers and hyphens only.")
    for field in ["tags", "source_attribution", "facts_checked", "risks_disclosures", "internal_links", "embed_media_notes"]:
        if field in payload and not isinstance(payload[field], list):
            errors.append(f"{field} must be a list.")
    if isinstance(payload.get("tags"), list):
        clean_tags = [tag for tag in payload["tags"] if isinstance(tag, str) and tag.strip()]
        if not clean_tags:
            errors.append("tags must contain at least one non-empty string.")
    if "related_keyphrases" in payload and not isinstance(payload["related_keyphrases"], list):
        errors.append("related_keyphrases must be a list when supplied.")
    if "category_suggestion" in payload and payload["category_suggestion"] is not None and not isinstance(payload["category_suggestion"], str):
        errors.append("category_suggestion must be a string when supplied.")
    return not errors, errors


def render_writing_packet_markdown(packet: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# MFO Writing Packet",
            "",
            f"Generated: {packet['generated_at']}",
            f"Lead ID: {packet['lead_id']}",
            "",
            "## Editorial Rules",
            "",
            packet.get("editorial_rules", ""),
            "",
            "## Required Article JSON Schema",
            "",
            "```json",
            json.dumps(packet.get("required_article_schema", {}), indent=2),
            "```",
            "",
            "## Production Brief",
            "",
            "```json",
            json.dumps(
                {
                    "lead": packet.get("lead"),
                    "agent_assessment": packet.get("agent_assessment"),
                    "scanner_evidence": packet.get("scanner_evidence"),
                    "archive_overlap": packet.get("archive_overlap"),
                    "source_links": packet.get("source_links"),
                },
                indent=2,
            ),
            "```",
            "",
            "Return complete JSON only. Do not invent missing facts.",
            "",
        ]
    )


def build_writing_packet(lead_id: str, lead: dict[str, Any] | None, assessment: dict[str, Any] | None) -> dict[str, Any]:
    mfo_index = load_json_file(SCANNER_DIR / "mfo_index.json", {})
    source_links = []
    for key in ["source_url", "url", "video_url", "pubmed_url", "publisher_url"]:
        value = (lead or {}).get(key)
        if isinstance(value, str) and value:
            source_links.append(value)
    for value in (lead or {}).get("evidence_links", []) if isinstance((lead or {}).get("evidence_links"), list) else []:
        if isinstance(value, str) and value and value not in source_links:
            source_links.append(value)
    packet = {
        "packet_schema_version": 1,
        "generated_at": utc_iso(),
        "lead_id": lead_id,
        "purpose": "Manual article-writing packet for an MFO commissioned story.",
        "lead": lead or {"lead_id": lead_id},
        "agent_assessment": assessment or {},
        "scanner_evidence": {
            "lead": lead or {},
            "facts_to_check": (assessment or {}).get("facts_to_check", []),
            "evidence_risk": (assessment or {}).get("evidence_risk", ""),
        },
        "archive_overlap": (lead or {}).get("archive_overlap") or (assessment or {}).get("archive_overlap_warning") or {},
        "source_links": source_links,
        "editorial_rules": (
            "Write for Australian men 35-65. Use source attribution, practical context, caveats, "
            "and clear disclosures. Do not invent missing facts; mark gaps in facts_checked or risks_disclosures."
        ),
        "archive_overlap_information": {
            "refreshed_at": mfo_index.get("refreshed_at") or mfo_index.get("generated_at"),
            "page_count": len(mfo_index.get("pages", [])) if isinstance(mfo_index.get("pages"), list) else None,
        },
        "required_article_schema": ARTICLE_SCHEMA,
    }
    packet["markdown"] = render_writing_packet_markdown(packet)
    return packet


def wp_config() -> dict[str, str]:
    cfg = {
        "base_url": os.getenv("MFO_WP_BASE_URL", "").rstrip("/"),
        "username": os.getenv("MFO_WP_USERNAME", ""),
        "app_password": os.getenv("MFO_WP_APP_PASSWORD", ""),
    }
    missing = [name for name, value in {
        "MFO_WP_BASE_URL": cfg["base_url"],
        "MFO_WP_USERNAME": cfg["username"],
        "MFO_WP_APP_PASSWORD": cfg["app_password"],
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Missing WordPress environment variables: {', '.join(missing)}.")
    return cfg


def wp_request(method: str, path: str, payload: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> dict[str, Any] | list[Any]:
    cfg = wp_config()
    url = f"{cfg['base_url']}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    token = base64.b64encode(f"{cfg['username']}:{cfg['app_password']}".encode("utf-8")).decode("ascii")
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"WordPress {method} {path} failed with HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"WordPress request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def wp_term_id(taxonomy: str, name: str) -> int | None:
    name = name.strip()
    if not name:
        return None
    existing = wp_request("GET", f"/wp-json/wp/v2/{taxonomy}", query={"search": name, "per_page": "20"})
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
                return int(item["id"])
    created = wp_request("POST", f"/wp-json/wp/v2/{taxonomy}", {"name": name})
    if isinstance(created, dict) and created.get("id"):
        return int(created["id"])
    return None


def wp_registered_yoast_meta_keys() -> set[str]:
    try:
        post_type = wp_request("GET", "/wp-json/wp/v2/types/post", query={"context": "edit"})
    except RuntimeError:
        return set()
    schema = post_type.get("schema", {}) if isinstance(post_type, dict) else {}
    meta_props = schema.get("properties", {}).get("meta", {}).get("properties", {})
    return set(meta_props) if isinstance(meta_props, dict) else set()


def create_wp_draft_payload(article: dict[str, Any], tag_ids: list[int], category_ids: list[int], yoast_keys: set[str]) -> tuple[dict[str, Any], str]:
    payload: dict[str, Any] = {
        "status": "draft",
        "title": article["headline"],
        "content": article["article_html"],
        "excerpt": article["excerpt"],
        "slug": article["slug"],
        "tags": tag_ids,
        "categories": category_ids,
    }
    desired_meta = {
        "_yoast_wpseo_title": article["seo_title"],
        "_yoast_wpseo_metadesc": article["meta_description"],
        "_yoast_wpseo_focuskw": article["focus_keyphrase"],
    }
    writable_meta = {key: value for key, value in desired_meta.items() if key in yoast_keys}
    if writable_meta:
        payload["meta"] = writable_meta
    yoast_status = "applied" if set(writable_meta) == set(desired_meta) else "manual_copy_required"
    return payload, yoast_status


def create_wordpress_draft(article: dict[str, Any]) -> dict[str, Any]:
    tag_ids = [
        term_id for term_id in (wp_term_id("tags", tag) for tag in article.get("tags", []))
        if term_id is not None
    ]
    category_ids: list[int] = []
    category = str(article.get("category_suggestion") or "").strip()
    if category:
        category_id = wp_term_id("categories", category)
        if category_id is not None:
            category_ids.append(category_id)
    payload, yoast_status = create_wp_draft_payload(article, tag_ids, category_ids, wp_registered_yoast_meta_keys())
    draft = wp_request("POST", "/wp-json/wp/v2/posts", payload)
    if not isinstance(draft, dict) or not draft.get("id"):
        raise RuntimeError("WordPress did not return a draft post ID.")
    cfg = wp_config()
    draft_id = int(draft["id"])
    fetched = wp_request("GET", f"/wp-json/wp/v2/posts/{draft_id}", query={"context": "edit"})
    draft_url = ""
    if isinstance(fetched, dict):
        draft_url = str(fetched.get("link") or fetched.get("guid", {}).get("rendered") or "")
    if not draft_url:
        draft_url = str(draft.get("link") or "")
    return {
        "wp_draft_id": draft_id,
        "wp_draft_url": draft_url,
        "wp_edit_url": f"{cfg['base_url']}/wp-admin/post.php?post={draft_id}&action=edit",
        "wp_yoast_status": yoast_status,
        "payload": payload,
    }


def require_production_row(conn: sqlite3.Connection, lead_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM production_queue WHERE lead_id = ?", (lead_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Commission this lead before using production actions.")
    return row


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
        "production_queue": list_production_queue(),
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
        if req.decision == "commission":
            upsert_production_queue_item(conn, req.lead_id)
        conn.commit()
    return {"lead_id": req.lead_id, "decision": req.decision, "note": req.note or "", "updated_at": now}


@app.get("/api/editorial/decisions")
def list_editorial_decisions():
    return {"decisions": get_decisions()}


@app.get("/api/editorial/production-queue")
def get_production_queue():
    return {"production_queue": list_production_queue()}


@app.post("/api/editorial/production-queue/{lead_id:path}/writing-packet")
def prepare_writing_packet(lead_id: str, req: Optional[WritingPacketRequest] = None):
    lead = req.lead if req else None
    assessment = req.assessment if req else None
    packet = build_writing_packet(lead_id, lead, assessment)
    now = utc_iso()
    with editorial_conn() as conn:
        require_production_row(conn, lead_id)
        conn.execute(
            """
            UPDATE production_queue
            SET source_lead_json = COALESCE(?, source_lead_json),
                assessment_json = COALESCE(?, assessment_json),
                status = ?,
                writing_packet_json = ?,
                writing_packet_markdown = ?,
                updated_at = ?
            WHERE lead_id = ?
            """,
            (
                json_dumps(lead) if lead else None,
                json_dumps(assessment) if assessment else None,
                "writing_packet_prepared",
                json_dumps({key: value for key, value in packet.items() if key != "markdown"}),
                packet["markdown"],
                now,
                lead_id,
            ),
        )
        conn.commit()
    return {"lead_id": lead_id, "status": "writing_packet_prepared", "packet": packet}


@app.post("/api/editorial/production-queue/{lead_id:path}/article")
def import_article(lead_id: str, req: ArticleImportRequest):
    valid, errors = validate_article_payload(req.article)
    if not valid:
        raise HTTPException(status_code=400, detail={"message": "Malformed article JSON.", "errors": errors})
    article = req.article
    now = utc_iso()
    with editorial_conn() as conn:
        require_production_row(conn, lead_id)
        conn.execute(
            """
            UPDATE production_queue
            SET source_lead_json = COALESCE(?, source_lead_json),
                assessment_json = COALESCE(?, assessment_json),
                status = ?,
                article_json = ?,
                article_html = ?,
                headline = ?,
                slug = ?,
                excerpt = ?,
                seo_title = ?,
                meta_description = ?,
                focus_keyphrase = ?,
                related_keyphrases_json = ?,
                tags_json = ?,
                category_suggestion = ?,
                internal_link_notes_json = ?,
                image_video_notes_json = ?,
                wp_error = NULL,
                updated_at = ?
            WHERE lead_id = ?
            """,
            (
                json_dumps(req.lead) if req.lead else None,
                json_dumps(req.assessment) if req.assessment else None,
                "article_imported",
                json_dumps(article),
                article["article_html"],
                article["headline"],
                article["slug"],
                article["excerpt"],
                article["seo_title"],
                article["meta_description"],
                article["focus_keyphrase"],
                json_dumps(article.get("related_keyphrases", [])),
                json_dumps(article.get("tags", [])),
                article.get("category_suggestion", ""),
                json_dumps(article.get("internal_links", [])),
                json_dumps(article.get("embed_media_notes", [])),
                now,
                lead_id,
            ),
        )
        conn.commit()
    return {"lead_id": lead_id, "status": "article_imported", "article": article}


@app.post("/api/editorial/production-queue/{lead_id:path}/wp-draft")
def create_wordpress_draft_for_queue_item(lead_id: str):
    now = utc_iso()
    with editorial_conn() as conn:
        row = require_production_row(conn, lead_id)
        article = json_loads_maybe(row["article_json"])
        if row["status"] not in {"article_imported", "wp_draft_failed"} or not isinstance(article, dict):
            raise HTTPException(status_code=400, detail="Import a valid article before creating a WordPress draft.")
        valid, errors = validate_article_payload(article)
        if not valid:
            raise HTTPException(status_code=400, detail={"message": "Stored article is invalid.", "errors": errors})
        try:
            draft = create_wordpress_draft(article)
        except RuntimeError as exc:
            conn.execute(
                """
                UPDATE production_queue
                SET status = ?, wp_error = ?, updated_at = ?
                WHERE lead_id = ?
                """,
                ("wp_draft_failed", str(exc), now, lead_id),
            )
            conn.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.execute(
            """
            UPDATE production_queue
            SET status = ?,
                wp_draft_id = ?,
                wp_draft_url = ?,
                wp_edit_url = ?,
                wp_yoast_status = ?,
                wp_error = NULL,
                updated_at = ?
            WHERE lead_id = ?
            """,
            (
                "wp_draft_created",
                draft["wp_draft_id"],
                draft["wp_draft_url"],
                draft["wp_edit_url"],
                draft["wp_yoast_status"],
                now,
                lead_id,
            ),
        )
        conn.commit()
    return {"lead_id": lead_id, "status": "wp_draft_created", **draft}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
