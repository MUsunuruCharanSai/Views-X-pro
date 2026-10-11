"""Utility functions and thread-safe logger for Views X Pro."""

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from config import LOG_FILE_PATH

# Force UTF-8 stdout on Windows to avoid cp1252 UnicodeEncodeError from emoji/arrows in logs
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configure logging
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, encoding="utf-8"),
        _stream_handler,
    ]
)

logger = logging.getLogger("ViewsXPro")

_FFMPEG_BIN: Optional[str] = None
_FFPROBE_BIN: Optional[str] = None


class UILogHandler(logging.Handler):
    """Logging handler that emits log messages to a GUI callback function."""

    def __init__(self, callback: Optional[Callable[[str], None]] = None):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        if self.callback:
            self.callback(msg)


def _candidate_ffmpeg_bins() -> list:
    """Build candidate ffmpeg.exe paths (PATH first, then common Windows installs)."""
    found = []
    which = shutil.which("ffmpeg")
    if which:
        found.append(Path(which))

    env_ffmpeg = os.environ.get("FFMPEG_PATH") or os.environ.get("FFMPEG_BINARY")
    if env_ffmpeg:
        found.append(Path(env_ffmpeg))

    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    winget_root = Path(local) / "Microsoft" / "WinGet" / "Packages" if local else None

    extras = [
        Path(program_files) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\tools\ffmpeg\bin\ffmpeg.exe"),
    ]
    if winget_root and winget_root.is_dir():
        extras.extend(winget_root.glob("Gyan.FFmpeg*\ffmpeg-*\bin\ffmpeg.exe"))
        extras.extend(winget_root.glob("Gyan.FFmpeg*\*\bin\ffmpeg.exe"))

    found.extend(extras)
    return found


def resolve_ffmpeg_bin() -> Optional[str]:
    """Locate ffmpeg binary; caches result and prefers PATH / FFMPEG_PATH / WinGet."""
    global _FFMPEG_BIN
    if _FFMPEG_BIN and Path(_FFMPEG_BIN).is_file():
        return _FFMPEG_BIN

    for cand in _candidate_ffmpeg_bins():
        try:
            if cand.is_file():
                _FFMPEG_BIN = str(cand.resolve())
                return _FFMPEG_BIN
        except OSError:
            continue
    return None


def resolve_ffprobe_bin() -> Optional[str]:
    """Locate ffprobe next to ffmpeg or on PATH."""
    global _FFPROBE_BIN
    if _FFPROBE_BIN and Path(_FFPROBE_BIN).is_file():
        return _FFPROBE_BIN

    which = shutil.which("ffprobe")
    if which:
        _FFPROBE_BIN = which
        return _FFPROBE_BIN

    ffmpeg = resolve_ffmpeg_bin()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            _FFPROBE_BIN = str(sibling)
            return _FFPROBE_BIN
    return None


def ensure_ffmpeg_on_path() -> Optional[str]:
    """Ensure ffmpeg/ffprobe directories are on PATH for yt-dlp and subprocess calls."""
    ffmpeg = resolve_ffmpeg_bin()
    if not ffmpeg:
        return None

    bin_dir = str(Path(ffmpeg).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in path_parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        # Clear which() cache indirectly by not relying on prior negative lookups
    return ffmpeg


def check_ffmpeg() -> bool:
    """Verify FFmpeg is available (PATH or common install locations)."""
    return ensure_ffmpeg_on_path() is not None


def get_video_dimensions(video_path) -> tuple:
    """Probe video width and height using ffprobe. Returns (width, height)."""
    import subprocess

    ensure_ffmpeg_on_path()
    ffprobe = resolve_ffprobe_bin() or "ffprobe"
    try:
        cmd = [
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(video_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("x")
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    except Exception as e:
        logger.warning(f"ffprobe dimension error for {getattr(video_path, 'name', video_path)}: {e}")
    return 1920, 1080
