"""Configuration module for Views X Pro."""

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Base directories
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CLIPS_DIR = BASE_DIR / "clips"
LOGS_DIR = BASE_DIR / "logs"
TEMP_DIR = BASE_DIR / "temp"
ASSETS_DIR = BASE_DIR / "assets"

# Ensure directories exist
for directory in [DOWNLOADS_DIR, CLIPS_DIR, LOGS_DIR, TEMP_DIR, ASSETS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Logo and subscribe asset paths are resolved dynamically via get_logo_path() / get_subscribe_path()



def get_available_logos() -> list:
    """Scan assets directory for brand logo images/gifs (excluding like/subscribe badges)."""
    valid_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    logos = []
    if ASSETS_DIR.exists():
        for p in sorted(ASSETS_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in valid_exts:
                name_lower = p.stem.lower()
                if name_lower.startswith(("like", "subscribe")):
                    continue
                display_name = p.stem.replace("_", " ").replace("-", " ").title()
                logos.append({
                    "id": p.name,
                    "name": display_name,
                    "filename": p.name,
                    "url": f"/assets/{p.name}?v={int(p.stat().st_mtime)}"
                })
    return logos


def get_logo_path(logo_name: Optional[str] = None) -> Optional[Path]:
    """Find logo image/gif in assets directory.
    
    If logo_name is explicitly provided, look for that exact file or stem.
    If logo_name is 'none', return None.
    Otherwise, fallback to logo, brand, or the first available logo.
    """
    if logo_name and str(logo_name).lower().strip() == "none":
        return None

    if logo_name:
        candidate = ASSETS_DIR / logo_name
        if candidate.exists() and candidate.is_file():
            return candidate
        # Try with extensions
        for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
            c = ASSETS_DIR / f"{logo_name}{ext}"
            if c.exists() and c.is_file():
                return c

    # Fallback order if no specific logo requested or not found
    for stem in ["logo", "brand"]:
        for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
            p = ASSETS_DIR / f"{stem}{ext}"
            if p.exists() and p.is_file():
                return p

    # General fallback: first available logo
    available = get_available_logos()
    if available:
        return ASSETS_DIR / available[0]["filename"]
    return None


def get_subscribe_path() -> Optional[Path]:
    """Find subscribe badge image/gif in assets directory (png, jpg, jpeg, webp, gif)."""
    for ext in [".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        p = ASSETS_DIR / f"subscribe{ext}"
        if p.exists():
            return p
    return None


def get_like_path() -> Optional[Path]:
    """Find like button image/gif in assets directory (png, jpg, jpeg, webp, gif)."""
    for ext in [".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        p = ASSETS_DIR / f"like{ext}"
        if p.exists():
            return p
    return None


# Selectable Resolution Tiers with YouTube-aligned Target/Max Bitrates & Buffer Sizes
RESOLUTION_TIERS = {
    "480p": {
        "height": 480,
        "output_w": 480,
        "output_h": 854,
        "h264_level": "3.1",
        "target_bitrate": "2M",
        "max_bitrate": "2.5M",
        "bufsize": "4M",
        "label": "480p (Low Memory)",
    },
    "540p": {
        "height": 540,
        "output_w": 540,
        "output_h": 960,
        "h264_level": "3.1",
        "target_bitrate": "2.5M",
        "max_bitrate": "3M",
        "bufsize": "5M",
        "label": "540p (Low Memory)",
    },
    "720p": {
        "height": 720,
        "output_w": 720,
        "output_h": 1280,
        "h264_level": "3.1",
        "target_bitrate": "4M",
        "max_bitrate": "5M",
        "bufsize": "8M",
        "label": "720p",
    },
    "1080p": {
        "height": 1080,
        "output_w": 1080,
        "output_h": 1920,
        "h264_level": "4.2",
        "target_bitrate": "8M",
        "max_bitrate": "10M",
        "bufsize": "16M",
        "label": "1080p (Recommended)"
    },
    "1440p": {
        "height": 1440,
        "output_w": 1440,
        "output_h": 2560,
        "h264_level": "5.1",
        "target_bitrate": "16M",
        "max_bitrate": "20M",
        "bufsize": "32M",
        "label": "1440p (High RAM)"
    },
    "4k": {
        "height": 2160,
        "output_w": 2160,
        "output_h": 3840,
        "h264_level": "5.2",
        "target_bitrate": "35M",
        "max_bitrate": "45M",
        "bufsize": "70M",
        "label": "4K (Best Quality, Slower)"
    },
}
# Local default; Railway / FFMPEG_LOW_MEMORY hosts resolve to 480p via resolve_encode_tier()
DEFAULT_RESOLUTION_TIER = "1080p"

# Bounded encode fallback ladder (never retry the same canvas after SIGKILL)
ENCODE_FALLBACK_LADDER = ("720p", "540p", "480p")
LOW_MEMORY_DEFAULT_TIER = "480p"


def is_low_memory_host() -> bool:
    return (
        os.environ.get("FFMPEG_LOW_MEMORY", "").strip() in ("1", "true", "yes")
        or bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    )


# FFmpeg thread budget — single-threaded on low-memory hosts to limit RAM spikes
def get_ffmpeg_thread_count() -> int:
    raw = os.environ.get("FFMPEG_THREADS", "").strip()
    if raw.isdigit():
        return max(1, min(8, int(raw)))
    if is_low_memory_host():
        return 1
    return min(2, os.cpu_count() or 2)


def get_encode_preset() -> str:
    """libx264 preset; ultrafast on low-memory hosts."""
    raw = os.environ.get("FFMPEG_PRESET", "").strip()
    if raw:
        return raw
    return "ultrafast" if is_low_memory_host() else "faster"


def get_encode_crf() -> str:
    """libx264 CRF; higher = smaller/faster on low-memory hosts."""
    raw = os.environ.get("FFMPEG_CRF", "").strip()
    if raw.isdigit():
        return raw
    return "28" if is_low_memory_host() else "18"


def get_encode_audio_bitrate() -> str:
    raw = os.environ.get("FFMPEG_AUDIO_BITRATE", "").strip()
    if raw:
        return raw
    return "96k" if is_low_memory_host() else "192k"


def resolve_download_tier(tier: str) -> str:
    """Source download height — independent of encode canvas.

    On low-memory hosts, still fetch up to 720p (enough for letterbox/crop).
    Never force 480p download just because encode is 480p.
    """
    tier = tier if tier in RESOLUTION_TIERS else DEFAULT_RESOLUTION_TIER
    if is_low_memory_host():
        # Cap heavy downloads to save disk/RAM, but keep usable source quality
        if tier in ("4k", "1440p", "1080p"):
            return "720p"
        if tier in ("720p", "540p", "480p"):
            return tier
        return "720p"
    return tier


def resolve_encode_tier(tier: str) -> str:
    """Resolve output canvas tier. Low-memory hosts default to 480p (override via FFMPEG_ENCODE_TIER)."""
    override = os.environ.get("FFMPEG_ENCODE_TIER", "").strip().lower()
    if override in RESOLUTION_TIERS:
        return override
    tier = tier if tier in RESOLUTION_TIERS else DEFAULT_RESOLUTION_TIER
    if is_low_memory_host():
        # Prefer 480p on Railway; never encode 1080p+ on constrained containers
        if tier in ("4k", "1440p", "1080p"):
            return LOW_MEMORY_DEFAULT_TIER
        if tier in ENCODE_FALLBACK_LADDER:
            return tier
        return LOW_MEMORY_DEFAULT_TIER
    return tier


def get_encode_fallback_ladder(start_tier: str) -> list:
    """Return unique descending canvas tiers to try for one clip (no duplicate retries)."""
    start = start_tier if start_tier in RESOLUTION_TIERS else (
        LOW_MEMORY_DEFAULT_TIER if is_low_memory_host() else DEFAULT_RESOLUTION_TIER
    )
    ladder = list(ENCODE_FALLBACK_LADDER)
    if start in ladder:
        return ladder[ladder.index(start):]
    # High-quality start (1080p+) then step down the low-mem ladder once each
    start_w = RESOLUTION_TIERS[start]["output_w"]
    result = [start]
    for t in ladder:
        if RESOLUTION_TIERS[t]["output_w"] < start_w:
            result.append(t)
    return result


# Quality & Denoise Tuning
DEFAULT_CRF = 18          # CPU libx264 high-quality CRF (local / high-RAM)
DEFAULT_QSV_QUALITY = 18  # QSV high-quality global_quality
DENOISE_FILTER = "hqdn3d=3:2:4:3"  # Optimal spatial/temporal denoise — removes camera sensor grain without blurring

# Target clip duration range & constraints
MIN_CLIP_DURATION_SEC = 15.0
MAX_CLIP_DURATION_SEC = 90.0
TARGET_CLIP_COUNT = 4
MIN_CLIP_SPACING_SEC = 180.0  # 3 minutes minimum gap between clip start times
MIN_CONFIDENCE_THRESHOLD = 0.6  # Minimum confidence score required to include a clip


# Multi-factor scoring weights
SCORING_WEIGHTS = {
    "hook_strength": 0.30,
    "emotional_intensity": 0.30,
    "unexpectedness": 0.20,
    "payoff_quality": 0.20,
}

# Gemini API Config (Google AI Studio)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"  # Primary flagship model (high viral reasoning + 1M context)
FALLBACK_GEMINI_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]  # Ultra-fast secondary fallbacks

# yt-dlp YouTube auth (needed when anonymous downloads are blocked)
# Local: YTDLP_COOKIES_FROM_BROWSER=chrome  (or edge / firefox). Close the browser if cookie lock fails.
# Server/Railway: export Netscape cookies.txt and set YTDLP_COOKIES_FILE=cookies.txt
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "").strip()

# Log config
LOG_FILE_PATH = LOGS_DIR / "app.log"

