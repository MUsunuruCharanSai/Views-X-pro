"""MediaPipe Tasks Face Detector Helper with auto-download and fallback.

Provides a unified interface for face detection across facecam_calibration.py
and facecam_detector.py using MediaPipe Tasks API (compatible with MediaPipe 1.0+).
"""

import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

from config import BASE_DIR
from utils import logger

MODEL_DIR = BASE_DIR / "models"
MODEL_PATH = MODEL_DIR / "blaze_face_full_range.tflite"
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite"


def ensure_model_file() -> Path:
    """Ensure the MediaPipe BlazeFace model file exists, auto-downloading if missing."""
    if not MODEL_PATH.exists():
        logger.info(f"[FaceDetector] Downloading face detector model to {MODEL_PATH.name}...")
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(MODEL_URL, str(MODEL_PATH))
            logger.info("[FaceDetector] Model download complete.")
        except Exception as e:
            logger.error(f"[FaceDetector] Failed to download model from {MODEL_URL}: {e}")
            raise RuntimeError(f"Could not obtain face detector model file: {e}")
    return MODEL_PATH


def create_face_detector(min_confidence: float = 0.4) -> Any:
    """Initialize and return a MediaPipe Tasks FaceDetector instance."""
    model_file = ensure_model_file()
    base_options = mp_tasks.BaseOptions(model_asset_path=str(model_file))
    options = vision.FaceDetectorOptions(
        base_options=base_options,
        min_detection_confidence=min_confidence
    )
    return vision.FaceDetector.create_from_options(options)


def detect_faces_in_bgr_frame(
    frame_bgr: Any, detector: Any
) -> List[Dict[str, float]]:
    """Run face detection on an OpenCV BGR image frame.

    Returns list of normalized dicts:
    [{"x": norm_x, "y": norm_y, "w": norm_w, "h": norm_h, "cx": cx, "cy": cy, "score": score}]
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return []

    h_img, w_img = frame_bgr.shape[:2]
    if h_img <= 0 or w_img <= 0:
        return []

    rgb_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

    try:
        results = detector.detect(mp_image)
    except Exception as e:
        logger.warning(f"[FaceDetector] Detection failed ({e}).")
        return []

    faces = []
    if results and results.detections:
        for det in results.detections:
            bbox = det.bounding_box
            nx = max(0.0, min(1.0, bbox.origin_x / w_img))
            ny = max(0.0, min(1.0, bbox.origin_y / h_img))
            nw = max(0.0, min(1.0, bbox.width / w_img))
            nh = max(0.0, min(1.0, bbox.height / h_img))
            cx = nx + (nw / 2.0)
            cy = ny + (nh / 2.0)
            score = det.categories[0].score if det.categories else 0.5

            faces.append({
                "x": nx,
                "y": ny,
                "w": nw,
                "h": nh,
                "cx": cx,
                "cy": cy,
                "score": score
            })

    return faces
