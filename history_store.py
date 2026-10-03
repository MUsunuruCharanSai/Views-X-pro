"""History Store module for Views X Pro."""

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from config import BASE_DIR, CLIPS_DIR
from utils import logger

HISTORY_FILE = BASE_DIR / "job_history.json"
MAX_HISTORY_ITEMS = 50


def _safe_unlink(clip_path: Path, max_retries: int = 3, delay_sec: float = 0.2) -> bool:
    """Bug 7 fix: Retry-with-backoff around Path.unlink() for Windows file-lock issues."""
    for attempt in range(1, max_retries + 1):
        try:
            if clip_path.exists():
                clip_path.unlink()
                logger.info(f"Deleted clip file: {clip_path.name}")
                return True
        except (PermissionError, OSError) as e:
            if attempt < max_retries:
                time.sleep(delay_sec)
            else:
                logger.warning(f"Could not delete {clip_path} after {max_retries} attempts: {e}")
    return False


def load_history() -> List[Dict[str, Any]]:
    """Load job history records from JSON file."""
    if not HISTORY_FILE.exists():
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Could not read history file ({e}). Returning empty history.")
        return []


def save_history(records: List[Dict[str, Any]]):
    """Save history records array to job_history.json."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write history file: {e}")


def add_job_to_history(job_record: Dict[str, Any]):
    """Add a new job record to history, cap at MAX_HISTORY_ITEMS (50), and persist."""
    records = load_history()
    job_id = job_record.get("job_id")

    # Remove existing record with same job_id if present
    records = [r for r in records if r.get("job_id") != job_id]

    # Prepend new job record
    records.insert(0, job_record)

    # Cap at MAX_HISTORY_ITEMS
    if len(records) > MAX_HISTORY_ITEMS:
        records = records[:MAX_HISTORY_ITEMS]

    save_history(records)
    logger.info(f"Job {job_id} added to job_history.json (total history records: {len(records)})")


def delete_history_entry(job_id: str) -> bool:
    """Delete history entry for job_id AND remove its clip files from disk."""
    records = load_history()
    target_record = None

    for r in records:
        if r.get("job_id") == job_id:
            target_record = r
            break

    if not target_record:
        logger.warning(f"History entry for job {job_id} not found.")
        return False

    # Delete clip files on disk associated with this job
    clips = target_record.get("clips", [])
    for clip in clips:
        clip_name = clip.get("name")
        if clip_name:
            _safe_unlink(CLIPS_DIR / clip_name)

    # Also clean up any lingering clips matching pattern `{job_id}_Clip_*.mp4`
    for clip_path in CLIPS_DIR.glob(f"{job_id}_Clip_*.mp4"):
        _safe_unlink(clip_path)

    # Save updated records
    updated_records = [r for r in records if r.get("job_id") != job_id]
    save_history(updated_records)
    logger.info(f"Deleted history record for job_id: {job_id}")
    return True


def prune_missing_files():
    """Scans history entries and updates/removes entries whose clip files no longer exist on disk.
    Failed jobs older than 7 days are also pruned to prevent history bloat.
    """
    import datetime
    records = load_history()
    pruned_records = []
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    ).isoformat()

    for r in records:
        clips = r.get("clips", [])
        existing_clips = [
            clip for clip in clips
            if clip.get("name") and (CLIPS_DIR / clip["name"]).exists()
        ]

        is_recent_failure = (
            r.get("status") == "failed" and
            r.get("timestamp", "") >= cutoff
        )

        if existing_clips or is_recent_failure:
            r["clips"] = existing_clips
            r["generated_clips"] = len(existing_clips)
            pruned_records.append(r)
        else:
            logger.info(f"Pruned history entry {r.get('job_id')} (no clips on disk or old failure)")

    save_history(pruned_records)
    return pruned_records
