"""Settings Store module for Views X Pro."""

import json
import os
from typing import Any, Dict

from config import (
    BASE_DIR,
    DEFAULT_RESOLUTION_TIER,
    GEMINI_API_KEY,
    MAX_CLIP_DURATION_SEC,
    MIN_CLIP_DURATION_SEC,
    MIN_CLIP_SPACING_SEC,
    MIN_CONFIDENCE_THRESHOLD,
    RESOLUTION_TIERS,
    TARGET_CLIP_COUNT,
)
from utils import logger

SETTINGS_FILE = BASE_DIR / "user_settings.json"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "llm_provider": "gemini",
    "resolution_tier": DEFAULT_RESOLUTION_TIER,
    "gemini_api_key": GEMINI_API_KEY,
    "gemini_model": "gemini-3.5-flash",
    "target_clip_count": TARGET_CLIP_COUNT,
    "min_confidence_score": MIN_CONFIDENCE_THRESHOLD,
    "min_clip_spacing_seconds": int(MIN_CLIP_SPACING_SEC),
    "min_clip_duration_sec": MIN_CLIP_DURATION_SEC,
    "max_clip_duration_sec": MAX_CLIP_DURATION_SEC,
    # Flash-Forward Hook: show 2s peak preview at start of clip to stop the scroll (default: ON)
    "flash_forward_hook": True,
}


def load_settings() -> Dict[str, Any]:
    """Load user settings from JSON file with default fallback on missing or unreadable file."""
    if not SETTINGS_FILE.exists():
        settings = dict(DEFAULT_SETTINGS)
    else:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            settings = dict(DEFAULT_SETTINGS)
            settings.update(data)
        except Exception as e:
            logger.warning(f"Could not read settings file ({e}). Falling back to defaults.")
            settings = dict(DEFAULT_SETTINGS)

    # Fallback to environment variable only if key is entirely MISSING from saved settings file
    _raw = {}
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as _f:
                _raw = json.load(_f)
        except Exception as raw_read_err:
            logger.warning(f"Could not re-read raw settings for env-var fallback check ({raw_read_err}).")
            _raw = {}
    if "gemini_api_key" not in _raw or not settings.get("gemini_api_key"):
        settings["gemini_api_key"] = os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY)

    settings["llm_provider"] = "gemini"
    return settings


def validate_settings(settings: Dict[str, Any]):
    """Validate settings fields and raise ValueError on invalid values."""
    settings["llm_provider"] = "gemini"

    tier = settings.get("resolution_tier", DEFAULT_RESOLUTION_TIER)
    if tier not in RESOLUTION_TIERS:
        raise ValueError(f"Resolution tier must be one of: {list(RESOLUTION_TIERS.keys())}")

    target_count = settings.get("target_clip_count")
    if not isinstance(target_count, int) or target_count < 1 or target_count > 10:
        raise ValueError("Target clip count must be an integer between 1 and 10.")

    conf = settings.get("min_confidence_score")
    if not isinstance(conf, (int, float)) or conf < 0.0 or conf > 1.0:
        raise ValueError("Minimum confidence score must be a number between 0.0 and 1.0.")

    spacing = settings.get("min_clip_spacing_seconds")
    if not isinstance(spacing, (int, float)) or spacing < 0:
        raise ValueError("Minimum clip spacing must be a non-negative number of seconds.")

    min_dur = settings.get("min_clip_duration_sec")
    max_dur = settings.get("max_clip_duration_sec")
    if not isinstance(min_dur, (int, float)) or min_dur <= 0:
        raise ValueError("Minimum clip duration must be greater than 0 seconds.")
    if not isinstance(max_dur, (int, float)) or max_dur <= 0:
        raise ValueError("Maximum clip duration must be greater than 0 seconds.")
    if min_dur >= max_dur:
        raise ValueError("Minimum clip duration must be strictly less than maximum clip duration.")


def save_settings(settings: Dict[str, Any], validate: bool = True) -> Dict[str, Any]:
    """Validate and persist user settings to user_settings.json."""
    current = load_settings()
    merged = dict(current)
    merged.update(settings)

    if validate:
        validate_settings(merged)

    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        logger.info(f"User settings successfully saved to {SETTINGS_FILE.name}")
        return merged
    except Exception as e:
        logger.error(f"Failed to save user settings: {e}")
        raise RuntimeError(f"Could not save settings: {e}")
