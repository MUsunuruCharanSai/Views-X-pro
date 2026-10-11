import base64
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yt_dlp

from config import BASE_DIR, DEFAULT_RESOLUTION_TIER, DOWNLOADS_DIR, RESOLUTION_TIERS, TEMP_DIR
from utils import ensure_ffmpeg_on_path, logger, resolve_ffmpeg_bin

# Materialized cookies path for Railway env injection
_RUNTIME_COOKIES_PATH = TEMP_DIR / "youtube_cookies.txt"


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


def is_railway_host() -> bool:
    """True when running on Railway (not a synonym for 'cookies required')."""
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))


def cookies_recommended() -> bool:
    """Warn/prefer cookies on datacenter hosts; never used as a hard pre-flight block."""
    flag = os.environ.get("YTDLP_REQUIRE_COOKIES", "").strip().lower() in ("1", "true", "yes")
    return is_railway_host() or flag


def cookies_setup_message() -> str:
    return (
        "YouTube is blocking this server IP (bot check). Add cookies from a throwaway Google account: "
        "export Netscape cookies.txt, then set Railway variable YTDLP_COOKIES_BASE64 "
        "(PowerShell: [Convert]::ToBase64String([IO.File]::ReadAllBytes('cookies.txt'))), "
        "or POST the file body to /api/youtube-cookies. Do not use your main Google account."
    )


def _is_bot_block_error(err: Any) -> bool:
    msg = str(err).lower()
    return (
        "sign in to confirm" in msg
        or "not a bot" in msg
        or "cookies-from-browser" in msg
        or "login required" in msg
    )


