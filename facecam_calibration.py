"""Facecam Auto-Calibration & Preset Memory System.

Extracts spread-sampled keyframes from a video, runs MediaPipe face detection,
clusters detections into corner regions (common facecam positions), and
saves the learned bounding box to facecam_presets.json keyed by YouTube channel ID.

Usage:
    calibrator = FacecamCalibrator()
    region = calibrator.calibrate_from_video(video_path, channel_id)
    # Returns {"x": 0.0, "y": 0.0, "w": 0.22, "h": 0.30} or None
"""

import json
import subprocess
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

import cv2

from config import BASE_DIR, TEMP_DIR
from face_detector_helper import create_face_detector, detect_faces_in_bgr_frame
from utils import logger

# Path to per-channel preset store
PRESETS_FILE = BASE_DIR / "facecam_presets.json"

# Corner zone definitions as (x_min, y_min, x_max, y_max) in normalized [0,1] coords.
# These cover the four quadrant corners where streamers typically place their facecam.
CORNER_ZONES: Dict[str, Tuple[float, float, float, float]] = {
    "top_left":     (0.00, 0.00, 0.35, 0.40),
    "top_right":    (0.65, 0.00, 1.00, 0.40),
    "bottom_left":  (0.00, 0.60, 0.35, 1.00),
    "bottom_right": (0.65, 0.60, 1.00, 1.00),
}

# Minimum fraction of sampled frames that must contain a face in the
# same corner zone before we trust that zone as the facecam position.
MIN_ZONE_CONSISTENCY = 0.40  # 40% of frames = reliable


