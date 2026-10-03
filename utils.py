"""Utility functions and thread-safe logger for Views X Pro."""

import logging
import shutil
import sys
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


class UILogHandler(logging.Handler):
    """Logging handler that emits log messages to a GUI callback function."""

    def __init__(self, callback: Optional[Callable[[str], None]] = None):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        if self.callback:
            self.callback(msg)


def check_ffmpeg() -> bool:
    """Verify FFmpeg installation on the system path."""
    return shutil.which("ffmpeg") is not None


def get_video_dimensions(video_path) -> tuple:
    """Probe video width and height using ffprobe. Returns (width, height)."""
    import subprocess
    try:
        cmd = [
            "ffprobe", "-v", "error",
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
