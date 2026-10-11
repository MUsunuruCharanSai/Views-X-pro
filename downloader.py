import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yt_dlp

from config import BASE_DIR, DEFAULT_RESOLUTION_TIER, DOWNLOADS_DIR, RESOLUTION_TIERS, TEMP_DIR
from utils import ensure_ffmpeg_on_path, logger, resolve_ffmpeg_bin


def normalize_youtube_url(url: str) -> str:
    """Strip markdown wrappers and canonicalize to watch?v=ID."""
    raw = (url or "").strip()
    md = re.fullmatch(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", raw)
    if md:
        raw = md.group(2).strip()
    vid = re.search(r"(?:v=|/|youtu\.be/)([0-9A-Za-z_-]{11})", raw)
    if vid:
        return f"https://www.youtube.com/watch?v={vid.group(1)}"
    return raw


def _js_runtime_opts() -> Dict[str, Any]:
    """Enable Node for yt-dlp EJS challenges when available."""
    node = shutil.which("node")
    if node:
        return {"js_runtimes": {"node": {"path": node}}, "remote_components": ["ejs:github"]}
    return {"remote_components": ["ejs:github"]}


def resolve_cookie_opts() -> Dict[str, Any]:
    """Resolve cookie auth at call-time (env + auto cookies.txt)."""
    opts: Dict[str, Any] = {}
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    cookies_browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()

    candidates: List[Path] = []
    if cookies_file:
        p = Path(cookies_file)
        candidates.append(p if p.is_absolute() else BASE_DIR / p)
    candidates.append(BASE_DIR / "cookies.txt")

    for cookie_path in candidates:
        if cookie_path.is_file() and cookie_path.stat().st_size > 0:
            opts["cookiefile"] = str(cookie_path.resolve())
            logger.info(f"yt-dlp auth: cookies file → {cookie_path.name}")
            return opts

    if cookies_browser:
        parts = tuple(p for p in cookies_browser.split(":") if p)
        opts["cookiesfrombrowser"] = parts or (cookies_browser,)
        logger.info(f"yt-dlp auth: cookies from browser → {cookies_browser}")
        return opts

    return opts


def youtube_auth_status() -> str:
    opts = resolve_cookie_opts()
    if "cookiefile" in opts:
        return f"cookies file ({Path(opts['cookiefile']).name})"
    if "cookiesfrombrowser" in opts:
        return f"browser ({':'.join(opts['cookiesfrombrowser'])})"
    node = "node" if shutil.which("node") else "no-node"
    return f"default clients + ejs ({node})"


def build_ydl_base_opts(
    *,
    format_str: Optional[str] = None,
    out_template: Optional[str] = None,
    sub_template: Optional[str] = None,
    player_clients: Optional[List[str]] = None,
    with_cookies: bool = False,
    progress_hooks: Optional[List[Callable]] = None,
    skip_download: bool = False,
    max_duration_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """Build yt-dlp options.

    Important: do NOT force android/ios/web/tv clients by default — those currently
    trigger YouTube bot challenges. Prefer yt-dlp default clients (visionos/m3u8).
    Never pair cookies with the tv client.
    """
    ensure_ffmpeg_on_path()
    ffmpeg_bin = resolve_ffmpeg_bin()

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        **_js_runtime_opts(),
    }
    # Critical: without this, bestvideo+bestaudio merge fails and looks like a YouTube bot error
    if ffmpeg_bin:
        opts["ffmpeg_location"] = str(Path(ffmpeg_bin).parent)

    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": player_clients}}

    if skip_download:
        opts["skip_download"] = True
        opts["ignore_no_formats_error"] = True
    else:
        opts.update(
            {
                "format": format_str,
                "outtmpl": {"default": out_template, "subtitle": sub_template},
                "merge_output_format": "mp4",
                "concurrent_fragment_downloads": 4,
                "http_chunk_size": 10485760,
                "buffersize": 2097152,
                "writeautosub": True,
                "subtitleslangs": ["en", "en-US", "en-GB", "a.en"],
                "subtitlesformat": "vtt",
            }
        )
        if progress_hooks:
            opts["progress_hooks"] = progress_hooks

    if with_cookies and (not player_clients or "tv" not in player_clients):
        cookie_opts = resolve_cookie_opts()
        if cookie_opts:
            opts.update(cookie_opts)

    if max_duration_sec and max_duration_sec > 0 and not skip_download:
        from yt_dlp.utils import download_range_func

        opts["download_ranges"] = download_range_func(None, [(0, max_duration_sec)])
        opts["force_keyframes_at_cuts"] = True

    return opts


def extract_youtube_info(url: str) -> Dict[str, Any]:
    """Shared metadata extraction with the same strategy as downloads."""
    url = normalize_youtube_url(url)
    cookie_opts = resolve_cookie_opts()
    attempts: List[Tuple[str, Optional[List[str]], bool]] = [
        ("default", None, False),
        ("android_vr", ["android_vr"], False),
        ("default+web_embedded+cookies", ["default", "web_embedded"], True),
    ]
    last_err: Optional[Exception] = None
    for label, clients, use_cookies in attempts:
        if use_cookies and not cookie_opts:
            continue
        try:
            opts = build_ydl_base_opts(
                player_clients=clients,
                with_cookies=use_cookies,
                skip_download=True,
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                logger.info(f"Metadata OK via {label}")
                return info
        except Exception as err:
            last_err = err
            logger.warning(f"Metadata {label} failed: {err}")
            time.sleep(1.0)
    raise RuntimeError(str(last_err) if last_err else "YouTube metadata extraction failed")


def ytdlp_cookie_opts() -> Dict[str, Any]:
    return resolve_cookie_opts()


class YouTubeDownloader:
    """Downloads YouTube video for the clipping pipeline."""

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
        max_duration_sec: Optional[float] = None,
    ) -> Tuple[Path, Optional[Path], str, str]:
        url = normalize_youtube_url(url)
        tier_info = RESOLUTION_TIERS.get(resolution_tier, RESOLUTION_TIERS[DEFAULT_RESOLUTION_TIER])
        max_height = tier_info["height"]

        ffmpeg_bin = ensure_ffmpeg_on_path()
        if not ffmpeg_bin:
            raise RuntimeError(
                "FFmpeg not found. Install FFmpeg and ensure it is on PATH, "
                "or set FFMPEG_PATH to ffmpeg.exe. yt-dlp cannot merge video+audio without it."
            )
        logger.info(f"FFmpeg for yt-dlp: {ffmpeg_bin}")

        dur_label = f" (first {int(max_duration_sec)}s)" if max_duration_sec else ""
        logger.info(f"Initiating yt-dlp download for ({resolution_tier} max {max_height}p{dur_label}): {url}")
        logger.info(f"YouTube auth mode: {youtube_auth_status()}")
        if self.progress_callback:
            self.progress_callback(5.0, f"Fetching video metadata ({resolution_tier}{dur_label})...")

        vid_match = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url)
        video_id = vid_match.group(1) if vid_match else None
        if video_id and not max_duration_sec:
            # Ignore yt-dlp temp fragments (*.f616.mp4, *.part, etc.) from failed merges
            candidates = list(DOWNLOADS_DIR.glob(f"{video_id}_*.mp4")) + list(DOWNLOADS_DIR.glob(f"{video_id}.mp4"))
            for cand in candidates:
                name = cand.name
                if re.search(r"\.f\d+\.", name) or name.endswith(".part") or ".ytdl" in name:
                    continue
                if cand.is_file() and cand.stat().st_size > 10 * 1024 * 1024:
                    logger.info(f"Using existing downloaded video: {cand.name} ({cand.stat().st_size / (1024*1024):.1f} MB)")
                    if self.progress_callback:
                        self.progress_callback(100.0, f"Using existing video: {cand.name}")
                    try:
                        info = extract_youtube_info(url)
                        title = info.get("title", cand.stem)
                        channel_id = info.get("channel_id") or info.get("uploader_id", "")
                    except Exception:
                        title = cand.stem.split("_", 1)[1] if "_" in cand.stem else cand.stem
                        channel_id = ""
                    return cand, None, title, channel_id

        out_template = str(DOWNLOADS_DIR / "%(id)s_%(title).50s.%(ext)s")
        sub_template = str(TEMP_DIR / "%(id)s.%(ext)s")
        # Prefer HLS (m3u8) for BOTH video+audio — https audio streams often 403 without PO tokens
        hls_merge_format = (
            f"bestvideo[height<={max_height}][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
            f"bestvideo*[height<={max_height}][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
            f"best[height<={max_height}][protocol^=m3u8]/"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]/best"
        )
        progressive_format = (
            f"best[height<={max_height}][ext=mp4]/"
            f"best[height<={max_height}]/"
            f"best[ext=mp4]/best"
        )

        cookie_opts = resolve_cookie_opts()
        # (label, clients, want_cookies, format_str)
        attempts: List[Tuple[str, Optional[List[str]], bool, str]] = [
            ("default+hls", None, False, hls_merge_format),
            ("default+progressive", None, False, progressive_format),
            ("android_vr+progressive", ["android_vr"], False, progressive_format),
            ("default+web_embedded+cookies", ["default", "web_embedded"], True, hls_merge_format),
            ("mweb+cookies", ["mweb"], True, hls_merge_format),
        ]

        errors: List[str] = []
        video_path: Optional[Path] = None
        title = "YouTube Video"
        channel_id = ""

        for label, clients, want_cookies, format_str in attempts:
            if want_cookies and not cookie_opts:
                continue
            try:
                logger.info(f"yt-dlp attempt: {label}")
                opts = build_ydl_base_opts(
                    format_str=format_str,
                    out_template=out_template,
                    sub_template=sub_template,
                    player_clients=clients,
                    with_cookies=want_cookies,
                    progress_hooks=[self._ydl_hook],
                    max_duration_sec=max_duration_sec,
                )
                # Prefer HLS over https progressive (avoids GVS PO-token 403 on audio)
                opts["format_sort"] = ["res", "proto:m3u8"]
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "YouTube Video")
                    channel_id = info.get("channel_id") or info.get("uploader_id", "")
                    filename = ydl.prepare_filename(info)
                    video_path = Path(filename)
                    if not video_path.exists():
                        # Merged output is often forced to .mp4
                        for alt in (
                            video_path.with_suffix(".mp4"),
                            video_path.with_suffix(".webm"),
                            video_path.with_suffix(".mkv"),
                        ):
                            if alt.exists():
                                video_path = alt
                                break
                        else:
                            raise RuntimeError(
                                f"Downloaded video file not found at expected path: {video_path}"
                            )
                logger.info(f"yt-dlp succeeded with {label}")
                break
            except Exception as err:
                errors.append(f"{label}: {err}")
                logger.warning(f"yt-dlp attempt {label} failed: {err}")
                time.sleep(1.0)
        else:
            auth = youtube_auth_status()
            detail = " | ".join(errors[-3:]) if errors else "unknown"
            raise RuntimeError(
                f"All YouTube download strategies failed (auth={auth}). "
                f"Recent errors: {detail}. "
                "Ensure FFmpeg is installed. On Railway/datacenter IPs, add cookies.txt "
                "(throwaway Google account) via YTDLP_COOKIES_FILE."
            )

        from utils import get_video_dimensions

        actual_w, actual_h = get_video_dimensions(video_path)
        actual_str = f"{actual_w}x{actual_h}" if (actual_w and actual_h) else "unknown"

        if actual_h and actual_h < int(max_height * 0.9):
            info_msg = (
                f"ℹ Source video native resolution is {actual_h}p (selected tier: {resolution_tier}). "
                f"Will upscale to {tier_info['output_w']}x{tier_info['output_h']} canvas during encoding."
            )
            logger.info(info_msg)
            if self.progress_callback:
                self.progress_callback(100.0, info_msg)
        else:
            logger.info(f"Video downloaded successfully ({resolution_tier} tier, actual source: {actual_str}): {video_path}")
            if self.progress_callback:
                self.progress_callback(100.0, f"Video download complete ({actual_str} source).")

        logger.info("Audio extraction skipped (transcript sourced directly from YouTube API/VTT).")
        return video_path, None, title, channel_id
