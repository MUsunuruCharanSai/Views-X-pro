"""Comprehensive QA Test Script for Project Brief v7: Caption Overlap Bug Fix."""

import os
import re
import subprocess
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer import VideoAnalyzer
from clipper import FFmpegClipper, generate_ass_subtitles
from config import BASE_DIR, TEMP_DIR
from downloader import YouTubeDownloader
from utils import logger


def parse_ass_dialogues(ass_path: Path):
    """Parse dialogue entries from generated .ass subtitle file."""
    text_content = ass_path.read_text(encoding="utf-8")
    lines = text_content.splitlines()
    dialogues = []

    for line in lines:
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) >= 10:
                style = parts[3].strip()
                start_str = parts[1].strip()
                end_str = parts[2].strip()
                text = parts[9].strip()

                # Helper to convert h:mm:ss.cs into float seconds
                def parse_time(ts):
                    h, m, s_cs = ts.split(":")
                    s, cs = s_cs.split(".")
                    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0

                dialogues.append({
                    "style": style,
                    "start": parse_time(start_str),
                    "end": parse_time(end_str),
                    "text": text
                })

    return dialogues


def test_v7_caption_fix():
    print("==================================================================")
    print("       PROJECT BRIEF V7 — CAPTION OVERLAP BUG FIX QA TEST        ")
    print("==================================================================")

    # ------------------------------------------------------------------
    # Test 1: Programmatic ASS Subtitle Non-Overlap Verification
    # ------------------------------------------------------------------
    print("\n--- Test 1: Programmatic ASS Timing & Non-Overlap Check ---")
    mock_overlapping_transcript = [
        {"start": 10.0, "duration": 4.0, "text": "looking at? What? They all decided to"},
        {"start": 11.5, "duration": 4.0, "text": "looking at? What the [ __ ] are you"},
        {"start": 13.0, "duration": 4.0, "text": "doing here today?"},
        {"start": 15.0, "duration": 3.0, "text": "This is incredible content!"}
    ]

    mock_clip = {
        "clip_index": 1,
        "start_time": 10.0,
        "end_time": 25.0,
        "duration": 15.0,
        "hook_text": "DON'T LOOK AWAY!",
        "confidence_score": 0.9
    }

    test_ass_file = TEMP_DIR / "test_v7_timing.ass"
    generate_ass_subtitles(mock_clip, mock_overlapping_transcript, test_ass_file)

    assert test_ass_file.exists(), "ASS file failed to generate."
    dialogues = parse_ass_dialogues(test_ass_file)
    print(f"Generated {len(dialogues)} dialogue entries in .ass file.")

    # Separate Hook text and Caption dialogues
    hook_entries = [d for d in dialogues if d["style"] == "HookStyle"]
    caption_entries = [d for d in dialogues if d["style"] == "CaptionStyle"]

    assert len(hook_entries) == 1, "Expected 1 HookStyle entry"
    assert len(caption_entries) == 4, f"Expected 4 CaptionStyle entries, got {len(caption_entries)}"

    print("\nParsed Caption Entries:")
    for idx, c in enumerate(caption_entries):
        print(f"  Caption #{idx+1}: [{c['start']:.2f}s - {c['end']:.2f}s] Text: '{c['text']}'")

    # Programmatic assertion: Verify NO two caption entries overlap in time window!
    for i in range(len(caption_entries) - 1):
        curr = caption_entries[i]
        nxt = caption_entries[i + 1]
        assert curr["end"] <= nxt["start"], (
            f"OVERLAP BUG DETECTED! Caption #{i+1} end ({curr['end']}s) "
            f"exceeds Caption #{i+2} start ({nxt['start']}s)"
        )

    print("[PASS] Test 1 Passed: Zero caption timing overlaps verified by construction.")

    # ------------------------------------------------------------------
    # Test 2: Real Video Download, Subtitle Burn, and Frame Extraction
    # ------------------------------------------------------------------
    print("\n--- Test 2: Real Video Download & Frame Extraction QA ---")
    test_url = "https://www.youtube.com/watch?v=lK5FNZUJeBo"
    print(f"Downloading real video: {test_url}...")
    downloader = YouTubeDownloader()
    video_path, audio_path, title = downloader.download(test_url)

    analyzer = VideoAnalyzer()
    transcript = analyzer.fetch_transcript(test_url)
    assert transcript, "Failed to fetch real transcript."
    print(f"Fetched real transcript ({len(transcript)} entries).")

    real_clip_record = {
        "clip_index": 1,
        "start_time": 20.0,
        "end_time": 50.0,
        "duration": 30.0,
        "hook_text": "THIS IS INSANE!",
        "confidence_score": 0.88
    }

    clipper = FFmpegClipper()
    clips = clipper.cut_clips(video_path, [real_clip_record], transcript=transcript, job_id="v7_qa")
    assert len(clips) == 1, "Failed to render real test clip."
    out_clip = clips[0]
    assert out_clip.exists(), "Rendered real test clip missing."

    w, h, lvl = clipper._get_video_dimensions(out_clip)
    print(f"Rendered clip: {out_clip.name} ({w}x{h}, H.264 level {lvl})")

    # Extract 5 sample frames at 10%, 25%, 50%, 75%, 90% of clip duration
    frame_dir = TEMP_DIR / "v7_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_timestamps = [3.0, 7.5, 15.0, 22.5, 27.0]

    for idx, ts in enumerate(frame_timestamps):
        frame_out = frame_dir / f"frame_{idx+1}_{ts}s.jpg"
        cmd = [
            "ffmpeg", "-y", "-ss", str(ts),
            "-i", str(out_clip),
            "-vframes", "1", "-q:v", "2",
            str(frame_out)
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert frame_out.exists(), f"Frame extraction failed for timestamp {ts}s"
        print(f"  Extracted frame {idx+1}/{len(frame_timestamps)} at {ts}s -> {frame_out.name}")

    # Cleanup temporary test files
    if out_clip.exists():
        out_clip.unlink()
    if video_path.exists():
        video_path.unlink()
    if audio_path.exists():
        audio_path.unlink()
    if test_ass_file.exists():
        test_ass_file.unlink()

    print("\n==================================================================")
    print("      ALL BRIEF V7 CAPTION OVERLAP FIX TESTS PASSED 100%!       ")
    print("==================================================================")


if __name__ == "__main__":
    test_v7_caption_fix()