def _looks_like_netscape_cookies(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "# Netscape HTTP Cookie File" in t or "# HTTP Cookie File" in t:
        return True
    # Tab-separated cookie lines are also accepted by yt-dlp
    return any("\t" in line and "youtube" in line.lower() for line in t.splitlines() if line and not line.startswith("#"))


# Cookies YouTube typically needs for a logged-in session on datacenter IPs
_YT_LOGIN_COOKIE_NAMES = (
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "SID",
    "__Secure-1PSIDTS",
    "LOGIN_INFO",
)


def inspect_youtube_cookies(text: str) -> Dict[str, Any]:
    """Check Netscape cookie body for YouTube/Google login markers."""
    names: set = set()
    domains: set = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name = parts[0].lower(), parts[5]
        names.add(name)
        domains.add(domain.lstrip("."))
    has_yt = any("youtube.com" in d for d in domains)
    has_google = any(d == "google.com" or d.endswith(".google.com") for d in domains)
    login_hits = [n for n in _YT_LOGIN_COOKIE_NAMES if n in names]
    ok = has_yt and len(login_hits) >= 2
    return {
        "ok": ok,
        "has_youtube_domain": has_yt,
        "has_google_domain": has_google,
        "login_cookies": login_hits,
        "cookie_count": len(names),
    }


def cookies_expired_or_weak_message(inspection: Optional[Dict[str, Any]] = None) -> str:
    detail = ""
    if inspection and not inspection.get("ok"):
        detail = (
            f" Cookie file looks weak/incomplete (youtube_domain={inspection.get('has_youtube_domain')}, "
            f"login_markers={inspection.get('login_cookies') or []})."
        )
    return (
        "YouTube still bot-checks this server even with cookies.txt — the session is likely "
        "expired, incomplete, or from the wrong account."
        f"{detail} "
        "Re-export fresh Netscape cookies while signed into https://www.youtube.com "
        "with a throwaway Google account (extension: 'Get cookies.txt LOCALLY'), "
        "then paste again in Settings or set YTDLP_COOKIES_BASE64. "
        "Do not use your main Google account."
    )


def materialize_cookies_from_env() -> Optional[Path]:
    """Write cookies from Railway env vars to a local Netscape file.

    Supports:
      - YTDLP_COOKIES_BASE64  (base64 of cookies.txt)
      - YTDLP_COOKIES_CONTENT (raw Netscape cookies.txt body)
    """
    b64 = os.environ.get("YTDLP_COOKIES_BASE64", "").strip()
    raw = os.environ.get("YTDLP_COOKIES_CONTENT", "").strip()
    if not b64 and not raw:
        return None

    content = ""
    if b64:
        try:
            # Tolerate whitespace/newlines pasted into Railway UI
            compact = re.sub(r"\s+", "", b64)
            content = base64.b64decode(compact).decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(
                f"YTDLP_COOKIES_BASE64 is set but could not be decoded ({e}). "
                "Re-export cookies.txt and regenerate the base64 value."
            )
            return None
    else:
        # Allow \n escaped newlines from some secret UIs
        content = raw.replace("\\n", "\n")

    if not content.strip():
        logger.error("YTDLP_COOKIES_BASE64/CONTENT is set but decoded content is empty.")
        return None
    if not _looks_like_netscape_cookies(content):
        logger.error(
            "YTDLP_COOKIES_BASE64/CONTENT is set but is not a valid Netscape cookies.txt "
            "(expected '# Netscape HTTP Cookie File' or youtube.com tab-separated lines)."
        )
        return None

    try:
        _RUNTIME_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RUNTIME_COOKIES_PATH.write_text(content, encoding="utf-8")
        logger.info(f"yt-dlp auth: materialized cookies from env → {_RUNTIME_COOKIES_PATH.name}")
        return _RUNTIME_COOKIES_PATH
    except OSError as e:
        logger.error(f"Could not write runtime cookies file: {e}")
        return None


def save_cookies_text(content: str) -> Path:
    """Persist uploaded Netscape cookies for local/Railway volume use."""
    content = content.replace("\\n", "\n")
    if not _looks_like_netscape_cookies(content):
        raise ValueError(
            "Invalid cookies format. Export a Netscape cookies.txt "
            "(e.g. 'Get cookies.txt LOCALLY' browser extension) while signed into YouTube."
        )
    inspection = inspect_youtube_cookies(content)
    if not inspection["ok"]:
        raise ValueError(
            "Cookies file is missing YouTube login markers "
            f"(found login cookies: {inspection['login_cookies'] or 'none'}; "
            f"youtube.com domain: {inspection['has_youtube_domain']}). "
            "Export again from youtube.com while logged in — include .youtube.com and .google.com cookies."
        )
    dest = BASE_DIR / "cookies.txt"
    dest.write_text(content, encoding="utf-8")
    # Keep runtime copy in sync
    _RUNTIME_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RUNTIME_COOKIES_PATH.write_text(dest.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info(
        f"yt-dlp auth: saved cookies.txt ({inspection['cookie_count']} names, "
        f"login={inspection['login_cookies']})"
    )
    return dest


def resolve_cookie_opts() -> Dict[str, Any]:
    """Resolve cookie auth at call-time (file / env / browser)."""
    opts: Dict[str, Any] = {}
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    cookies_browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()

    candidates: List[Path] = []
    if cookies_file:
        p = Path(cookies_file)
        candidates.append(p if p.is_absolute() else BASE_DIR / p)
    candidates.append(BASE_DIR / "cookies.txt")
    candidates.append(_RUNTIME_COOKIES_PATH)

    for cookie_path in candidates:
        if cookie_path.is_file() and cookie_path.stat().st_size > 0:
            opts["cookiefile"] = str(cookie_path.resolve())
            logger.info(f"yt-dlp auth: cookies file → {cookie_path.name}")
            return opts

    # Railway-friendly: inject from env secret if no file present
    materialized = materialize_cookies_from_env()
    if materialized and materialized.is_file():
        opts["cookiefile"] = str(materialized.resolve())
        return opts

    # Browser cookies only work locally (not on Railway containers)
    if cookies_browser and not is_railway_host():
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
    if cookies_recommended():
        return f"anonymous+ejs ({node}); cookies recommended on this host"
    return f"default+ejs ({node})"


def cookies_configured() -> bool:
    return bool(resolve_cookie_opts())


def fetch_youtube_oembed(url: str) -> Dict[str, Any]:
    """Cookie-free preview metadata via YouTube oEmbed (title/thumb/uploader only)."""
    import requests

    url = normalize_youtube_url(url)
    vid_match = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url)
    video_id = vid_match.group(1) if vid_match else ""
    resp = requests.get(
        "https://www.youtube.com/oembed",
        params={"url": url, "format": "json"},
        timeout=15,
        headers={"User-Agent": "ViewsXPro/1.0"},
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "id": video_id,
        "title": data.get("title") or "YouTube Video",
        "thumbnail": data.get("thumbnail_url")
        or (f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""),
        "uploader": data.get("author_name") or "YouTube Creator",
        "channel": data.get("author_name") or "YouTube Creator",
        "duration": 0,
        "_source": "oembed",
    }


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
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en", "en-US", "en-GB", "a.en", "hi", "hi-IN"],
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


def extract_youtube_info(url: str, *, allow_oembed_fallback: bool = True) -> Dict[str, Any]:
    """Shared metadata extraction. Never hard-fails before trying yt-dlp / oEmbed."""
    url = normalize_youtube_url(url)
    cookie_opts = resolve_cookie_opts()

    # NOTE: do not pass player_client="default" — that is not a valid yt-dlp client id
    attempts: List[Tuple[str, Optional[List[str]], bool]] = [
        ("auto", None, False),
        ("android_vr", ["android_vr"], False),
    ]
    if cookie_opts:
        attempts = [
            ("cookies+auto", None, True),
            ("cookies+web", ["web"], True),
            ("cookies+mweb", ["mweb"], True),
            ("cookies+web_safari", ["web_safari"], True),
        ]

    last_err: Optional[Exception] = None
    saw_bot_block = False
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
            if _is_bot_block_error(err):
                saw_bot_block = True
            logger.warning(f"Metadata {label} failed: {err}")
            time.sleep(0.5)

    if allow_oembed_fallback:
        try:
            info = fetch_youtube_oembed(url)
            logger.info("Metadata OK via youtube oEmbed fallback (preview-only; duration may be 0)")
            return info
        except Exception as oembed_err:
            logger.warning(f"oEmbed fallback failed: {oembed_err}")

    if saw_bot_block and not cookie_opts:
        raise RuntimeError(cookies_setup_message())
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
        cookie_inspection: Optional[Dict[str, Any]] = None
        if cookie_opts.get("cookiefile"):
            try:
                cookie_inspection = inspect_youtube_cookies(
                    Path(cookie_opts["cookiefile"]).read_text(encoding="utf-8", errors="replace")
                )
                logger.info(
                    f"Cookie inspection: ok={cookie_inspection['ok']} "
                    f"login={cookie_inspection['login_cookies']} "
                    f"count={cookie_inspection['cookie_count']}"
                )
            except OSError as e:
                logger.warning(f"Could not inspect cookie file: {e}")

        if cookies_recommended() and not cookie_opts:
            logger.warning(
                "No YouTube cookies configured on a datacenter-like host. "
                "Will try anonymous download first; bot-check may still require YTDLP_COOKIES_BASE64."
            )

        # Valid yt-dlp clients only (never pass player_client="default").
        # When cookies exist on Railway, skip anonymous retries — they always bot-fail and hide real errors.
        attempts: List[Tuple[str, Optional[List[str]], bool, str]] = []
        if cookie_opts:
            attempts.extend(
                [
                    ("cookies+auto+hls", None, True, hls_merge_format),
                    ("cookies+web+hls", ["web"], True, hls_merge_format),
                    ("cookies+mweb+hls", ["mweb"], True, hls_merge_format),
                    ("cookies+web_safari+hls", ["web_safari"], True, hls_merge_format),
                    ("cookies+web+progressive", ["web"], True, progressive_format),
                    ("cookies+mweb+progressive", ["mweb"], True, progressive_format),
                ]
            )
        else:
            attempts.extend(
                [
                    ("auto+hls", None, False, hls_merge_format),
                    ("auto+progressive", None, False, progressive_format),
                    ("android_vr+progressive", ["android_vr"], False, progressive_format),
                ]
            )

        errors: List[str] = []
        saw_bot_block = False
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
                # Browser-like UA helps some cookie sessions on datacenter IPs
                if want_cookies:
                    opts["http_headers"] = {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                # Fewer parallel fragments on Railway to reduce bot-score / RAM spikes
                if is_railway_host() or cookies_recommended():
                    opts["concurrent_fragment_downloads"] = 2
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
                if _is_bot_block_error(err):
                    saw_bot_block = True
                logger.warning(f"yt-dlp attempt {label} failed: {err}")
                time.sleep(1.0)
        else:
            auth = youtube_auth_status()
            # Prefer cookie-attempt errors in the summary (not anonymous noise)
            cookie_errs = [e for e in errors if e.startswith("cookies+")]
            detail = " | ".join((cookie_errs or errors)[:3]) if errors else "unknown"
            if saw_bot_block and not cookie_opts:
                raise RuntimeError(f"{cookies_setup_message()} (auth={auth})")
            if saw_bot_block and cookie_opts:
                raise RuntimeError(
                    f"{cookies_expired_or_weak_message(cookie_inspection)} "
                    f"(auth={auth}). Attempts: {detail}."
                )
            raise RuntimeError(
                f"All YouTube download strategies failed (auth={auth}). Recent errors: {detail}."
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
