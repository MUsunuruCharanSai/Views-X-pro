"""FastAPI Web Server with Preview Endpoint, Settings, Job History, ZIP Export, Storage Management, Single-Job Lock, Facecam Region Selection, Resolution Tiers, WebSockets, and Lifespan Banner."""

import asyncio
import datetime
import json
import os
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import PipelineOrchestrator
from config import (
    ASSETS_DIR,
    BASE_DIR,
    CLIPS_DIR,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_RESOLUTION_TIER,
    DOWNLOADS_DIR,
    GEMINI_API_KEY,
    LOGS_DIR,
    RESOLUTION_TIERS,
    TEMP_DIR,
    get_available_logos,
)
from facecam_calibration import FacecamCalibrator
from history_store import (
    add_job_to_history,
    delete_history_entry,
    load_history,
    prune_missing_files,
)
from settings_store import load_settings, save_settings
from utils import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Log startup configuration summary banner."""
    settings = load_settings()
    tier = settings.get("resolution_tier", DEFAULT_RESOLUTION_TIER)
    g_key = settings.get("gemini_api_key") or GEMINI_API_KEY
    api_status = f"Google Gemini ({DEFAULT_GEMINI_MODEL}) - {'Key Active' if g_key else 'Key Missing (Fallback to Heuristic)'}"

    logger.info("================================================================")
    logger.info("             VIEWS X PRO — SERVER INITIALIZATION                ")
    logger.info("================================================================")
    logger.info(" Target Hardware        : Intel Core i5-8250U / UHD 620 (QSV `h264_qsv`)")
    logger.info(f" Active LLM Provider    : {api_status}")
    logger.info(f" Selected Resolution    : {tier} ({RESOLUTION_TIERS.get(tier, {}).get('output_w')}x{RESOLUTION_TIERS.get(tier, {}).get('output_h')})")
    logger.info(f" Target Clip Count      : {settings.get('target_clip_count')}")
    logger.info(f" Candidate Clip Spacing : >= {settings.get('min_clip_spacing_seconds')}s")
    logger.info(f" Min Confidence Score   : >= {settings.get('min_confidence_score')}")
    logger.info(f" Clip Duration Range    : {settings.get('min_clip_duration_sec')}s - {settings.get('max_clip_duration_sec')}s")
    logger.info(" Facecam Framing        : Desktop Streamer (Full-Frame 16:9) / IRL (Smart Face Crop)")
    logger.info(" Concurrency Model       : Single-Job Lock (Queue active)")
    try:
        from utils import ensure_ffmpeg_on_path
        ffmpeg_bin = ensure_ffmpeg_on_path()
        logger.info(f" FFmpeg                : {ffmpeg_bin or 'NOT FOUND — downloads/encoding will fail'}")
    except Exception as ff_err:
        logger.warning(f" FFmpeg                : unavailable ({ff_err})")
    try:
        from downloader import youtube_auth_status
        logger.info(f" YouTube yt-dlp Auth   : {youtube_auth_status()}")
    except Exception as auth_err:
        logger.warning(f" YouTube yt-dlp Auth   : unavailable ({auth_err})")
    logger.info("================================================================")
    yield
    logger.info("Server shutting down.")


app = FastAPI(title="Views X Pro Web Interface", version="9.0.0", lifespan=lifespan)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single-job concurrency lock to respect hardware limits (Intel Core i5-8250U, 8GB RAM)
pipeline_lock = asyncio.Lock()

# Active WebSocket connections per job_id
active_websockets: Dict[str, Set[WebSocket]] = {}
job_status_store: Dict[str, Dict[str, Any]] = {}

# Ensure static directory exists
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/clips", StaticFiles(directory=str(CLIPS_DIR)), name="clips")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/health")
async def health():
    """Railway / uptime health check."""
    return {"status": "ok"}


@app.get("/api/logos")
async def list_available_logos():
    """Return available channel brand logos found in assets directory."""
    return {"logos": get_available_logos()}


class PreviewRequest(BaseModel):
    url: str


class ProcessRequest(BaseModel):
    url: str
    api_key: str = ""
    resolution_tier: str = DEFAULT_RESOLUTION_TIER
    facecam_region: Optional[Dict[str, Any]] = None
    selected_logo: Optional[str] = None


class SettingsRequest(BaseModel):
    # Bug 1 fix: All fields optional with None default so partial POST payloads do not wipe existing saved settings/keys
    llm_provider: Optional[str] = None
    resolution_tier: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    target_clip_count: Optional[int] = None
    min_confidence_score: Optional[float] = None
    min_clip_spacing_seconds: Optional[int] = None
    min_clip_duration_sec: Optional[float] = None
    max_clip_duration_sec: Optional[float] = None
    flash_forward_hook: Optional[bool] = None  # 2s peak-preview flash-forward at clip start


async def broadcast_event(job_id: str, event_type: str, data: dict):
    """Broadcast JSON event to all connected WebSocket clients for a job."""
    payload = json.dumps({"type": event_type, "job_id": job_id, **data})
    if job_id in active_websockets:
        dead_sockets = set()
        # Iterate over a snapshot list — the async yield inside send_text() allows concurrent
        # disconnect handlers to call .discard(), which would mutate the set mid-iteration
        # and raise RuntimeError: Set changed size during iteration.
        for ws in list(active_websockets[job_id]):
            try:
                await ws.send_text(payload)
            except Exception as ws_err:
                logger.debug(f"WebSocket send failed for job {job_id} ({ws_err}). Removing dead socket.")
                dead_sockets.add(ws)
        active_websockets[job_id].difference_update(dead_sockets)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return HTMLResponse("<h1>Web Interface Loading...</h1>")


# ============================================================================
# Video Preview Info Endpoint
# ============================================================================

@app.post("/api/preview-info")
async def get_video_preview_info(req: PreviewRequest):
    """Fetch video title, thumbnail, duration, and uploader using lightweight yt-dlp call."""
    if not req.url:
        raise HTTPException(status_code=400, detail="YouTube URL is required.")

    def fetch_meta():
        from downloader import extract_youtube_info
        return extract_youtube_info(req.url)

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, fetch_meta)
        return {
            "title": info.get("title", "YouTube Video"),
            "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{info.get('id')}/hqdefault.jpg",
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader") or info.get("channel", "YouTube Creator")
        }
    except Exception as e:
        logger.warning(f"Could not fetch preview metadata for {req.url}: {e}")
        raise HTTPException(status_code=400, detail=f"Could not fetch video info: {e}")





# ============================================================================
# Settings Endpoints
# ============================================================================

@app.get("/api/settings")
async def get_user_settings():
    return load_settings()


@app.post("/api/settings")
async def update_user_settings(req: SettingsRequest):
    try:
        # Bug 1 fix: Only pass explicitly provided fields so partial updates preserve existing saved settings
        payload = {k: v for k, v in req.dict(exclude_unset=True).items() if v is not None}
        updated = save_settings(payload, validate=True)
        return {"status": "success", "message": "Settings saved successfully.", "settings": updated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {e}")


# ============================================================================
# Job History Endpoints
# ============================================================================

@app.get("/api/history")
async def get_job_history():
    prune_missing_files()
    return load_history()


@app.delete("/api/history/{job_id}")
async def delete_job_history(job_id: str):
    if pipeline_lock.locked():
        raise HTTPException(status_code=400, detail="Cannot delete history while a clipping job is active.")
    success = delete_history_entry(job_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"History entry for job {job_id} not found.")
    return {"status": "success", "message": f"Job {job_id} and associated clip files deleted."}


# ============================================================================
# Streaming ZIP Download Endpoint
# ============================================================================

@app.get("/api/jobs/{job_id}/download-zip")
async def download_job_zip(job_id: str):
    clip_files = sorted(list(CLIPS_DIR.glob(f"{job_id}_Clip_*.mp4")))

    if not clip_files:
        history = load_history()
        for h in history:
            if h.get("job_id") == job_id:
                for c in h.get("clips", []):
                    c_path = CLIPS_DIR / c.get("name", "")
                    if c_path.exists():
                        clip_files.append(c_path)
                break

    if not clip_files:
        raise HTTPException(status_code=404, detail="No clip files found for this job ID.")

    def stream_zip_generator():
        import tempfile
        # Spill to disk if ZIP exceeds 50 MB — prevents memory bomb on 8GB RAM machine
        buffer = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
            for clip_path in clip_files:
                zf.write(clip_path, arcname=clip_path.name)
        buffer.seek(0)
        chunk_size = 4 * 1024 * 1024  # 4 MB chunks
        while True:
            chunk = buffer.read(chunk_size)
            if not chunk:
                break
            yield chunk
        buffer.close()

    filename = f"Shorts_{job_id}.zip"
    return StreamingResponse(
        stream_zip_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ============================================================================
# Storage Management Endpoints
# ============================================================================

@app.get("/api/storage")
async def get_storage_stats():
    total_bytes = 0
    file_count = 0
    dirs_info = {}
    for d_name, d_path in [("downloads", DOWNLOADS_DIR), ("clips", CLIPS_DIR), ("temp", TEMP_DIR), ("logs", LOGS_DIR)]:
        d_bytes = 0
        d_count = 0
        if d_path.exists():
            for f in d_path.rglob("*"):
                if f.is_file():
                    try:
                        size = f.stat().st_size
                    except FileNotFoundError:
                        # File was deleted between is_file() and stat() by concurrent job cleanup
                        continue
                    d_bytes += size
                    d_count += 1
        dirs_info[d_name] = {"count": d_count, "size_mb": round(d_bytes / 1024 / 1024, 2)}
        total_bytes += d_bytes
        file_count += d_count

    return {
        "total_files": file_count,
        "total_size_mb": round(total_bytes / 1024 / 1024, 2),
        "directories": dirs_info
    }


@app.post("/api/storage/clear")
async def clear_storage():
    if pipeline_lock.locked():
        raise HTTPException(status_code=400, detail="Cannot clear storage while a job is running.")

    async with pipeline_lock:
        cleared_count = 0
        for d_path in [DOWNLOADS_DIR, CLIPS_DIR, TEMP_DIR, LOGS_DIR]:
            if d_path.exists():
                for f in d_path.rglob("*"):
                    if f.is_file() and f.name != "app.log":
                        try:
                            f.unlink()
                            cleared_count += 1
                        except Exception as e:
                            logger.warning(f"Could not delete {f}: {e}")

        prune_missing_files()
        return {"status": "success", "cleared_files": cleared_count, "message": "Storage cleared successfully."}


# ============================================================================
# Facecam Preset Endpoints
# ============================================================================

@app.get("/api/presets")
async def list_facecam_presets():
    """List all saved per-channel facecam calibration presets."""
    calibrator = FacecamCalibrator()
    return {"presets": calibrator.list_presets()}


@app.delete("/api/presets/{channel_id}")
async def delete_facecam_preset(channel_id: str):
    """Delete the saved facecam calibration preset for a specific channel."""
    calibrator = FacecamCalibrator()
    removed = calibrator.clear_channel_preset(channel_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No preset found for channel '{channel_id}'.")
    return {"status": "success", "message": f"Preset for '{channel_id}' deleted."}


class CalibrateRequest(BaseModel):
    youtube_url: str
    force: bool = False


@app.post("/api/calibrate")
async def manual_calibrate(req: CalibrateRequest):
    """Manually trigger facecam calibration from a YouTube URL.

    Extracts channel_id, downloads first 3 minutes of video, runs
    MediaPipe calibration, and saves preset to facecam_presets.json.
    """
    if not req.youtube_url:
        raise HTTPException(status_code=400, detail="YouTube URL is required.")
    if pipeline_lock.locked():
        raise HTTPException(status_code=400, detail="Cannot calibrate while a clipping job is running.")

    async with pipeline_lock:
        loop = asyncio.get_running_loop()

        def run_calibration():
            from downloader import extract_youtube_info
            info = extract_youtube_info(req.youtube_url)
            channel_id = info.get("channel_id") or info.get("uploader_id", "")
            video_title = info.get("title", "")
            uploader = info.get("uploader", "")

            if not channel_id:
                return {"status": "error", "message": "Could not extract channel ID from URL."}

            calibrator = FacecamCalibrator()

            # Check if preset already exists
            if not req.force:
                existing = calibrator.load_channel_preset(channel_id)
                if existing:
                    return {
                        "status": "exists",
                        "channel_id": channel_id,
                        "uploader": uploader,
                        "region": existing,
                        "message": f"Preset already exists for '{channel_id}'. Pass force=true to re-calibrate."
                    }

            # Download a short sample (first 180s) of video for calibration at 1080p tier
            from downloader import YouTubeDownloader
            downloader = YouTubeDownloader()
            video_path, audio_path, _, _ch = downloader.download(
                req.youtube_url, resolution_tier="1080p", max_duration_sec=180.0
            )

            try:
                region = calibrator.calibrate_from_video(
                    video_path, channel_id,
                    n_frames=14, scan_duration_sec=150.0, force=req.force
                )
            finally:
                for p in [video_path, audio_path]:
                    if p and p.exists():
                        try:
                            p.unlink()
                        except Exception as unlink_err:
                            logger.debug(f"Temp file cleanup notice during calibration: {unlink_err}")

            if region:
                return {
                    "status": "success",
                    "channel_id": channel_id,
                    "uploader": uploader,
                    "video_title": video_title,
                    "region": region,
                    "message": f"Facecam calibrated for '{uploader}' (channel: {channel_id})."
                }
            else:
                return {
                    "status": "no_facecam",
                    "channel_id": channel_id,
                    "uploader": uploader,
                    "message": "No consistent facecam region detected in this video. Streamer may not use a facecam."
                }

        result = await loop.run_in_executor(None, run_calibration)
        return result



# ============================================================================
# Core Pipeline Execution Endpoint
# ============================================================================

@app.post("/api/process")
async def start_processing(req: ProcessRequest):
    if not req.url:
        raise HTTPException(status_code=400, detail="YouTube URL is required.")

    # Prune old completed/failed jobs if store grows beyond 50 entries
    if len(job_status_store) > 50:
        old_keys = list(job_status_store.keys())[:-40]
        for k in old_keys:
            job_status_store.pop(k, None)
            active_websockets.pop(k, None)

    # Prune empty WebSocket sets for completed jobs
    dead_ws_keys = [k for k, v in active_websockets.items() if not v]
    for k in dead_ws_keys:
        active_websockets.pop(k, None)

    job_id = str(uuid.uuid4())[:8]
    job_status_store[job_id] = {
        "status": "queued",
        "stage": "Queued",
        "progress": 0,
        "logs": [],
        "clips": [],
        "analysis_method": "unknown",
        "attempted_clips": 0,
        "generated_clips": 0,
        "resolution_tier": req.resolution_tier
    }

    asyncio.create_task(
        run_pipeline_background(job_id, req.url, req.api_key, req.facecam_region, req.resolution_tier, req.selected_logo)
    )

    return {"job_id": job_id, "status": "queued", "message": "Job queued successfully."}


@app.get("/api/status/{job_id}")
async def get_job_status(job_id: str):
    """Fetch live status and logs for a specific job."""
    if job_id in job_status_store:
        return job_status_store[job_id]
    
    # Check completed history if pruned from active store
    history = load_history()
    for h in history:
        if h.get("job_id") == job_id:
            return h
            
    raise HTTPException(status_code=404, detail="Job ID not found.")


async def run_pipeline_background(
    job_id: str,
    youtube_url: str,
    api_key: str,
    facecam_region: Optional[Dict[str, float]] = None,
    resolution_tier: str = DEFAULT_RESOLUTION_TIER,
    selected_logo: Optional[str] = None,
):
    loop = asyncio.get_running_loop()

    if pipeline_lock.locked():
        job_status_store[job_id]["stage"] = "Waiting for previous job..."
        await broadcast_event(job_id, "progress", {
            "stage": "Waiting for previous job...",
            "progress": 0,
            "status": "Queued in single-job lock",
            "log": "[Queue] Waiting for previous clipping job to complete..."
        })

    async with pipeline_lock:
        job_status_store[job_id]["status"] = "processing"
        job_status_store[job_id]["stage"] = "Initializing..."

        def sync_progress_cb(stage: str, percent: float, status: str):
            job_status_store[job_id]["stage"] = stage
            job_status_store[job_id]["progress"] = percent
            log_entry = f"[{stage}] {percent:.0f}% - {status}"
            job_status_store[job_id]["logs"].append(log_entry)

            asyncio.run_coroutine_threadsafe(
                broadcast_event(job_id, "progress", {
                    "stage": stage,
                    "progress": percent,
                    "status": status,
                    "log": log_entry
                }),
                loop
            )

        def sync_log_cb(msg: str):
            job_status_store[job_id]["logs"].append(msg)
            asyncio.run_coroutine_threadsafe(
                broadcast_event(job_id, "log", {"log": msg}),
                loop
            )

        video_title = "YouTube Video"
        try:
            orchestrator = PipelineOrchestrator(
                progress_cb=sync_progress_cb,
                log_cb=sync_log_cb,
                api_key=api_key
            )
            
            result = await loop.run_in_executor(
                None, orchestrator.run, youtube_url, job_id, facecam_region, resolution_tier, selected_logo
            )

            output_files = result.get("output_files", [])
            analysis_method = result.get("analysis_method", "unknown")
            attempted_clips = result.get("attempted_clips", 0)
            generated_clips = result.get("generated_clips", 0)
            video_title = result.get("video_title", "YouTube Video")
            eff_tier = result.get("resolution_tier", resolution_tier)
            raw_clips = result.get("raw_clips", [])

            tier_data = RESOLUTION_TIERS.get(eff_tier, RESOLUTION_TIERS[DEFAULT_RESOLUTION_TIER])

            clips_data = []
            for idx, file_path in enumerate(output_files):
                clip_raw = raw_clips[idx] if idx < len(raw_clips) else {}
                clips_data.append({
                    "name": file_path.name,
                    "url": f"/clips/{file_path.name}",
                    "size_mb": round(file_path.stat().st_size / 1024 / 1024, 2),
                    "resolution": f"{tier_data['output_w']}x{tier_data['output_h']}",
                    "tier": eff_tier,
                    "hook_text": clip_raw.get("hook_text", ""),
                    "confidence_score": round(float(clip_raw.get("confidence_score", 0.7)), 3),
                    "hook_strength": round(float(clip_raw.get("hook_strength", 0.7)), 2),
                    "emotional_intensity": round(float(clip_raw.get("emotional_intensity", 0.7)), 2),
                    "unexpectedness": round(float(clip_raw.get("unexpectedness", 0.7)), 2),
                    "payoff_quality": round(float(clip_raw.get("payoff_quality", 0.7)), 2),
                    "explanation": clip_raw.get("explanation", ""),
                    "duration": round(float(clip_raw.get("duration", 30.0)), 1),
                    "start_time": round(float(clip_raw.get("start_time", 0.0)), 1),
                    "clip_index": clip_raw.get("clip_index", idx + 1),
                })

            job_status_store[job_id]["status"] = "completed"
            job_status_store[job_id]["clips"] = clips_data
            job_status_store[job_id]["analysis_method"] = analysis_method
            job_status_store[job_id]["attempted_clips"] = attempted_clips
            job_status_store[job_id]["generated_clips"] = generated_clips
            job_status_store[job_id]["resolution_tier"] = eff_tier

            # Persist to Job History
            history_record = {
                "job_id": job_id,
                "youtube_url": youtube_url,
                "video_title": video_title,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "completed",
                "analysis_method": analysis_method,
                "attempted_clips": attempted_clips,
                "generated_clips": generated_clips,
                "resolution_tier": eff_tier,
                "clips": clips_data
            }
            add_job_to_history(history_record)

            await broadcast_event(job_id, "completed", {
                "stage": "✓ Processing Complete!",
                "progress": 100,
                "clips": clips_data,
                "analysis_method": analysis_method,
                "attempted_clips": attempted_clips,
                "generated_clips": generated_clips,
                "video_title": video_title,
                "resolution_tier": eff_tier
            })

        except Exception as e:
            logger.error(f"Pipeline error for job {job_id}: {e}")
            job_status_store[job_id]["status"] = "failed"
            job_status_store[job_id]["error"] = str(e)

            # Record failed job in history with partial results if any
            history_record = {
                "job_id": job_id,
                "youtube_url": youtube_url,
                "video_title": video_title,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "failed",
                "error": str(e),
                "clips": job_status_store[job_id].get("clips", [])
            }
            add_job_to_history(history_record)

            await broadcast_event(job_id, "failed", {"error": str(e)})


@app.websocket("/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()
    if job_id not in active_websockets:
        active_websockets[job_id] = set()
    active_websockets[job_id].add(websocket)

    if job_id in job_status_store:
        await websocket.send_text(json.dumps({
            "type": "state",
            **job_status_store[job_id]
        }))

    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        # Guard: job_id may have been pruned from active_websockets by cleanup task
        if job_id in active_websockets:
            active_websockets[job_id].discard(websocket)
            if not active_websockets[job_id]:
                active_websockets.pop(job_id, None)


if __name__ == "__main__":
    import uvicorn
    # Bug 5 fix: Gate reload behind APP_ENV=development flag (defaults to False for production stability)
    dev_mode = os.environ.get("APP_ENV", "production") == "development"
    host = "127.0.0.1" if dev_mode else "0.0.0.0"
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host=host, port=port, reload=dev_mode)
