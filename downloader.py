import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
import yt_dlp

from config import (
    BASE_DIR,
    DEFAULT_RESOLUTION_TIER,
    DOWNLOADS_DIR,
    RESOLUTION_TIERS,
    TEMP_DIR,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
)
from utils import logger


def ytdlp_cookie_opts() -> Dict[str, Any]:
    """Build yt-dlp cookie options from env (browser cookies locally, cookies.txt on servers)."""
    opts: Dict[str, Any] = {}
    if YTDLP_COOKIES_FILE:
        cookie_path = Path(YTDLP_COOKIES_FILE)
        if not cookie_path.is_absolute():
            cookie_path = BASE_DIR / cookie_path
        if cookie_path.is_file():
            opts["cookiefile"] = str(cookie_path)
            logger.info(f"yt-dlp auth: using cookies file ({cookie_path.name})")
        else:
            logger.warning(f"YTDLP_COOKIES_FILE set but file not found: {cookie_path}")
    elif YTDLP_COOKIES_FROM_BROWSER:
        # Supports "chrome", "edge", "firefox", or "chrome:Profile Name"
        parts = tuple(p for p in YTDLP_COOKIES_FROM_BROWSER.split(":") if p)
        opts["cookiesfrombrowser"] = parts or (YTDLP_COOKIES_FROM_BROWSER,)
        logger.info(f"yt-dlp auth: using cookies from browser ({YTDLP_COOKIES_FROM_BROWSER})")
    return opts


