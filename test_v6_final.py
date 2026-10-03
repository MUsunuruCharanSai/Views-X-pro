"""Comprehensive QA & Validation Script for Project Brief v6 (FINAL)."""

import json
import os
import subprocess
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer import VideoAnalyzer, compute_derived_score
from clipper import FFmpegClipper, generate_ass_subtitles
from config import BASE_DIR, CLIPS_DIR, TEMP_DIR
from downloader import YouTubeDownloader
from settings_store import load_settings, save_settings
from timestamp_refiner import TimestampRefiner
from utils import logger


def run_v6_tests():
    print("==================================================================")
    print("      PROJECT BRIEF V6 (FINAL) — COMPREHENSIVE QA TEST SUITE      ")
    print("==================================================================")

    # ------------------------------------------------------------------
    # Test 1: Verify Settings Store Provider & Duration Bounds
    # ------------------------------------------------------------------
    print("\n--- Test 1: Settings Store Provider & Duration Bounds ---")
    initial_settings = load_settings()
    save_settings({
        "llm_provider": "gemini",
        "min_clip_duration_sec": 15.0,
        "max_clip_duration_sec": 90.0,
        "target_clip_count": 3
    })
    saved = load_settings()
    assert saved["llm_provider"] == "gemini", "Failed to persist llm_provider"
    assert saved["min_clip_duration_sec"] == 15.0, "Failed min_clip_duration_sec"
    assert saved["max_clip_duration_sec"] == 90.0, "Failed max_clip_duration_sec"
    print("[PASS] Test 1 Passed: Settings store successfully persists provider and 15s-90s bounds.")

    # ------------------------------------------------------------------
    # Test 2: Derived Score Calculation & Multi-Factor Scoring
    # ------------------------------------------------------------------
    print("\n--- Test 2: Derived Score & Multi-Factor Scoring ---")
    mock_clip = {
        "start_time": 10.0,
        "end_time": 40.0,
        "hook_strength": 0.90,       # 0.30 * 0.90 = 0.270
        "emotional_intensity": 0.80, # 0.30 * 0.80 = 0.240
        "unexpectedness": 0.70,      # 0.20 * 0.70 = 0.140
        "payoff_quality": 0.85,      # 0.20 * 0.85 = 0.170
        "hook_text": "THIS IS UNBELIEVABLE!",
        "explanation": "Test clip"
    }
    expected_score = round(0.270 + 0.240 + 0.140 + 0.170, 3) # 0.820
    derived = compute_derived_score(mock_clip)
    assert derived == expected_score, f"Derived score calculation error: expected {expected_score}, got {derived}"
    print(f"[PASS] Test 2 Passed: Multi-factor derived score calculated correctly ({derived}).")

    # ------------------------------------------------------------------
    # Test 3: Timestamp Refinement Clamping & Story-Arc Preservation
    # ------------------------------------------------------------------
    print("\n--- Test 3: Story-Aware Timestamp Refinement ---")
    refiner = TimestampRefiner(settings=saved)
    raw_candidates = [
        {
            "start_time": 10.0,
            "end_time": 20.0,  # 10s duration -> should clamp to min 15s
            "hook_strength": 0.8,
            "emotional_intensity": 0.8,
            "unexpectedness": 0.8,
            "payoff_quality": 0.8,
            "hook_text": "HOOK ONE",
            "explanation": "Short candidate"
        },
        {
            "start_time": 100.0,
            "end_time": 210.0, # 110s duration -> should clamp to max 90s
            "hook_strength": 0.8,
            "emotional_intensity": 0.8,
            "unexpectedness": 0.8,
            "payoff_quality": 0.8,
            "hook_text": "HOOK TWO",
            "explanation": "Long candidate"
        }
    ]
    refined = refiner.refine_clips(raw_candidates, video_duration=300.0)
    assert len(refined) == 2, "Refined clips count mismatch"
    assert refined[0]["duration"] >= 15.0, f"Min duration violation: {refined[0]['duration']}s < 15.0s"
    assert refined[1]["duration"] <= 90.0, f"Max duration violation: {refined[1]['duration']}s > 90.0s"
    assert "hook_text" in refined[0], "Hook text lost during refinement"
    print(f"[PASS] Test 3 Passed: Clamped short clip to {refined[0]['duration']}s and long clip to {refined[1]['duration']}s.")

    # ------------------------------------------------------------------
    # Test 4: OpenRouter Free Models Auto-Discovery
    # ------------------------------------------------------------------
    print("\n--- Test 4: OpenRouter Free Model Discovery ---")
    analyzer = VideoAnalyzer(settings={"llm_provider": "openrouter", "openrouter_api_key": "test_dummy_key"})
    chosen_model = analyzer._get_free_openrouter_model()
    assert chosen_model.endswith(":free") or "free" in chosen_model.lower(), f"Non-free model returned: {chosen_model}"
    print(f"[PASS] Test 4 Passed: OpenRouter free model discovery returned: {chosen_model}")

    # ------------------------------------------------------------------
    # Test 5: End-to-End Download, ASS Subtitle Generation, and 1080p Clip Render
    # ------------------------------------------------------------------
    print("\n--- Test 5: E2E Download & 1080p Burned Subtitles Render ---")
    test_url = "https://www.youtube.com/watch?v=lK5FNZUJeBo"
    print(f"Downloading video from {test_url}...")
    downloader = YouTubeDownloader()
    video_path, audio_path, title = downloader.download(test_url)
    assert video_path.exists(), "Downloaded video file does not exist."
    print(f"Video downloaded: {video_path.name}")

    # Probe downloaded video resolution
    dur_cmd = [
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
        str(video_path)
    ]
    probe_res = subprocess.run(dur_cmd, stdout=subprocess.PIPE, text=True, check=True)
    src_w, src_h = map(int, probe_res.stdout.strip().split(","))
    print(f"Downloaded source video resolution: {src_w}x{src_h}")

    # Render a test clip with burned-in ASS subtitles
    test_clip_record = {
        "clip_index": 1,
        "start_time": 10.0,
        "end_time": 25.0,
        "duration": 15.0,
        "confidence_score": 0.85,
        "hook_text": "DON'T MISS THIS MOMENT!",
        "explanation": "E2E Test Clip"
    }
    mock_transcript = [
        {"start": 10.5, "duration": 2.0, "text": "Welcome everyone to the stream!"},
        {"start": 13.0, "duration": 3.0, "text": "This is AMAZING video content!"},
        {"start": 17.0, "duration": 4.0, "text": "Make sure to subscribe for more clips!"}
    ]

    clipper = FFmpegClipper()
    clips = clipper.cut_clips(video_path, [test_clip_record], transcript=mock_transcript, job_id="qa_v6")
    assert len(clips) == 1, "Failed to render clip"
    clip_file = clips[0]
    assert clip_file.exists(), f"Rendered clip file {clip_file} missing"

    w, h, lvl = clipper._get_video_dimensions(clip_file)
    print(f"Rendered Shorts clip probe: {w}x{h}, H.264 level {lvl}")
    assert w == 1080 and h == 1920, f"Expected 1080x1920, got {w}x{h}"

    # Clean up test output clip
    if clip_file.exists():
        clip_file.unlink()
    if video_path.exists():
        video_path.unlink()
    if audio_path.exists():
        audio_path.unlink()

    print("\n==================================================================")
    print("      ALL BRIEF V6 (FINAL) QA & VALIDATION TESTS PASSED!        ")
    print("==================================================================")


if __name__ == "__main__":
    run_v6_tests()
