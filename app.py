"""Application Pipeline Orchestrator with dynamic settings integration, resolution tiers, facecam framing, and auto-calibration."""

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from analyzer import VideoAnalyzer, _detect_content_type, clear_transcript_cache
from clipper import FFmpegClipper
from config import DEFAULT_RESOLUTION_TIER, resolve_encode_tier
from utils import resolve_ffprobe_bin
from downloader import YouTubeDownloader
from facecam_calibration import FacecamCalibrator
from settings_store import load_settings
from timestamp_refiner import TimestampRefiner
from utils import UILogHandler, logger


class PipelineOrchestrator:
    """Orchestrates end-to-end processing pipeline with stage updates, dynamic settings, resolution tiers, facecam framing, and temp cleanup."""

    def __init__(
        self,
        progress_cb: Optional[Callable[[str, float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        api_key: Optional[str] = None
    ):
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.api_key = api_key
        self.is_cancelled = False

    def emit_progress(self, stage: str, percent: float, status: str):
        logger.info(f"[{stage}] {percent:.0f}% - {status}")
        if self.progress_cb:
            self.progress_cb(stage, percent, status)

    def cancel(self):
        """Cancel processing."""
        self.is_cancelled = True
        logger.warning("Pipeline cancellation requested.")

    def _cleanup_temp_files(self, video_path: Optional[Path], audio_path: Optional[Path]):
        """Clean up intermediate audio and temp files. Preserves full downloads in DOWNLOADS_DIR for instant re-runs and logo switching."""
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
                logger.info(f"Cleaned up temporary file: {audio_path.name}")
            except Exception as e:
                logger.warning(f"Could not remove temp file {audio_path}: {e}")

    def run(
        self,
        youtube_url: str,
        job_id: str = "default",
        facecam_region: Optional[Dict[str, float]] = None,
        resolution_tier: Optional[str] = None,
        selected_logo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute end-to-end YouTube Shorts clipping pipeline."""
        self.is_cancelled = False
        video_path: Optional[Path] = None
        audio_path: Optional[Path] = None
        title = "YouTube Video"

        # Attach UI Log Handler so all module logs stream live over WebSocket
        ui_handler = None
        if self.log_cb:
            ui_handler = UILogHandler(self.log_cb)
            ui_handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(ui_handler)
            logging.getLogger().addHandler(ui_handler)

        try:
            # Load settings dynamically at job start
            settings = load_settings()
            effective_api_key = self.api_key or settings.get("gemini_api_key", "")
            requested_tier = resolution_tier or settings.get("resolution_tier", DEFAULT_RESOLUTION_TIER)
            effective_tier = resolve_encode_tier(requested_tier)
            if effective_tier != requested_tier:
                logger.warning(
                    f"[Memory Guard] Requested {requested_tier} downgraded to {effective_tier} for this host."
                )

            # Stage 1: Downloading Video
            self.emit_progress("Downloading Video", 10.0, f"Fetching video ({effective_tier}) via yt-dlp...")
            downloader = YouTubeDownloader(
                progress_callback=lambda p, msg: self.emit_progress("Downloading Video", p, msg)
            )
            video_path, audio_path, title, channel_id = downloader.download(youtube_url, resolution_tier=effective_tier)

            if self.is_cancelled:
                raise InterruptedError("Cancelled by user.")

            # Get total video duration using ffprobe
            ffprobe = resolve_ffprobe_bin() or "ffprobe"
            dur_cmd = [
                ffprobe, "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
            ]
            video_duration = 300.0
            try:
                # Bug 2 fix: Add 15s timeout to ffprobe duration probe
                res = subprocess.run(dur_cmd, stdout=subprocess.PIPE, text=True, timeout=15)
                if res.returncode == 0 and res.stdout.strip():
                    video_duration = float(res.stdout.strip().splitlines()[0])
            except Exception as e:
                logger.warning(f"ffprobe duration probe timed out or failed ({e}). Defaulting to 300.0s.")
                video_duration = 300.0

            # Mode Detection — respects explicit user UI choice over auto-calibration.
            # Frontend sends {is_irl: true} or {force_desktop: true} — never null or fake coordinates.
            is_explicit_irl = isinstance(facecam_region, dict) and bool(facecam_region.get("is_irl"))
            is_force_desktop = isinstance(facecam_region, dict) and bool(facecam_region.get("force_desktop"))

            if is_explicit_irl:
                # User explicitly chose IRL Smart Face Crop — clear facecam_region, skip all presets/calibration
                facecam_region = None
                logger.info("[Content Mode] IRL / Smart Face Crop mode active (user explicitly selected — preset override skipped).")
                self.emit_progress("Downloading Video", 92.0, "ℹ IRL / Smart speaker face tracking active.")

            elif is_force_desktop:
                # User explicitly chose Desktop Streamer mode — load saved preset if exists (best accuracy), no re-calibration
                calibrator = FacecamCalibrator()
                existing_preset = calibrator.load_channel_preset(channel_id) if channel_id else None
                if existing_preset:
                    facecam_region = existing_preset
                    logger.info(f"[Content Mode] Desktop Streamer mode active — using saved preset for channel '{channel_id}'.")
                else:
                    # No preset: still honour Desktop mode with a full-frame sentinel region
                    facecam_region = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "_desktop_sentinel": True}
                    logger.info("[Content Mode] Desktop Streamer mode active — no saved preset, using full-frame sentinel.")
                self.emit_progress("Downloading Video", 92.0, "✅ Desktop Streamer mode active (Full-Frame 16:9 + Black Letterbox).")

            else:
                # No explicit mode flag (direct API call) — run auto-calibration as before
                if channel_id:
                    calibrator = FacecamCalibrator()
                    existing_preset = calibrator.load_channel_preset(channel_id)
                    if existing_preset:
                        logger.info(f"[Calibration] Loaded saved facecam preset for channel '{channel_id}'")
                        facecam_region = existing_preset
                    else:
                        logger.info(f"[Calibration] No preset found for channel '{channel_id}'. Running auto-calibration...")
                        calibrated_region = calibrator.calibrate_from_video(video_path, channel_id, force=True)
                        if calibrated_region:
                            logger.info(f"[Calibration] ✅ Freshly calibrated facecam region for channel '{channel_id}'")
                            facecam_region = calibrated_region
                        else:
                            logger.info(f"[Calibration] No consistent facecam region found for channel '{channel_id}' — using IRL mode.")

            # Confirm Desktop Streamer mode if facecam region is present (via preset, calibration, or sentinel)
            facecam_confirmed = False
            if facecam_region:
                facecam_confirmed = True
                if not (is_explicit_irl or is_force_desktop):
                    logger.info("[Content Mode] Desktop Streamer mode active (Full-Frame 16:9 + Black Letterbox).")
                    self.emit_progress("Downloading Video", 92.0, "✅ Desktop Streamer mode active (Full-Frame 16:9 + Black Letterbox).")
            else:
                if not (is_explicit_irl or is_force_desktop):
                    logger.info("[Content Mode] IRL / Smart Face Crop mode active.")
                    self.emit_progress("Downloading Video", 92.0, "ℹ IRL / Smart speaker face tracking active.")

            # Stage 2: Audio Prep
            self.emit_progress("Extracting Audio", 100.0, "Audio ready (direct transcript source).")

            # Stage 3: Generating Transcript
            self.emit_progress("Generating Transcript", 30.0, "Fetching transcript timestamps...")
            analyzer = VideoAnalyzer(api_key=effective_api_key, settings=settings)
            transcript = analyzer.fetch_transcript(youtube_url)
            self.emit_progress("Generating Transcript", 100.0, f"Transcript loaded ({len(transcript)} entries).")

            if self.is_cancelled:
                raise InterruptedError("Cancelled by user.")

            # Stage 4: AI Analyzing Video & Finding Hooks
            self.emit_progress("AI Analyzing Video", 50.0, "Analyzing full transcript for viral moments...")
            raw_clips, analysis_method = analyzer.analyze_viral_clips(transcript, video_duration, title, youtube_url=youtube_url)

            if facecam_region:
                # facecam_confirmed is always True here (set unconditionally at line 136-137)
                analysis_method += "_facecam"

            # Stage 5: Timestamp Refinement (Hook-Aware Smart Buffer)
            self.emit_progress("Finding Viral Hooks", 80.0, f"Refining candidate clip boundaries ({analysis_method})...")
            refiner = TimestampRefiner(settings=settings, transcript=transcript)
            refined_clips = refiner.refine_clips(raw_clips, video_duration)

            if self.is_cancelled:
                raise InterruptedError("Cancelled by user.")

            if not refined_clips:
                logger.warning("Zero candidate clips passed confidence/spacing filters.")
                self.emit_progress("Finalizing...", 100.0, "No candidate clips met confidence threshold.")
                return {
                    "output_files": [],
                    "analysis_method": analysis_method,
                    "attempted_clips": 0,
                    "generated_clips": 0,
                    "video_title": title,
                    "resolution_tier": effective_tier,
                    "raw_clips": []
                }

            # Stage 6: Cutting Clips
            attempted_count = len(refined_clips)
            clipper = FFmpegClipper(
                progress_callback=lambda current, total, msg: self.emit_progress(
                    f"Cutting Clip {current}/{total}",
                    (current / total) * 100.0,
                    msg
                )
            )
            content_type = _detect_content_type(transcript)
            flash_forward_enabled = bool(settings.get("flash_forward_hook", True))
            output_files = clipper.cut_clips(
                video_path,
                refined_clips,
                transcript=transcript,
                job_id=job_id,
                facecam_region=facecam_region,
                facecam_confirmed=facecam_confirmed,
                resolution_tier=effective_tier,
                content_type=content_type,
                flash_forward_hook=flash_forward_enabled,
                selected_logo=selected_logo,
                cancel_check=lambda: self.is_cancelled,
            )

            # Finalizing
            self.emit_progress("Finalizing...", 100.0, "Processing complete!")
            logger.info(f"Pipeline finished! Generated {len(output_files)}/{attempted_count} clips (method: {analysis_method}, tier: {effective_tier})")

            return {
                "output_files": output_files,
                "analysis_method": analysis_method,
                "attempted_clips": attempted_count,
                "generated_clips": len(output_files),
                "video_title": title,
                "resolution_tier": effective_tier,
                "raw_clips": refined_clips
            }

        finally:
            if ui_handler:
                try:
                    logger.removeHandler(ui_handler)
                    logging.getLogger().removeHandler(ui_handler)
                except Exception as handler_err:
                    # Bug 12 fix: Log swallowed logger cleanup exception at DEBUG level
                    logger.debug(f"UI handler cleanup notice: {handler_err}")
            # Clear transcript cache to free memory
            clear_transcript_cache()
            # Always clean up temporary video and audio files
            self._cleanup_temp_files(video_path, audio_path)
