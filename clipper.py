"""Clipper module using FFmpeg with Intel Quick Sync (h264_qsv), CPU fallback, selectable resolution tiers (1080p, 1440p, 4K), noise-free pre-upscale denoise, Lanczos+mild unsharp detail sharpening, smart face-centered speaker crop alignment, and resolution-relative ASS subtitles."""

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import (
    BASE_DIR,
    CLIPS_DIR,
    DEFAULT_CRF,
    DEFAULT_QSV_QUALITY,
    DEFAULT_RESOLUTION_TIER,
    DENOISE_FILTER,
    RESOLUTION_TIERS,
    TEMP_DIR,
    get_like_path,
    get_logo_path,
    get_subscribe_path,
)
from facecam_detector import FacecamDetector
from utils import logger


def format_ass_time(seconds: float) -> str:
    """Format float seconds into ASS timestamp string h:mm:ss.cs (e.g. 0:01:23.45)."""
    if seconds < 0:
        seconds = 0.0
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"


def build_crop_x_expression(keyframes: List[Tuple[float, int]]) -> str:
    """Turn a list of (relative_time, crop_x) keyframes into a piecewise-linear
    FFmpeg expression string usable as the crop filter's `x` param with
    eval=frame. Uses the `t` variable, which FFmpeg provides per-frame.
    """
    if len(keyframes) == 1:
        return str(keyframes[0][1])

    # Build nested if() expressions from the END backwards so the final
    # expression short-circuits correctly: if(lt(t,T1), interp01, if(lt(t,T2), interp12, ... lastX))
    expr = str(keyframes[-1][1])
    for i in range(len(keyframes) - 2, -1, -1):
        t0, x0 = keyframes[i]
        t1, x1 = keyframes[i + 1]
        if t1 == t0:
            segment = str(x0)
        else:
            # Linear interpolation between (t0,x0) and (t1,x1)
            segment = f"({x0}+({x1}-{x0})*(t-{t0})/({t1}-{t0}))"
        expr = f"if(lt(t,{t1}),{segment},{expr})"
    return expr


def _escape_ass_path(ass_path: Path) -> str:
    """Format ASS path for FFmpeg filtergraph: relative path or absolute with escaped Windows drive colon."""
    try:
        return str(ass_path.relative_to(BASE_DIR)).replace("\\", "/")
    except Exception:
        return str(ass_path.resolve()).replace("\\", "/").replace(":", r"\:")