class FacecamCalibrator:
    """Learns per-channel facecam region from video keyframes and persists it."""

    # -----------------------------------------------------------------------
    # Preset Store — Save / Load / List / Clear
    # -----------------------------------------------------------------------

    def load_presets(self) -> Dict[str, Any]:
        """Load all saved facecam presets from JSON file."""
        if not PRESETS_FILE.exists():
            return {}
        try:
            return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not read facecam_presets.json ({e}). Returning empty.")
            return {}

    def save_presets(self, presets: Dict[str, Any]) -> None:
        """Persist updated presets dict to JSON file."""
        try:
            PRESETS_FILE.write_text(
                json.dumps(presets, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Could not write facecam_presets.json ({e}).")

    def load_channel_preset(self, channel_id: str, force: bool = False) -> Optional[Dict[str, Any]]:
        """Return saved facecam preset for a channel, or None if not calibrated.

        Bug 9 fix: Caches low-confidence calibration failures with 'failed': True so repeated jobs skip re-scanning.
        """
        presets = self.load_presets()
        preset = presets.get(channel_id)
        if preset:
            if preset.get("failed"):
                logger.info(f"[Calibration] Channel '{channel_id}' has a cached low-confidence calibration failure. Returning None (skipping re-scan).")
                return None

            # Only log coordinates for valid (non-failed) presets
            logger.info(
                f"[Calibration] Loaded preset for channel '{channel_id}': "
                f"x={preset.get('x', 0.0):.3f} y={preset.get('y', 0.0):.3f} "
                f"w={preset.get('w', 0.0):.3f} h={preset.get('h', 0.0):.3f} "
                f"(confidence={preset.get('confidence', 0):.2f}, "
                f"calibrated_at={preset.get('calibrated_at', 'unknown')})"
            )
        return preset

    def save_channel_preset(
        self, channel_id: str, region: Dict[str, float], confidence: float
    ) -> None:
        """Save calibrated facecam region for a channel."""
        presets = self.load_presets()
        presets[channel_id] = {
            "x": round(region["x"], 4),
            "y": round(region["y"], 4),
            "w": round(region["w"], 4),
            "h": round(region["h"], 4),
            "confidence": round(confidence, 3),
            "calibrated_at": str(date.today()),
            "failed": False,
        }
        self.save_presets(presets)
        logger.info(
            f"[Calibration] Saved preset for channel '{channel_id}': "
            f"x={region['x']:.3f} y={region['y']:.3f} "
            f"w={region['w']:.3f} h={region['h']:.3f} (confidence={confidence:.2f})"
        )

    def clear_channel_preset(self, channel_id: str) -> bool:
        """Remove saved preset for a channel. Returns True if found and removed."""
        presets = self.load_presets()
        if channel_id in presets:
            del presets[channel_id]
            self.save_presets(presets)
            logger.info(f"[Calibration] Cleared preset for channel '{channel_id}'.")
            return True
        return False

    def list_presets(self) -> List[Dict[str, Any]]:
        """Return a list of all saved channel presets with their metadata."""
        presets = self.load_presets()
        return [
            {"channel_id": ch_id, **data}
            for ch_id, data in presets.items()
        ]

    # -----------------------------------------------------------------------
    # Keyframe Extraction
    # -----------------------------------------------------------------------

    def extract_sample_keyframes(
        self,
        video_path: Path,
        n: int = 16,
        scan_duration_sec: float = 600.0
    ) -> List[Path]:
        """Extract n spread-sampled JPEG keyframes across the video duration (up to scan_duration_sec)
        using FFmpeg.

        Returns list of temp frame paths that exist on disk.
        """
        frame_paths: List[Path] = []

        try:
            # Probe actual video duration
            probe_cmd = [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration",
                "-of", "csv=p=0",
                str(video_path)
            ]
            res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            video_dur = float(res.stdout.strip() or "0") if res.returncode == 0 else 0.0
            if video_dur <= 0:
                video_dur = scan_duration_sec

            # Step 7 polish: spread sampling across full video duration up to max scan cap
            scan_end = min(max(scan_duration_sec, video_dur * 0.85), video_dur * 0.95)
            interval = scan_end / (n + 1)

            from concurrent.futures import ThreadPoolExecutor

            def _extract_single_keyframe(args: Tuple[int, float]) -> Optional[Path]:
                idx, timestamp = args
                frame_path = TEMP_DIR / f"calib_{video_path.stem}_{idx:03d}.jpg"
                cmd = [
                    "ffmpeg", "-y", "-ss", f"{timestamp:.2f}", "-i", str(video_path),
                    "-vframes", "1", "-q:v", "3", str(frame_path)
                ]
                try:
                    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                    if frame_path.exists():
                        return frame_path
                except Exception as frame_err:
                    logger.debug(f"[Calibration] Keyframe {idx} extraction failed: {frame_err}")
                return None

            tasks = [(i, i * interval) for i in range(1, n + 1)]
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(_extract_single_keyframe, tasks))
                for fp in results:
                    if fp:
                        frame_paths.append(fp)

        except Exception as e:
            logger.warning(f"[Calibration] Exception extracting keyframes: {e}")

        return frame_paths

    # -----------------------------------------------------------------------
    # Face Detection & Zone Clustering
    # -----------------------------------------------------------------------

    def _classify_face_zone(self, cx: float, cy: float) -> Optional[str]:
        """Classify normalized (cx, cy) face center into a corner zone."""
        for zone_name, (x1, y1, x2, y2) in CORNER_ZONES.items():
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return zone_name
        return None  # Face is in centre — not a typical facecam position

    def _detect_faces_in_frame(
        self,
        frame_path: Path,
        face_detector: Any,
    ) -> List[Dict[str, float]]:
        """Run MediaPipe face detection on a single frame.

        Returns list of normalized {cx, cy, w, h, score} dicts for each face found.
        """
        img = cv2.imread(str(frame_path))
        if img is None:
            return []
        return detect_faces_in_bgr_frame(img, face_detector)

    def _compute_region_from_detections(
        self,
        detections: List[Dict[str, float]]
    ) -> Dict[str, float]:
        """Compute a stable bounding box from a cluster of face detections.

        Uses median of x/y/w/h to eliminate outliers.
        Pads the region by 15% in each direction to capture the full head.
        """
        xs = [d["x"] for d in detections]
        ys = [d["y"] for d in detections]
        ws = [d["w"] for d in detections]
        hs = [d["h"] for d in detections]

        med_x = median(xs)
        med_y = median(ys)
        med_w = median(ws)
        med_h = median(hs)

        # Pad 15% for head room
        pad_x = med_w * 0.15
        pad_y = med_h * 0.15

        final_x = max(0.0, med_x - pad_x)
        final_y = max(0.0, med_y - pad_y)
        final_w = min(1.0 - final_x, med_w + 2 * pad_x)
        final_h = min(1.0 - final_y, med_h + 2 * pad_y)

        return {
            "x": round(final_x, 4),
            "y": round(final_y, 4),
            "w": round(final_w, 4),
            "h": round(final_h, 4),
        }

    # -----------------------------------------------------------------------
    # Main Calibration Entry Point
    # -----------------------------------------------------------------------

    def calibrate_from_video(
        self,
        video_path: Path,
        channel_id: str,
        n_frames: int = 16,
        scan_duration_sec: float = 180.0,
        force: bool = False,
    ) -> Optional[Dict[str, float]]:
        """Auto-detect facecam region from video and save as channel preset.

        Args:
            video_path: Path to the downloaded video file.
            channel_id: YouTube channel ID (used as preset key).
            n_frames: Number of keyframes to sample (default 16).
            scan_duration_sec: How many seconds of the video to scan (default 180).
            force: If True, re-calibrate even if a preset already exists.

        Returns:
            Normalized region dict {"x", "y", "w", "h"} if a reliable facecam
            was found, or None if the video has no consistent corner-zone face.
        """
        # 1. Check existing preset (or cached failure)
        if not force:
            presets = self.load_presets()
            cached = presets.get(channel_id)
            if cached:
                if cached.get("failed"):
                    logger.info(
                        f"[Calibration] Channel '{channel_id}' has a cached low-confidence calibration failure. "
                        "Skipping re-scan (pass force=True to re-calibrate)."
                    )
                    return None
                logger.info(
                    f"[Calibration] Using existing preset for '{channel_id}' "
                    "(pass force=True to re-calibrate)."
                )
                return cached

        logger.info(
            f"[Calibration] Starting auto-calibration for channel '{channel_id}' "
            f"from '{video_path.name}' ({n_frames} frames, {scan_duration_sec:.0f}s scan)..."
        )

        # 2. Extract keyframes
        frame_paths = self.extract_sample_keyframes(video_path, n=n_frames, scan_duration_sec=scan_duration_sec)
        if not frame_paths:
            logger.warning("[Calibration] No keyframes extracted. Calibration aborted.")
            return None

        # 3. Run face detection across all keyframes
        zone_detections: Dict[str, List[Dict[str, float]]] = {z: [] for z in CORNER_ZONES}
        frames_with_corner_face = 0
        total_faces_found = 0
        face_detector = None
        try:
            face_detector = create_face_detector(min_confidence=0.45)
            for frame_path in frame_paths:
                faces = self._detect_faces_in_frame(frame_path, face_detector)
                total_faces_found += len(faces)
                frame_had_corner_face = False
                for face in faces:
                    zone = self._classify_face_zone(face["cx"], face["cy"])
                    if zone:
                        zone_detections[zone].append(face)
                        frame_had_corner_face = True
                if frame_had_corner_face:
                    frames_with_corner_face += 1
        except Exception as e:
            logger.warning(f"[Calibration] Face detection run failed: {e}")
        finally:
            if 'face_detector' in locals() and face_detector:
                try:
                    face_detector.close()
                except Exception as close_err:
                    logger.debug(f"[Calibration] face_detector.close() notice: {close_err}")
            # Clean up temp keyframe files
            for fp in frame_paths:
                try:
                    if fp.exists():
                        fp.unlink()
                except Exception as cleanup_err:
                    # Bug 12 fix: Log swallowed keyframe cleanup exception at DEBUG level
                    logger.debug(f"[Calibration] Keyframe cleanup debug notice: {cleanup_err}")

        logger.info(
            f"[Calibration] Face detection complete: {total_faces_found} faces found "
            f"across {len(frame_paths)} frames. "
            f"{frames_with_corner_face} frames had a corner-zone face."
        )

        # 4. Find dominant corner zone
        zone_counts = {z: len(dets) for z, dets in zone_detections.items()}
        best_zone = max(zone_counts, key=lambda z: zone_counts[z])
        best_count = zone_counts[best_zone]

        # Consistency check: best zone must appear in >= MIN_ZONE_CONSISTENCY of frames
        consistency = frames_with_corner_face / len(frame_paths) if frame_paths else 0
        zone_fraction = best_count / len(frame_paths) if frame_paths else 0

        logger.info(
            f"[Calibration] Zone counts: {zone_counts} | "
            f"Best zone: '{best_zone}' ({best_count} detections, {zone_fraction:.1%} of frames) | "
            f"Overall consistency: {consistency:.1%}"
        )

        if zone_fraction < MIN_ZONE_CONSISTENCY:
            logger.warning(
                f"[Calibration] Best zone '{best_zone}' only appeared in "
                f"{zone_fraction:.1%} of frames (min: {MIN_ZONE_CONSISTENCY:.0%}). "
                "Not confident enough — caching low-confidence failure marker. "
                "Video may not have a consistent facecam overlay."
            )
            # Bug 9 fix: Cache low-confidence calibration failure marker so subsequent jobs skip re-scanning
            presets = self.load_presets()
            presets[channel_id] = {
                "failed": True,
                "confidence": round(zone_fraction, 3),
                "calibrated_at": str(date.today()),
            }
            self.save_presets(presets)
            return None

        # 5. Compute stable region from best zone detections
        best_detections = zone_detections[best_zone]
        region = self._compute_region_from_detections(best_detections)
        confidence = min(1.0, zone_fraction * 1.5)  # Scale up slightly, cap at 1.0

        logger.info(
            f"[Calibration] ✅ Facecam detected in '{best_zone}' zone: "
            f"x={region['x']:.3f} y={region['y']:.3f} "
            f"w={region['w']:.3f} h={region['h']:.3f} "
            f"(confidence={confidence:.2f})"
        )

        # 6. Save to preset store
        self.save_channel_preset(channel_id, region, confidence)
        return region
