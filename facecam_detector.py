"""Facecam Detector — Smart panning face-centered 9:16 crop for IRL mode.

Provides detect_speaker_crop_timeline() which samples the video segment at regular
intervals and builds a stabilised list of (relative_time, crop_x) keyframes used by
clipper.py to produce a smooth panning crop that follows the primary speaker.
"""

from pathlib import Path
from typing import Dict, List, Tuple

import cv2

from face_detector_helper import create_face_detector, detect_faces_in_bgr_frame
from utils import logger


class FacecamDetector:
    """Performs face detection within a user-selected region and computes smart
    face-centered horizontal crop offsets using MediaPipe Face Detection."""

    # Bug 3 fix: Restored confirm_facecam_region using MediaPipe FaceDetection helper
    def confirm_facecam_region(
        self, video_path: Path, region: Dict[str, float], sample_frames: int = 5
    ) -> bool:
        """Sample frames across video and confirm whether a face is detected inside the specified normalized region."""
        if not region or not isinstance(region, dict):
            return False

        rx, ry, rw, rh = region.get("x", 0), region.get("y", 0), region.get("w", 1), region.get("h", 1)
        # If full-frame or invalid box, treat as standard/unconfirmed
        if rw >= 0.98 and rh >= 0.98:
            return True

        try:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                logger.warning(f"Could not open video for facecam confirmation: {video_path.name}")
                return False

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total_frames <= 0:
                cap.release()
                return False

            step = max(1, total_frames // (sample_frames + 1))
            sample_indices = [step * (i + 1) for i in range(sample_frames)]

            detector = create_face_detector(min_confidence=0.40)
            hits = 0
            read_count = 0

            try:
                for idx in sample_indices:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        continue

                    read_count += 1
                    fh, fw = frame.shape[:2]
                    x1 = max(0, min(int(rx * fw), fw - 1))
                    y1 = max(0, min(int(ry * fh), fh - 1))
                    x2 = max(x1 + 1, min(int((rx + rw) * fw), fw))
                    y2 = max(y1 + 1, min(int((ry + rh) * fh), fh))

                    crop = frame[y1:y2, x1:x2]
                    if crop.size == 0 or crop.shape[0] < 10 or crop.shape[1] < 10:
                        continue

                    faces = detect_faces_in_bgr_frame(crop, detector)
                    if faces:
                        hits += 1
            finally:
                detector.close()
                cap.release()

            if read_count == 0:
                return False

            consistency = hits / read_count
            logger.info(
                f"[Facecam Confirmation] MediaPipe scanned {read_count} frames in region "
                f"({rx:.2f}, {ry:.2f}, {rw:.2f}, {rh:.2f}) -> {hits}/{read_count} faces detected ({consistency * 100:.0f}%)"
            )
            return consistency >= 0.40
        except Exception as e:
            logger.warning(f"Facecam confirmation error ({e}); returning False.")
            return False


    def detect_speaker_crop_timeline(
        self, video_path: Path, start_t: float, duration: float,
        src_w: int, src_h: int, sample_interval_sec: float = 3.0
    ) -> List[Tuple[float, int]]:
        """Like detect_speaker_crop_x, but returns multiple (relative_time, x)
        keyframes across the clip instead of one static value, so the crop
        can pan to follow the speaker.

        Returns a list of (t_relative_to_clip_start, crop_x) tuples, always
        including t=0.0 and t=duration as endpoints.
        """
        crop_w = int(src_h * (9.0 / 16.0))
        default_x = max(0, (src_w - crop_w) // 2)

        try:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                logger.warning(f"Could not open video for panning crop: {video_path.name}")
                return [(0.0, default_x), (duration, default_x)]

            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 999999

            start_frame = int(start_t * fps)
            end_frame = int(min(total_frames, (start_t + duration) * fps))
            if end_frame <= start_frame:
                cap.release()
                return [(0.0, default_x), (duration, default_x)]

            step_frames = max(1, int(fps * sample_interval_sec))
            sample_frames = list(range(start_frame, end_frame, step_frames))
            if sample_frames[-1] != end_frame - 1:
                sample_frames.append(end_frame - 1)

            detector = create_face_detector(min_confidence=0.45)
            keyframes: List[Tuple[float, int]] = []

            # Performance Fix: Single initial seek, then cap.grab() forward step instead of expensive cap.set() per frame.
            # Downscale sampled frame to 480px width for 5x faster MediaPipe face detection with identical normalized math.
            target_proc_w = 480
            proc_scale = target_proc_w / float(src_w) if src_w > 480 else 1.0
            target_proc_h = int(src_h * proc_scale)

            cap.set(cv2.CAP_PROP_POS_FRAMES, sample_frames[0])
            curr_pos = sample_frames[0]

            try:
                for f_idx in sample_frames:
                    while curr_pos < f_idx:
                        cap.grab()
                        curr_pos += 1
                    ret, frame = cap.read()
                    curr_pos += 1

                    if not ret or frame is None:
                        continue

                    if proc_scale < 1.0:
                        frame = cv2.resize(frame, (target_proc_w, target_proc_h), interpolation=cv2.INTER_NEAREST)

                    faces = detect_faces_in_bgr_frame(frame, detector)
                    candidates = [f for f in faces if (f["cy"] * src_h) < src_h * 0.75]

                    rel_t = (f_idx - start_frame) / fps
                    if candidates:
                        # Sort candidates by bounding box area descending
                        sorted_faces = sorted(candidates, key=lambda f: f["w"] * f["h"], reverse=True)
                        primary = sorted_faces[0]
                        x_center = primary["cx"] * src_w

                        # Dual-speaker midpoint centering: if 2 major faces are present
                        # (secondary area >= 45% of primary) and fit inside 85% of crop width,
                        # frame the midpoint between both speakers
                        if len(sorted_faces) >= 2:
                            sec = sorted_faces[1]
                            pri_area = primary["w"] * primary["h"]
                            sec_area = sec["w"] * sec["h"]
                            if sec_area >= 0.45 * pri_area:
                                p_cx = primary["cx"] * src_w
                                s_cx = sec["cx"] * src_w
                                dist = abs(p_cx - s_cx)
                                if dist <= crop_w * 0.85:
                                    x_center = (p_cx + s_cx) / 2.0

                        smart_x = int(x_center - (crop_w / 2.0))
                        clamped_x = max(0, min(smart_x, src_w - crop_w))
                        keyframes.append((round(rel_t, 2), clamped_x))
            finally:
                detector.close()

            cap.release()

            if not keyframes:
                logger.info(
                    f"No face detected across pan samples ({start_t:.1f}s-"
                    f"{start_t + duration:.1f}s). Using static center crop X={default_x}"
                )
                return [(0.0, default_x), (duration, default_x)]

            # Dead-zone hysteresis filter (deadzone = 8% of crop_w, ~48px on 1080p).
            # Prevents micro-wobbles when a seated speaker naturally sways or tilts head.
            # Clamp maximum movement between consecutive keyframes to 25% crop_w.
            dead_zone = int(crop_w * 0.08)
            max_step = int(crop_w * 0.25)
            smoothed: List[Tuple[float, int]] = [keyframes[0]]
            for t, x in keyframes[1:]:
                prev_x = smoothed[-1][1]
                diff = x - prev_x
                if abs(diff) <= dead_zone:
                    # Inside dead-zone -> keep camera locked on prev_x
                    target_x = prev_x
                else:
                    # Speaker shifted position -> smooth pan towards target x with clamped velocity
                    delta = max(-max_step, min(max_step, diff))
                    target_x = prev_x + delta
                smoothed.append((t, target_x))

            # Ensure first/last keyframes bound the full clip duration
            if smoothed[0][0] > 0.0:
                smoothed.insert(0, (0.0, smoothed[0][1]))
            if smoothed[-1][0] < duration:
                smoothed.append((duration, smoothed[-1][1]))

            logger.info(
                f"[Pan] Built {len(smoothed)} stabilized crop keyframes over {duration:.1f}s "
                f"(range X={min(x for _, x in smoothed)}-{max(x for _, x in smoothed)})"
            )
            return smoothed

        except Exception as e:
            logger.warning(f"Panning crop detection exception ({e}); using static center crop.")
            return [(0.0, default_x), (duration, default_x)]