def _clean_caption_text(text: str) -> str:
    """Strip YouTube auto-caption noise from transcript text before display.

    Removes:
    - Speaker-change markers (>> or >) at start of line or after whitespace
    - Bracketed sound/music tags like [Music], [Applause], [Laughter], etc.
    - Parenthesised noise tags like (music), (laughing)
    - Excessive whitespace
    """
    # Remove bracketed tags: [Music], [Applause], [Laughter], etc.
    text = re.sub(r'\[[^\]]{1,30}\]', '', text)
    # Remove parenthesised tags: (music), (laughing), etc.
    text = re.sub(r'\([^)]{1,30}\)', '', text)
    # Remove speaker-change markers >> or > at start or after whitespace
    text = re.sub(r'(?:^|\s)>>?\s*', ' ', text)
    # Collapse multiple spaces and strip leading/trailing whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def generate_ass_subtitles(
    clip: Dict[str, Any],
    transcript: List[Dict[str, Any]],
    out_ass_path: Path,
    output_w: int = 1080,
    output_h: int = 1920,
    bottom_bar_height: Optional[int] = None
) -> Path:
    """Generate resolution-relative Advanced SubStation Alpha (.ass) subtitle file with
    YouTube Shorts-optimised styling: bold hook overlay, thick outline captions, no overlaps."""
    start_t = float(clip["start_time"])
    end_t = float(clip["end_time"])
    duration = end_t - start_t
    hook_text = str(clip.get("hook_text", "WATCH THIS")).strip().upper()
    # Clamp hook_text to 60 chars max to avoid overflow
    if len(hook_text) > 60:
        hook_text = hook_text[:57] + "..."

    # ── YouTube Shorts-tuned sizing ────────────────────────────────
    # Hook: large, centered at top — eye-catching within first 3s
    # Caption: bold white + thick black outline — readable on all backgrounds
    hook_fontsize    = max(52, round(output_h * 0.032))   # ~60px at 1920h
    caption_fontsize = max(38, round(output_h * 0.024))   # ~46px at 1920h
    hook_margin_v    = max(90, round(output_h * 0.07))    # 7% from top
    margin_l_r       = max(30, round(output_w * 0.04))    # 4% side margins

    if bottom_bar_height and bottom_bar_height > 60:
        # Letterboxed clip: anchor right below the gameplay frame (top of bottom black bar)
        # MarginV is distance from bottom edge of canvas up to caption.
        caption_margin_v = max(20, bottom_bar_height - max(40, round(caption_fontsize * 1.6)))
    else:
        caption_margin_v = max(80, round(output_h * 0.075))   # Mode C default, unchanged

    # ASS colour format: &HAABBGGRR  (alpha, blue, green, red)
    # Cyan: &H0000FFFF   White: &H00FFFFFF   Black: &H00000000   ShadowBlack: &H99000000
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {output_w}
PlayResY: {output_h}
ScaledBorderAndShadow: yes
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: HookStyle,Arial Black,{hook_fontsize},&H0000FFFF,&H00000000,&H00000000,&H99000000,-1,0,0,0,100,100,1,0,1,5,3,8,{margin_l_r},{margin_l_r},{hook_margin_v},1
Style: CaptionStyle,Arial Black,{caption_fontsize},&H00FFFFFF,&H00000000,&H00000000,&H99000000,-1,0,0,0,100,100,0.5,0,1,4,2,2,{margin_l_r},{margin_l_r},{caption_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []

    # 1. Prominent Top Hook Text Overlay for first 2.5 seconds (top-aligned \an8)
    hook_duration = min(2.5, duration)
    events.append(
        f"Dialogue: 0,{format_ass_time(0.0)},{format_ass_time(hook_duration)},HookStyle,,0,0,0,,{hook_text}"
    )

    # 2. Collect & Filter Transcript Snippets within Clip Window
    raw_snippets = []
    if transcript:
        for item in transcript:
            item_start = float(item["start"])
            item_dur = float(item.get("duration", 2.0))
            item_end = item_start + item_dur

            if item_end > start_t and item_start < end_t:
                rel_start = max(0.0, item_start - start_t)
                rel_end = min(duration, item_end - start_t)
                text = str(item.get("text", "")).strip()

                if text and (rel_end - rel_start > 0.1):
                    raw_snippets.append({
                        "rel_start": round(rel_start, 2),
                        "rel_end": round(rel_end, 2),
                        "text": _clean_caption_text(text)
                    })

    # Sort snippets by relative start time
    raw_snippets.sort(key=lambda x: x["rel_start"])

    # Deduplicate consecutive identical text snippets
    deduped_snippets = []
    for snip in raw_snippets:
        if deduped_snippets and deduped_snippets[-1]["text"] == snip["text"]:
            deduped_snippets[-1]["rel_end"] = max(deduped_snippets[-1]["rel_end"], snip["rel_end"])
        else:
            deduped_snippets.append(snip)

    # Clamp end times to guarantee strictly sequential, non-overlapping caption display windows
    for i in range(len(deduped_snippets)):
        curr = deduped_snippets[i]
        if i + 1 < len(deduped_snippets):
            next_start = deduped_snippets[i + 1]["rel_start"]
            if curr["rel_end"] > next_start:
                curr["rel_end"] = max(curr["rel_start"] + 0.1, round(next_start - 0.05, 2))

    # Programmatically verify zero timing overlaps between consecutive captions
    for i in range(len(deduped_snippets) - 1):
        c1 = deduped_snippets[i]
        c2 = deduped_snippets[i + 1]
        if c1["rel_end"] > c2["rel_start"]:
            c1["rel_end"] = max(c1["rel_start"] + 0.1, round(c2["rel_start"] - 0.05, 2))

    # Format & Append Subtitle Dialogue Lines
    for snip in deduped_snippets:
        rel_start = snip["rel_start"]
        rel_end = snip["rel_end"]
        raw_text = snip["text"]

        # Highlight emphasized words (ALL-CAPS or words containing !)
        words = raw_text.split()
        styled_words = []
        for w in words:
            if w.isupper() and len(w) > 1:
                styled_words.append(f"{{\\b1\\c&H0000FFFF&}}{w}{{\\r}}")
            elif "!" in w:
                styled_words.append(f"{{\\b1\\c&H0000FFFF&}}{w}{{\\r}}")
            else:
                styled_words.append(w)

        formatted_text = " ".join(styled_words)
        events.append(
            f"Dialogue: 0,{format_ass_time(rel_start)},{format_ass_time(rel_end)},CaptionStyle,,0,0,0,,{formatted_text}"
        )

    ass_content = header + "\n".join(events) + "\n"
    out_ass_path.write_text(ass_content, encoding="utf-8")
    return out_ass_path


def _find_peak_moment(
    clip: Dict[str, Any],
    transcript: List[Dict[str, Any]],
    clip_duration: float,
    ai_peak_offset: Optional[float] = None,
) -> float:
    """Find the best peak moment offset (seconds from clip start) to use as the 2s flash-forward preview.

    Priority order:
    1. AI-provided peak_moment_offset (most accurate — LLM picked the climax)
    2. Transcript heuristic — highest reaction-word density in last 40% of clip
    3. Fallback — 70% through the clip

    Returns a float: seconds from clip_start where the peak starts.
    The returned value is clamped so the 2s flash window fits inside the clip.
    """
    start_t = float(clip["start_time"])
    end_t = float(clip["end_time"])
    clip_dur = end_t - start_t

    FLASH_DURATION = 2.0
    # Ensure peak window fits: must be at least 5s in, and at most clip_dur-3s
    min_peak = max(5.0, clip_dur * 0.35)
    max_peak = max(min_peak + 1.0, clip_dur - FLASH_DURATION - 1.0)

    # 1. Use AI-provided offset if valid
    if ai_peak_offset is not None:
        clamped = max(min_peak, min(float(ai_peak_offset), max_peak))
        logger.info(f"[FlashForward] Using AI peak_moment_offset={ai_peak_offset:.1f}s → clamped to {clamped:.1f}s")
        return clamped

    # 2. Transcript heuristic — scan last 40% of clip for reaction word clusters
    from analyzer import STREAMING_REACTION_WORDS  # local import to avoid circular
    search_from = start_t + (clip_dur * 0.60)
    best_offset = clip_dur * 0.70  # fallback: 70% through
    best_score = -1

    if transcript:
        for item in transcript:
            item_start = float(item.get("start", 0))
            if item_start < search_from or item_start >= end_t - FLASH_DURATION:
                continue
            text_lower = str(item.get("text", "")).lower()
            score = sum(1 for w in STREAMING_REACTION_WORDS if w in text_lower)
            # Boost score for exclamation marks and ALL-CAPS words
            score += text_lower.count("!")
            score += sum(1 for w in text_lower.split() if w.isupper() and len(w) > 1)
            if score > best_score:
                best_score = score
                best_offset = item_start - start_t
        if best_score > 0:
            logger.info(f"[FlashForward] Heuristic peak at offset={best_offset:.1f}s (reaction score={best_score})")
        else:
            logger.info(f"[FlashForward] No reaction words found — defaulting to 70% offset={best_offset:.1f}s")

    return max(min_peak, min(best_offset, max_peak))


def generate_flash_ass_subtitles(
    clip: Dict[str, Any],
    transcript: List[Dict[str, Any]],
    peak_offset: float,
    flash_duration: float,
    out_ass_path: Path,
    output_w: int = 1080,
    output_h: int = 1920,
) -> Path:
    """Generate ASS subtitles for the 2-second flash-forward preview segment.

    Shows:
    - A small ⚡ FLASH FORWARD badge in the top-right corner (whole duration)
    - The transcript text at the peak moment (bottom caption)
    """
    start_t = float(clip["start_time"])
    peak_abs = start_t + peak_offset   # absolute video timestamp of peak
    peak_end_abs = peak_abs + flash_duration

    # Sizing — same resolution-relative approach as main subs
    badge_fontsize = max(26, round(output_h * 0.016))    # small badge
    caption_fontsize = max(38, round(output_h * 0.024))   # same as main captions
    margin_l_r = max(30, round(output_w * 0.04))
    badge_margin_v = max(60, round(output_h * 0.045))
    caption_margin_v = max(80, round(output_h * 0.075))

    # ASS colour: Orange &H0050AFFF (ABGR), White &H00FFFFFF, Black outline &H00000000
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {output_w}
PlayResY: {output_h}
ScaledBorderAndShadow: yes
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: FlashBadge,Arial Black,{badge_fontsize},&H0050AFFF,&H00000000,&H00000000,&HAA000000,-1,0,0,0,100,100,1,0,1,3,2,9,{margin_l_r},{margin_l_r},{badge_margin_v},1
Style: FlashCaption,Arial Black,{caption_fontsize},&H00FFFFFF,&H00000000,&H00000000,&H99000000,-1,0,0,0,100,100,0.5,0,1,4,2,2,{margin_l_r},{margin_l_r},{caption_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []

    # Badge: top-right corner (\\an9 = top-right alignment) for whole flash window
    events.append(
        f"Dialogue: 0,{format_ass_time(0.0)},{format_ass_time(flash_duration)},"
        f"FlashBadge,,0,0,0,,{{\\an9}}⚡ FLASH FORWARD"
    )

    # Caption: transcript text at the peak moment
    peak_snippets = []
    if transcript:
        for item in transcript:
            item_start = float(item.get("start", 0))
            item_dur = float(item.get("duration", 2.0))
            item_end = item_start + item_dur
            # Show any transcript that overlaps the peak window
            if item_end > peak_abs and item_start < peak_end_abs:
                text = str(item.get("text", "")).strip()
                text = _clean_caption_text(text)
                if text:
                    peak_snippets.append(text)

    if peak_snippets:
        peak_text = " ".join(peak_snippets[:3])  # max 3 entries to avoid overflow
        if len(peak_text) > 80:
            peak_text = peak_text[:77] + "..."
        events.append(
            f"Dialogue: 0,{format_ass_time(0.0)},{format_ass_time(flash_duration)},"
            f"FlashCaption,,0,0,0,,{peak_text.upper()}"
        )

    ass_content = header + "\n".join(events) + "\n"
    out_ass_path.write_text(ass_content, encoding="utf-8")
    return out_ass_path


class FFmpegClipper:
    """Cuts refined video segments into standalone vertical MP4 clips at target resolution tiers with quality tuning & denoise."""

    def __init__(self, progress_callback: Optional[Callable[[int, int, str], None]] = None):
        self.progress_callback = progress_callback
        self.detector = FacecamDetector()
        # Bug 1 & 4 fix: Cache QSV operational status per instance/job
        self._qsv_tested = False
        self._qsv_available = False


    def _check_qsv_support(self) -> bool:
        """Bug 4 fix: Fast 1-second smoke test to verify if Intel QSV hardware encoder is operational."""
        try:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=1",
                "-c:v", "h264_qsv", "-f", "null", "-"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            return res.returncode == 0
        except Exception as e:
            logger.debug(f"QSV probe exception: {e}")
            return False

    def _get_video_dimensions(self, video_path: Path) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        """Probe actual width, height, and level of encoded clip file via ffprobe."""
        try:
            cmd = [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,level",
                "-of", "csv=p=0",
                str(video_path)
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=15)
            output = res.stdout.strip()
            parts = output.split(",")
            if len(parts) >= 2:
                w = int(parts[0])
                h = int(parts[1])
                lvl = parts[2] if len(parts) >= 3 else "unknown"
                return w, h, lvl
        except Exception as e:
            logger.warning(f"Could not probe video dimensions for {video_path.name}: {e}")
        return None, None, None

    def cut_clips(
        self,
        video_path: Path,
        refined_clips: List[Dict[str, Any]],
        transcript: Optional[List[Dict[str, Any]]] = None,
        job_id: str = "default",
        facecam_region: Optional[Dict[str, float]] = None,
        facecam_confirmed: bool = False,
        resolution_tier: str = DEFAULT_RESOLUTION_TIER,
        content_type: str = "general",
        flash_forward_hook: bool = True,
        selected_logo: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[Path]:
        """Cut video segments into vertical MP4 files for specified resolution tier with noise-free Lanczos+mild unsharp sharpness & smart face-centered speaker crop.

        Flash-Forward Hook (when flash_forward_hook=True):
          Each clip starts with a silent 2-second preview of the peak/climax moment
          (orange ⚡ FLASH FORWARD badge + peak transcript text), followed by a 0.3s
          black divider, then the full clip plays from the beginning with normal audio
          and subtitles. This stops the scroll by showing the payoff first.
        """
        output_files = []
        total_clips = len(refined_clips)
        transcript = transcript or []

        tier_info = RESOLUTION_TIERS.get(resolution_tier, RESOLUTION_TIERS[DEFAULT_RESOLUTION_TIER])
        target_w = tier_info["output_w"]
        target_h = tier_info["output_h"]
        h264_level = tier_info["h264_level"]
        max_bitrate = tier_info.get("max_bitrate", "20M")
        bufsize = tier_info.get("bufsize", "32M")

        # Bug 1 & 4 fix: Perform fast QSV capability smoke-test once per job
        if not self._qsv_tested:
            self._qsv_available = self._check_qsv_support()
            self._qsv_tested = True
            if self._qsv_available:
                logger.info("[QSV Probe] Intel QSV hardware acceleration confirmed operational.")
            else:
                logger.warning("[QSV Probe] Intel QSV hardware encoder unavailable/unsupported on this host. Skipping QSV, defaulting to CPU libx264.")

        # Bug 5 fix: Thread budget control
        num_threads = str(min(8, os.cpu_count() or 4))

        logger.info(
            f"[Resolution Tier & Rate Control Trace] Clipper initialized for tier '{resolution_tier}' -> "
            f"Target Canvas: {target_w}x{target_h}, Rate Control: global_quality={DEFAULT_QSV_QUALITY} (QSV ICQ) / CRF={DEFAULT_CRF} (CPU fallback), "
            f"H.264 Level: {h264_level}, Threads: {num_threads}"
        )

        src_w, src_h, _ = self._get_video_dimensions(video_path)
        if not src_w or not src_h:
            logger.warning("Could not probe source video dimensions via ffprobe. Defaulting to 1920x1080 landscape.")
            src_w = 1920
            src_h = 1080

        logger.info(f"[Crop Math] Source dimensions: {src_w}x{src_h} | Output canvas: {target_w}x{target_h}")

        use_facecam = bool(facecam_region and facecam_confirmed)

        # Flash-forward constants
        FLASH_DURATION = 2.0    # seconds of peak preview shown at start
        DIVIDER_DUR = 0.3       # seconds of black frame between flash and main clip

        for idx, clip in enumerate(refined_clips):
            if cancel_check and cancel_check():
                logger.warning("Clipper: cancellation requested. Halting remaining clips.")
                raise InterruptedError("Cancelled by user.")

            clip_num = idx + 1
            start_t = clip["start_time"]
            duration = clip["duration"]
            clip_start_time_stamp = time.time()

            out_filename = CLIPS_DIR / f"{job_id}_Clip_{clip_num:02d}.mp4"
            ass_filename = TEMP_DIR / f"{job_id}_Clip_{clip_num:02d}_subs.ass"
            flash_ass_filename = TEMP_DIR / f"{job_id}_Clip_{clip_num:02d}_flash.ass"

            # ── Flash-Forward: determine if applicable ────────────────────────────────────
            # Only apply when: feature enabled AND clip is long enough (>= 15s) AND not already a very short clip
            ai_peak_offset = clip.get("peak_moment_offset")
            if ai_peak_offset is not None:
                try:
                    ai_peak_offset = float(ai_peak_offset)
                except (TypeError, ValueError):
                    ai_peak_offset = None

            do_flash = (
                flash_forward_hook
                and duration >= 15.0
                and not use_facecam  # Mode C (IRL) only — desktop mode has different structure
            )

            if do_flash:
                peak_offset = _find_peak_moment(clip, transcript, duration, ai_peak_offset)
                peak_abs = start_t + peak_offset  # absolute video timestamp of peak
                generate_flash_ass_subtitles(
                    clip, transcript, peak_offset, FLASH_DURATION, flash_ass_filename,
                    output_w=target_w, output_h=target_h
                )
                logger.info(
                    f"Clip {clip_num}: Flash-Forward enabled | peak_offset={peak_offset:.1f}s "
                    f"(abs={peak_abs:.1f}s, source={'AI' if ai_peak_offset is not None else 'heuristic'})"
                )
            else:
                peak_offset = 0.0
                peak_abs = start_t
                if flash_forward_hook and use_facecam:
                    logger.info(f"Clip {clip_num}: Flash-Forward skipped (Desktop Streamer mode)")
                elif flash_forward_hook:
                    logger.info(f"Clip {clip_num}: Flash-Forward skipped (clip too short: {duration:.1f}s < 15s)")

            msg = f"Cutting Clip {clip_num}/{total_clips} ({resolution_tier} {target_w}x{target_h}, {start_t}s, duration: {duration}s)..."
            logger.info(msg)
            if self.progress_callback:
                self.progress_callback(clip_num, total_clips, msg)

            try:
                filter_args = []
                logo_input_args = []
                use_logo = False

                if use_facecam and facecam_region:
                    # ── MODE B: Desktop Streamer — 16:9 letterboxed + branding ─────────────
                    scaled_video_h = int(round(target_w * src_h / src_w))
                    if scaled_video_h % 2 != 0:
                        scaled_video_h += 1
                    bar_height = max(0, (target_h - scaled_video_h) // 2)

                    logo_path = get_logo_path(selected_logo)
                    use_logo = bool(logo_path and bar_height > 60)
                    subscribe_path = get_subscribe_path()
                    use_subscribe = bool(subscribe_path and bar_height > 60)

                    generate_ass_subtitles(
                        clip, transcript, ass_filename,
                        output_w=target_w, output_h=target_h,
                        bottom_bar_height=bar_height if bar_height > 60 else None
                    )
                    escaped_ass = _escape_ass_path(ass_filename)

                    logger.info(
                        f"Clip {clip_num}: Desktop Streamer Full-Frame 16:9 (Black Letterbox) "
                        f"({target_w}x{target_h}, bar_height={bar_height}px, logo={'on (' + logo_path.name + ')' if use_logo else 'off'}, subscribe={'on' if use_subscribe else 'off'})"
                    )

                    like_path = get_like_path()
                    use_like = bool(like_path and bar_height > 60)

                    if use_logo:
                        if logo_path.suffix.lower() == ".gif":
                            logo_input_args.extend(["-ignore_loop", "0", "-i", str(logo_path)])
                        else:
                            logo_input_args.extend(["-loop", "1", "-i", str(logo_path)])
                    if use_like:
                        if like_path.suffix.lower() == ".gif":
                            logo_input_args.extend(["-ignore_loop", "0", "-i", str(like_path)])
                        else:
                            logo_input_args.extend(["-loop", "1", "-i", str(like_path)])
                    if use_subscribe:
                        if subscribe_path.suffix.lower() == ".gif":
                            logo_input_args.extend(["-ignore_loop", "0", "-i", str(subscribe_path)])
                        else:
                            logo_input_args.extend(["-loop", "1", "-i", str(subscribe_path)])

                    # ── Sizing constants ──────────────────────────────────────────────────
                    # Logo: centered in TOP black bar — Large & prominent branding
                    logo_h = max(60, min(bar_height - 60, round(target_w * 0.36)))
                    logo_y = max(10, (bar_height - logo_h) // 2)

                    # Badges in LOWER black box (bottom bar) — Super Extra Large (25% canvas width):
                    sub_h = max(210, round(target_w * 0.25))
                    like_h = sub_h
                    sub_w = int(round(sub_h * 420 / 270))
                    like_w = int(round(like_h * 440 / 420))
                    gap = max(35, round(target_w * 0.045))

                    # Centered in the middle of the lower black box:
                    badge_y = (target_h - bar_height) + (bar_height - sub_h) // 2

                    if use_subscribe and use_like:
                        # Subscribe on left, Like on right
                        total_w = sub_w + gap + like_w
                        start_x = (target_w - total_w) // 2
                        sub_x = str(start_x)
                        like_x = str(start_x + sub_w + gap)
                    elif use_subscribe:
                        sub_x = "(W-w)/2"
                    elif use_like:
                        like_x = "(W-w)/2"

                    # ── Build filter_complex based on which assets exist ──────────────────
                    # Input order MUST match logo_input_args: 0=video, 1=logo, 2=like, 3=subscribe
                    input_idx = 1

                    if use_logo:
                        logo_idx = input_idx; input_idx += 1
                    if use_like:
                        like_idx = input_idx; input_idx += 1
                    if use_subscribe:
                        sub_idx = input_idx; input_idx += 1

                    # Start building filter chain
                    fc = (
                        f"[0:v]scale={target_w}:-2:flags=bicubic,"
                        f"pad={target_w}:{target_h}:0:(oh-ih)/2:color=black[bg];"
                    )
                    prev = "bg"

                    if use_logo:
                        fc += f"[{logo_idx}:v]fps=30,scale=-1:{logo_h}:flags=bicubic,format=rgba[logo];"
                        fc += f"[{prev}][logo]overlay=(W-w)/2:{logo_y}:shortest=1[bg_logo];"
                        prev = "bg_logo"

                    if use_like:
                        like_filter = "fps=30,"
                        if like_path.suffix.lower() == ".gif":
                            like_filter += "colorkey=white:0.15:0.1,crop=440:420:180:90,"
                        like_filter += f"scale=-1:{like_h}:flags=bicubic,format=rgba"
                        fc += f"[{like_idx}:v]{like_filter}[like];"
                        fc += f"[{prev}][like]overlay={like_x}:{badge_y}:shortest=1[bg_like];"
                        prev = "bg_like"

                    if use_subscribe:
                        sub_filter = "fps=30,"
                        if subscribe_path.suffix.lower() == ".gif":
                            sub_filter += "crop=420:270:40:200,"
                        sub_filter += f"scale=-1:{sub_h}:flags=bicubic,format=rgba"
                        fc += f"[{sub_idx}:v]{sub_filter}[sub];"
                        fc += f"[{prev}][sub]overlay={sub_x}:{badge_y}:shortest=1[bg_sub];"
                        prev = "bg_sub"

                    fc += f"[{prev}]subtitles={escaped_ass},format=nv12[v]"

                    if use_logo or use_subscribe or use_like:
                        complex_filter = fc
                        filter_args = ["-filter_complex", complex_filter, "-map", "[v]", "-map", "0:a?"]
                    else:
                        vf_filter = (
                            f"scale={target_w}:-2:flags=bicubic,"
                            f"pad={target_w}:{target_h}:0:(oh-ih)/2:color=black,"
                            f"subtitles={escaped_ass},format=nv12"
                        )
                        filter_args = ["-vf", vf_filter]


                else:
                    # ── MODE C: IRL / Smart Face-Centered 9:16 Vertical Reframe ──────────────
                    generate_ass_subtitles(clip, transcript, ass_filename, output_w=target_w, output_h=target_h)
                    escaped_ass = _escape_ass_path(ass_filename)

                    crop_w = int(src_h * (9.0 / 16.0))
                    scaler_filter = f"scale={target_w}:{target_h}:flags=bicubic,unsharp=3:3:0.2:3:3:0.0"

                    keyframes = self.detector.detect_speaker_crop_timeline(
                        video_path, start_t, duration, src_w, src_h
                    )
                    crop_x_expr = build_crop_x_expression(keyframes)
                    escaped_crop_x_expr = crop_x_expr.replace(",", r"\,")

                    # ── Core video filter chain for a single pass of the clip ─────────────
                    # Used for both the flash segment (trimmed) and the main segment
                    core_vf = (
                        f"{DENOISE_FILTER},"
                        f"crop=w={crop_w}:h={src_h}:x={escaped_crop_x_expr}:y=0,"
                        f"{scaler_filter},setsar=1"
                    )

                    if do_flash:
                        # ── FLASH-FORWARD filter_complex ──────────────────────────────────
                        # Input [0:v] is the full source video (seeked to start_t).
                        # We split it into two paths:
                        #   [flash_v]: trim to peak window (peak_offset → peak_offset+FLASH_DURATION)
                        #   [main_v]:  trim full clip (0 → duration), with subtitles
                        # Then concat: [flash_v][div_v][main_v] → final output
                        # Audio: silence during flash+divider, real audio during main clip.
                        escaped_flash_ass = _escape_ass_path(flash_ass_filename)
                        total_output_dur = FLASH_DURATION + DIVIDER_DUR + duration

                        complex_filter = (
                            # ── Source split ────────────────────────────────────────────────
                            f"[0:v]split=2[flash_src][main_src];"

                            # ── Flash segment: trim to peak window, apply same crop+scale, add flash subs ──
                            f"[flash_src]trim=start={peak_offset:.3f}:end={peak_offset + FLASH_DURATION:.3f},"
                            f"setpts=PTS-STARTPTS,"
                            f"{core_vf},"
                            # NOTE: No quotes around path — single quotes are special in FFmpeg filter parser
                            # and cause EINVAL on Windows. Unquoted relative POSIX path works correctly.
                            f"subtitles={escaped_flash_ass},setsar=1,format=yuv420p[flash_v];"

                            # ── 0.3s black divider ────────────────────────────────────────
                            f"color=c=black:s={target_w}x{target_h}:d={DIVIDER_DUR:.3f}:r=60,"
                            f"setsar=1,format=yuv420p[div_v];"

                            # ── Main clip: full duration with panning crop + main subtitles ──
                            f"[main_src]trim=start=0:end={duration:.3f},"
                            f"setpts=PTS-STARTPTS,"
                            f"{core_vf},"
                            f"subtitles={escaped_ass},setsar=1,format=yuv420p[main_v];"

                            # ── Concat all three video segments ───────────────────────────
                            f"[flash_v][div_v][main_v]concat=n=3:v=1:a=0,fps=60,format=nv12[v_out];"

                            # ── Audio: silence for flash+divider, real audio for main clip ─
                            # anullsrc fills the flash+divider window; atrim takes main audio
                            f"anullsrc=r=48000:cl=stereo,"
                            f"atrim=duration={FLASH_DURATION + DIVIDER_DUR:.3f}[silence];"
                            f"[0:a]atrim=start=0:end={duration:.3f},asetpts=PTS-STARTPTS,aresample=async=1[main_a];"
                            f"[silence][main_a]concat=n=2:v=0:a=1[a_out]"
                        )
                        filter_args = [
                            "-filter_complex", complex_filter,
                            "-map", "[v_out]",
                            "-map", "[a_out]",
                        ]
                        logger.info(
                            f"Clip {clip_num}: Mode C + Flash-Forward | "
                            f"src={src_w}x{src_h} crop_w={crop_w} keyframes={len(keyframes)} "
                            f"peak@{peak_offset:.1f}s total_dur={total_output_dur:.1f}s"
                        )
                    else:
                        # ── Standard Mode C (no flash-forward) ───────────────────────────
                        vf_filter = (
                            f"{core_vf},"
                            f"subtitles={escaped_ass},format=nv12"
                        )
                        filter_args = ["-vf", vf_filter]
                        logger.info(
                            f"Clip {clip_num}: Mode C (9:16 panning face-centered) | "
                            f"src={src_w}x{src_h} crop_w={crop_w} keyframes={len(keyframes)}"
                        )


                # Bug 3 fix: Scaled timeout formula reflecting canvas resolution tier & duration
                tier_mult = 2.0 if target_h >= 2560 else (1.5 if target_h >= 1920 else 1.0)
                encode_timeout = max(300, int(float(duration) * 15 * tier_mult))

                encode_mode_used = "CPU libx264 (CRF 18)"
                clip_rendered_ok = False

                out_duration = (FLASH_DURATION + DIVIDER_DUR + duration) if do_flash else duration

                # FFmpeg 8.x: Cannot use -af if audio is generated by -filter_complex ([a_out])
                audio_filter_args = [] if any("[a_out]" in str(a) for a in filter_args) else ["-af", "aresample=async=1"]

                # 1. Primary Attempt: Intel QSV Hardware Accelerated GPU Encode (if available)
                if self._qsv_available:
                    qsv_cmd = [
                        "ffmpeg", "-y",
                        "-thread_queue_size", "512",
                        "-ss", str(start_t),
                        "-t", str(duration),
                        "-i", str(video_path),
                        *logo_input_args,
                        *filter_args,
                        "-profile:v", "high", "-level", h264_level,
                        "-c:v", "h264_qsv", "-preset", "medium",
                        "-global_quality", str(DEFAULT_QSV_QUALITY),
                        "-pix_fmt", "nv12", "-g", "60",
                        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                        *audio_filter_args,
                        "-shortest",
                        "-t", str(out_duration),
                        "-movflags", "+faststart",
                        "-avoid_negative_ts", "make_zero",
                        str(out_filename)
                    ]

                    try:
                        result = subprocess.run(qsv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=encode_timeout)
                        if result.returncode == 0:
                            encode_mode_used = "Intel QSV (h264_qsv)"
                            clip_rendered_ok = True
                        else:
                            err_tail = "\n".join((result.stderr or "").strip().splitlines()[-6:])
                            logger.warning(f"QSV render returned error code {result.returncode} for Clip {clip_num}. Error: {err_tail}")
                            if "Cannot allocate memory" in (result.stderr or "") or result.returncode in (4294967284, -12):
                                logger.warning(f"Intel QSV ran out of hardware surface memory for tier '{resolution_tier}'. Disabling QSV for remaining clips in this run to save time.")
                                self._qsv_available = False
                    except subprocess.TimeoutExpired:
                        logger.warning(f"QSV encode timed out for Clip {clip_num} after {encode_timeout}s. Falling back to CPU for this clip...")
                        self._qsv_available = False

                # 2. Secondary Attempt: CPU H.264 with CRF 18 (-preset faster)
                if not clip_rendered_ok:
                    cpu_cmd = [
                        "ffmpeg", "-y",
                        "-thread_queue_size", "512",
                        "-threads", num_threads,
                        "-ss", str(start_t),
                        "-t", str(duration),
                        "-i", str(video_path),
                        *logo_input_args,
                        *filter_args,
                        "-profile:v", "high", "-level", h264_level,
                        "-c:v", "libx264", "-preset", "faster", "-crf", "18",
                        "-maxrate", max_bitrate, "-bufsize", bufsize,
                        "-pix_fmt", "yuv420p", "-g", "60",
                        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                        *audio_filter_args,
                        "-shortest",
                        "-t", str(out_duration),
                        "-movflags", "+faststart",
                        "-avoid_negative_ts", "make_zero",
                        str(out_filename)
                    ]
                    
                    try:
                        cpu_res = subprocess.run(cpu_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=encode_timeout)
                        if cpu_res.returncode == 0:
                            encode_mode_used = "CPU libx264 (-preset faster)"
                            clip_rendered_ok = True
                        else:
                            err_tail = "\n".join((cpu_res.stderr or "").strip().splitlines()[-6:])
                            logger.warning(f"CPU libx264 (-preset faster) returned error {cpu_res.returncode} for Clip {clip_num}. Error: {err_tail}")
                    except subprocess.TimeoutExpired:
                        logger.warning(f"CPU libx264 (-preset faster) timed out for Clip {clip_num} after {encode_timeout}s.")

                # 3. Tertiary Attempt: Intermediate CPU H.264 with `-preset veryfast` (Bug 5 fix: intermediate tier retaining subtitles & logo)
                if not clip_rendered_ok:
                    logger.warning(f"Clip {clip_num}: Retrying with intermediate CPU libx264 (-preset veryfast)...")
                    veryfast_cmd = [
                        "ffmpeg", "-y",
                        "-thread_queue_size", "512",
                        "-threads", num_threads,
                        "-ss", str(start_t),
                        "-t", str(duration),
                        "-i", str(video_path),
                        *logo_input_args,
                        *filter_args,
                        "-profile:v", "high", "-level", h264_level,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-maxrate", max_bitrate, "-bufsize", bufsize,
                        "-pix_fmt", "yuv420p", "-g", "60",
                        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                        *audio_filter_args,
                        "-shortest",
                        "-t", str(out_duration),
                        "-movflags", "+faststart",
                        "-avoid_negative_ts", "make_zero",
                        str(out_filename)
                    ]
                    try:
                        vf_res = subprocess.run(veryfast_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=encode_timeout)
                        if vf_res.returncode == 0:
                            encode_mode_used = "CPU libx264 (-preset veryfast)"
                            clip_rendered_ok = True
                        else:
                            err_tail = "\n".join((vf_res.stderr or "").strip().splitlines()[-6:])
                            logger.warning(f"CPU libx264 (-preset veryfast) returned error {vf_res.returncode} for Clip {clip_num}. Error: {err_tail}")
                    except subprocess.TimeoutExpired:
                        logger.warning(f"CPU libx264 (-preset veryfast) timed out for Clip {clip_num} after {encode_timeout}s.")

                # 4. Quaternary Attempt: Safe Fallback (Bare center crop, no subtitles/logo)
                if not clip_rendered_ok:
                    logger.warning(
                        f"CPU libx264 render failed for Clip {clip_num}. "
                        f"Retrying bare safe fallback (center-crop to {target_w}x{target_h})..."
                    )
                    safe_crop_w = int(src_h * 9.0 / 16.0)
                    safe_crop_x = max(0, (src_w - safe_crop_w) // 2)
                    fallback_filter = (
                        f"crop={safe_crop_w}:{src_h}:{safe_crop_x}:0,"
                        f"scale={target_w}:{target_h}:flags=lanczos"
                    )
                    fallback_cmd = [
                        "ffmpeg", "-y",
                        "-threads", num_threads,
                        "-ss", str(start_t),
                        "-i", str(video_path),
                        "-t", str(duration),
                        "-vf", fallback_filter,
                        "-profile:v", "high", "-level", h264_level,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-maxrate", max_bitrate, "-bufsize", bufsize,
                        "-pix_fmt", "yuv420p", "-g", "60",
                        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                        "-af", "aresample=async=1", "-shortest",
                        "-movflags", "+faststart",
                        "-avoid_negative_ts", "make_zero",
                        str(out_filename)
                    ]
                    try:
                        subprocess.run(fallback_cmd, check=True, timeout=encode_timeout)
                        encode_mode_used = "CPU libx264 (Safe Bare Fallback)"
                        clip_rendered_ok = True
                    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as fb_err:
                        logger.error(f"Safe fallback encode failed or timed out for Clip {clip_num}: {fb_err}")

                # Clean up temporary ASS files
                for tmp_ass in [ass_filename, flash_ass_filename]:
                    try:
                        if tmp_ass and tmp_ass.exists():
                            tmp_ass.unlink()
                    except Exception as cleanup_err:
                        logger.debug(f"ASS subtitle cleanup notice for Clip {clip_num} ({tmp_ass.name}): {cleanup_err}")

                # Verify rendered output file
                if out_filename.exists() and out_filename.stat().st_size > 0:
                    actual_w, actual_h, actual_lvl = self._get_video_dimensions(out_filename)
                    elapsed_clip_sec = time.time() - clip_start_time_stamp
                    logger.info(
                        f"Clip {clip_num} generated successfully ({resolution_tier}) via {encode_mode_used} in {elapsed_clip_sec:.1f}s: "
                        f"{out_filename.name} (Verified: {actual_w}x{actual_h}, H.264 level {actual_lvl})"
                    )
                    output_files.append(out_filename)
                else:
                    logger.error(f"Clip {clip_num} output file missing or zero bytes after render: {out_filename}")

            except Exception as e:
                logger.error(f"Failed to generate Clip {clip_num}/{total_clips} ({start_t}s): {e}. Skipping clip.")
                continue

        return output_files


# Backward compatibility alias
VideoClipper = FFmpegClipper