class YouTubeDownloader:
    """Downloads YouTube video and extracts WAV audio for analysis."""

    def __init__(self, progress_callback: Optional[Callable[[float, str], None]] = None):
        self.progress_callback = progress_callback
        self._last_percent = -1.0
        self._last_emit_time = 0.0

    def _ydl_hook(self, d: Dict[str, any]):
        if d.get("status") == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total_bytes > 0:
                percent = (downloaded / total_bytes) * 100.0
                now = time.time()
                # Progress smoothing: emit only if percent changed by >=1% or >=500ms elapsed
                if (percent - self._last_percent >= 1.0) or (now - self._last_emit_time >= 0.5) or percent >= 100.0:
                    self._last_percent = percent
                    self._last_emit_time = now
                    speed_str = d.get("_speed_str", "").strip()
                    eta_str = d.get("_eta_str", "").strip()
                    msg = f"Downloading video... {percent:.1f}% ({speed_str}, ETA: {eta_str})"
                    logger.info(msg)
                    if self.progress_callback:
                        self.progress_callback(percent, msg)

    def download(
        self,
        url: str,
        resolution_tier: str = DEFAULT_RESOLUTION_TIER,
        max_duration_sec: Optional[float] = None
    ) -> Tuple[Path, Optional[Path], str, str]:
        """Download MP4 video capped at specified resolution tier and extract 16kHz WAV audio.

        If max_duration_sec is specified (e.g. 180s for calibration), yt-dlp only downloads
        the leading N seconds instead of the full video.
        
        Returns:
            Tuple[Path, Path, str, str]: (video_path, audio_path, title, channel_id)
        """
        tier_info = RESOLUTION_TIERS.get(resolution_tier, RESOLUTION_TIERS[DEFAULT_RESOLUTION_TIER])
        max_height = tier_info["height"]

        dur_label = f" (first {int(max_duration_sec)}s)" if max_duration_sec else ""
        logger.info(f"Initiating yt-dlp download for ({resolution_tier} max {max_height}p{dur_label}): {url}")
        if self.progress_callback:
            self.progress_callback(5.0, f"Fetching video metadata ({resolution_tier}{dur_label})...")

        # Check if full video already exists in DOWNLOADS_DIR (skip re-downloading across runs / logo switches)
        import re
        vid_match = re.search(r"(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
        video_id = vid_match.group(1) if vid_match else None
        if video_id and not max_duration_sec:
            existing = list(DOWNLOADS_DIR.glob(f"{video_id}_*.mp4")) + list(DOWNLOADS_DIR.glob(f"{video_id}.mp4"))
            for cand in existing:
                if cand.is_file() and cand.stat().st_size > 10 * 1024 * 1024:
                    logger.info(f"Using existing downloaded video: {cand.name} ({cand.stat().st_size / (1024*1024):.1f} MB)")
                    if self.progress_callback:
                        self.progress_callback(100.0, f"Using existing video: {cand.name}")
                    try:
                        meta_opts = {"quiet": True, "skip_download": True, **ytdlp_cookie_opts()}
                        with yt_dlp.YoutubeDL(meta_opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                            title = info.get("title", cand.stem)
                            channel_id = info.get("channel_id") or info.get("uploader_id", "")
                    except Exception:
                        title = cand.stem.split("_", 1)[1] if "_" in cand.stem else cand.stem
                        channel_id = ""
                    return cand, None, title, channel_id

        out_template = str(DOWNLOADS_DIR / "%(id)s_%(title).50s.%(ext)s")
        sub_template = str(TEMP_DIR / "%(id)s.%(ext)s")

        # Bug 1 & 1440p fix: Split video+audio stream selection prioritizing highest height video stream (up to max_height)
        format_str = (
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}]/"
            f"best[height<={max_height}][ext=mp4]/"
            f"best"
        )
        fallback_format_str = (
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}]/"
            f"best"
        )

        # Bug 11 fix: Pass player_client=["android", "ios"] directly in primary ydl_opts to prevent
        # 403 Forbidden errors on standard web client and avoid 15-20s retry overhead on every job.
        # Bug 11 & 13 fix: Include web_embedded + ejs:github remote components to unlock full 1080p/1440p/4K
        # source resolutions without triggering 403 Forbidden errors or 360p mobile caps.
        ydl_opts = {
            "format": format_str,
            "outtmpl": {"default": out_template, "subtitle": sub_template},
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [self._ydl_hook],
            "nocheckcertificate": True,
            "socket_timeout": 30,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 8,  # Multi-threaded parallel stream download (3x-5x speedup)
            "http_chunk_size": 10485760,  # 10MB chunk bursts to bypass YouTube rate-limiting
            "buffersize": 2097152,
            "writeautosub": True,
            "subtitleslangs": ["en", "en-US", "en-GB", "a.en"],
            "subtitlesformat": "vtt",
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "mweb", "web_embedded"]
                }
            },
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-us,en;q=0.5",
            }
        }
        ydl_opts.update(ytdlp_cookie_opts())

        # Bug 7 fix: If max_duration_sec provided, restrict download range to first N seconds
        if max_duration_sec and max_duration_sec > 0:
            from yt_dlp.utils import download_range_func
            ydl_opts["download_ranges"] = download_range_func(None, [(0, max_duration_sec)])
            ydl_opts["force_keyframes_at_cuts"] = True

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_id = info.get("id", "video")
                title = info.get("title", "YouTube Video")
                channel_id = info.get("channel_id") or info.get("uploader_id", "")
                filename = ydl.prepare_filename(info)
                video_path = Path(filename)
                if not video_path.exists():
                    alt_path = video_path.with_suffix(".mp4")
                    if alt_path.exists():
                        video_path = alt_path
                    else:
                        raise RuntimeError(
                            f"Downloaded video file not found at expected path: {video_path} "
                            f"(also checked {alt_path}). yt-dlp may have failed silently."
                        )
        except Exception as e:
            logger.warning(f"Primary download attempt failed ({e}). Retrying with fallback player clients...")
            fallback_clients = [["ios"], ["mweb"], ["web"]]
            download_success = False

            for client in fallback_clients:
                try:
                    fallback_opts = {
                        "format": fallback_format_str,
                        "outtmpl": {"default": out_template, "subtitle": sub_template},
                        "merge_output_format": "mp4",
                        "quiet": True,
                        "no_warnings": True,
                        "nocheckcertificate": True,
                        "socket_timeout": 30,
                        "retries": 10,
                        "concurrent_fragment_downloads": 8,
                        "writeautosub": True,
                        "subtitleslangs": ["en", "en-US", "en-GB", "a.en"],
                        "subtitlesformat": "vtt",
                        "js_runtimes": {"node": {}},
                        "extractor_args": {
                            "youtube": {
                                "player_client": client
                            }
                        }
                    }
                    fallback_opts.update(ytdlp_cookie_opts())
                    with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        video_id = info.get("id", "video")
                        title = info.get("title", "YouTube Video")
                        channel_id = info.get("channel_id") or info.get("uploader_id", "")
                        filename = ydl.prepare_filename(info)
                        video_path = Path(filename)
                        if not video_path.exists():
                            alt_path = video_path.with_suffix(".mp4")
                            if alt_path.exists():
                                video_path = alt_path
                            else:
                                raise RuntimeError(
                                    f"Downloaded video file not found at expected path: {video_path} "
                                    f"(also checked {alt_path}). yt-dlp fallback may have failed silently."
                                )
                        download_success = True
                        break
                except Exception as client_err:
                    logger.warning(f"Fallback player client {client} failed: {client_err}")

            if not download_success:
                raise RuntimeError(
                    "All download tiers and player clients failed. "
                    "YouTube may require login — set YTDLP_COOKIES_FROM_BROWSER=chrome "
                    "(local; close Chrome if cookie access fails) or YTDLP_COOKIES_FILE=cookies.txt "
                    "(Railway/servers)."
                )

        # Probe actual resolution and log status
        from utils import get_video_dimensions
        actual_w, actual_h = get_video_dimensions(video_path)
        actual_str = f"{actual_w}x{actual_h}" if (actual_w and actual_h) else "unknown"

        if actual_h and actual_h < int(max_height * 0.9):
            info_msg = f"ℹ Source video native resolution is {actual_h}p (selected tier: {resolution_tier}). Will upscale to {tier_info['output_w']}x{tier_info['output_h']} canvas during encoding."
            logger.info(info_msg)
            if self.progress_callback:
                self.progress_callback(100.0, info_msg)
        else:
            logger.info(f"Video downloaded successfully ({resolution_tier} tier, actual source: {actual_str}): {video_path}")
            if self.progress_callback:
                self.progress_callback(100.0, f"Video download complete ({actual_str} source).")

        # NOTE: WAV audio extraction removed — transcript is fetched via YouTube API / VTT subtitles,
        # not local audio analysis. Skipping saves 5-15s and ~50MB disk per job.
        audio_path = None
        logger.info("Audio extraction skipped (transcript sourced directly from YouTube API/VTT).")
        return video_path, audio_path, title, channel_id
