"""Timestamp refiner module with hook-aware smart buffering and story-arc duration boundaries."""

from typing import Any, Dict, List, Optional
from config import MAX_CLIP_DURATION_SEC, MIN_CLIP_DURATION_SEC
from utils import logger


# Hook / high-energy words — if the AI's start_time lands on one of these,
# trust it and do NOT shift the start earlier (the AI found the hook).
_HOOK_INDICATORS = {
    "what", "wait", "no", "noo", "nooo", "noooo", "yoo", "yooo", "yoooo",
    "omg", "holy", "bro", "bruh", "dude", "insane", "crazy", "so",
    "why", "how", "who", "when", "where", "did", "can", "should",
    "watch", "look", "listen", "imagine", "okay", "right",
    "literally", "actually", "honestly", "seriously",
    "!", "?", "clip", "clutch",
}


class TimestampRefiner:
    """Refines AI candidate clip boundaries with hook-aware smart buffering."""

    def __init__(self, settings: Optional[Dict[str, Any]] = None, transcript: Optional[List[Dict[str, Any]]] = None):
        self.settings = settings or {}
        self.transcript = transcript or []
        self.min_clip_dur = float(self.settings.get("min_clip_duration_sec", MIN_CLIP_DURATION_SEC))
        self.max_clip_dur = float(self.settings.get("max_clip_duration_sec", MAX_CLIP_DURATION_SEC))

    def _is_hook_at_time(self, t: float, tolerance: float = 2.5) -> bool:
        """Check if there's a hook/high-energy word in the transcript within ±tolerance of time t."""
        for item in self.transcript:
            item_start = float(item.get("start", 0))
            if abs(item_start - t) <= tolerance:
                text_raw = item.get("text", "")
                text_lower = text_raw.lower()
                # Use word-level check to avoid false positives:
                # substring check would match "so" in "sound", "no" in "know", "can" in "cancel"
                words_lower = set(text_lower.split())
                if any(w in words_lower for w in _HOOK_INDICATORS):
                    return True
                # ALL-CAPS words or exclamation/question marks are hooks
                if "!" in text_raw or "?" in text_raw:
                    return True
                words_raw = text_raw.split()
                if any(w.isupper() and len(w) > 1 for w in words_raw):
                    return True
        return False

    def _has_speech_at_time(self, t: float, tolerance: float = 2.0) -> bool:
        """Check if there's any transcript text within ±tolerance of time t."""
        for item in self.transcript:
            item_start = float(item.get("start", 0))
            if abs(item_start - t) <= tolerance:
                if item.get("text", "").strip():
                    return True
        return False

    def refine_clips(self, raw_clips: List[Dict[str, Any]], video_duration: float) -> List[Dict[str, Any]]:
        """Refine start and end timestamps with hook-aware smart buffer."""
        refined_clips = []

        for idx, clip in enumerate(raw_clips):
            start_t = float(clip["start_time"])
            end_t = float(clip["end_time"])

            # Smart hook-aware 2-second pre-hook buffer:
            # The AI/heuristic is now instructed to place start_time 2s BEFORE the best hook line.
            # - If AI placed start_time near a hook word → trust exact placement (0.0s extra margin)
            # - If AI placed start_time on speech but no hook marker → add 2.0s to back up before speech start
            # - If AI placed start_time on dead air → add 2.5s to ensure speech begins by ~0.5s into clip
            if self._is_hook_at_time(start_t, tolerance=2.5):
                buffer = 0.0
                buffer_type = "hook-locked (2s pre-hook trusted)"
            elif self._has_speech_at_time(start_t, tolerance=2.0):
                buffer = 2.0
                buffer_type = "speech-2s-prepull"
            else:
                buffer = 2.5
                buffer_type = "dead-air-catch"

            original_start = start_t
            start_t = max(0.0, start_t - buffer)
            duration = end_t - start_t

            is_clamped = False
            # Clamp duration into [min_clip_dur, max_clip_dur] window if needed
            if duration < self.min_clip_dur:
                end_t = min(video_duration, start_t + self.min_clip_dur)
                is_clamped = True
            elif duration > self.max_clip_dur:
                # Bug 15 fix: Anchor clamp on end_t to preserve the payoff/climax at the end of the AI window
                start_t = max(0.0, end_t - self.max_clip_dur)
                is_clamped = True

            if end_t > video_duration:
                desired_duration = end_t - start_t
                end_t = video_duration
                start_t = max(0.0, end_t - desired_duration)

            refined_duration = round(end_t - start_t, 2)
            if refined_duration <= 0:
                logger.warning(f"Skipping invalid zero-length refined clip: start={start_t}, end={end_t}")
                continue

            dur_type_str = "Clamped/Heuristic" if is_clamped else "AI Story-Determined"
            logger.info(
                f"Clip {idx+1} refined: {start_t:.1f}s -> {end_t:.1f}s "
                f"(Duration: {refined_duration}s, Type: {dur_type_str}, "
                f"Buffer: {buffer}s [{buffer_type}], AI start was {original_start:.1f}s)"
            )

            # Adjust peak_moment_offset if provided by AI to reflect any shift in start_t
            raw_peak_offset = clip.get("peak_moment_offset")
            adjusted_peak_offset = None
            if raw_peak_offset is not None:
                try:
                    abs_peak = original_start + float(raw_peak_offset)
                    adjusted_peak_offset = round(max(0.0, min(abs_peak - start_t, refined_duration - 1.0)), 2)
                except (ValueError, TypeError):
                    adjusted_peak_offset = None

            refined_clips.append({
                "clip_index": idx + 1,
                "start_time": round(start_t, 2),
                "end_time": round(end_t, 2),
                "duration": refined_duration,
                "peak_moment_offset": adjusted_peak_offset,
                "confidence_score": clip.get("confidence_score", 0.85),
                "hook_strength": clip.get("hook_strength", 0.85),
                "emotional_intensity": clip.get("emotional_intensity", 0.80),
                "unexpectedness": clip.get("unexpectedness", 0.75),
                "payoff_quality": clip.get("payoff_quality", 0.85),
                "hook_text": clip.get("hook_text", "Watch this moment!"),
                "explanation": clip.get("explanation", "Viral segment."),
                "reasoning": clip.get("reasoning", ""),
                "caption": clip.get("caption", ""),
                "tags": clip.get("tags", "#shorts #viral"),
            })

        return refined_clips
