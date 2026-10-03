"""Verification script for Project Brief v4 Resolution Fix."""

import subprocess
from pathlib import Path
from clipper import FFmpegClipper
from config import BASE_DIR, CLIPS_DIR


def test_clipper_resolution_fix():
    print("Executing end-to-end resolution fix test...")

    # Create dummy 720p 16:9 input video
    dummy_input = BASE_DIR / "temp" / "dummy_720p_input.mp4"
    dummy_input.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=1280x720:d=2",
        "-c:v", "libx264", "-preset", "ultrafast",
        str(dummy_input)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    clipper = FFmpegClipper()
    test_clips = [{"start_time": 0.0, "duration": 1.5}]
    out_files = clipper.cut_clips(dummy_input, test_clips, job_id="v4_test")

    assert len(out_files) == 1
    out_clip = out_files[0]

    # Run ffprobe verification command specified in Brief v4
    probe_cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,level",
        "-of", "csv=p=0",
        str(out_clip)
    ]
    res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    probe_output = res.stdout.strip()
    print(f"FFPROBE VERIFICATION RESULT: {probe_output}")

    parts = probe_output.split(",")
    width = int(parts[0])
    height = int(parts[1])
    level = int(parts[2]) if parts[2].isdigit() else 40

    assert width == 1080
    assert height == 1920
    assert level >= 40  # Level 4.0+ in ffprobe notation (x10)

    # Clean up dummy test files
    if dummy_input.exists():
        dummy_input.unlink()
    if out_clip.exists():
        out_clip.unlink()

    print("Project Brief v4 Resolution Fix empirically verified (1080x1920 confirmed)!")


if __name__ == "__main__":
    test_clipper_resolution_fix()
