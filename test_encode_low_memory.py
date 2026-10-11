"""Unit tests for low-memory FFmpeg encode profile, fallback ladder, and validation."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root imports
os.environ.setdefault("FFMPEG_LOW_MEMORY", "1")
os.environ.setdefault("FFMPEG_ENCODE_TIER", "480p")

from config import (
    ENCODE_FALLBACK_LADDER,
    get_encode_crf,
    get_encode_fallback_ladder,
    get_encode_preset,
    get_ffmpeg_thread_count,
    resolve_encode_tier,
)
from clipper import FFmpegClipper, read_cgroup_memory


class TestEncodeConfig(unittest.TestCase):
    def test_low_mem_defaults(self):
        with patch.dict(os.environ, {"FFMPEG_LOW_MEMORY": "1", "FFMPEG_ENCODE_TIER": ""}, clear=False):
            # Clear override
            os.environ.pop("FFMPEG_ENCODE_TIER", None)
            self.assertEqual(resolve_encode_tier("1080p"), "480p")
            self.assertEqual(resolve_encode_tier("1440p"), "480p")
            self.assertEqual(get_ffmpeg_thread_count(), 1)
            self.assertEqual(get_encode_preset(), "ultrafast")
            self.assertEqual(get_encode_crf(), "28")

    def test_encode_tier_override(self):
        with patch.dict(os.environ, {"FFMPEG_ENCODE_TIER": "720p", "FFMPEG_LOW_MEMORY": "1"}):
            self.assertEqual(resolve_encode_tier("1080p"), "720p")
            self.assertEqual(get_encode_fallback_ladder("720p"), ["720p", "540p", "480p"])

    def test_ladder_from_480(self):
        self.assertEqual(get_encode_fallback_ladder("480p"), ["480p"])
        self.assertEqual(ENCODE_FALLBACK_LADDER, ("720p", "540p", "480p"))

    def test_ladder_from_1080_local(self):
        with patch.dict(os.environ, {"FFMPEG_LOW_MEMORY": "0", "FFMPEG_ENCODE_TIER": ""}, clear=False):
            os.environ.pop("FFMPEG_ENCODE_TIER", None)
            os.environ.pop("RAILWAY_ENVIRONMENT", None)
            os.environ.pop("RAILWAY_PROJECT_ID", None)
            # Force not low-mem
            with patch("config.is_low_memory_host", return_value=False):
                ladder = get_encode_fallback_ladder("1080p")
                self.assertEqual(ladder[0], "1080p")
                self.assertIn("480p", ladder)


class TestClipperValidationAndCleanup(unittest.TestCase):
    def setUp(self):
        self.clipper = FFmpegClipper()

    def test_is_oom_kill(self):
        self.assertTrue(self.clipper._is_oom_kill(-9))
        self.assertTrue(self.clipper._is_oom_kill(137))
        self.assertFalse(self.clipper._is_oom_kill(1))
        self.assertFalse(self.clipper._is_oom_kill(0))

    def test_unlink_partial(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "partial.mp4"
            p.write_bytes(b"x" * 100)
            self.clipper._unlink_partial(p)
            self.assertFalse(p.exists())

    def test_validate_missing_file(self):
        ok, msg = self.clipper._validate_output_clip(
            Path("definitely_missing_clip_xyz.mp4"),
            expected_min_duration=10.0,
            expected_w=480,
            expected_h=854,
        )
        self.assertFalse(ok)
        self.assertIn("missing", msg)

    def test_validate_too_small(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tiny.mp4"
            p.write_bytes(b"not-a-real-mp4")
            ok, msg = self.clipper._validate_output_clip(
                p, expected_min_duration=5.0, expected_w=480, expected_h=854
            )
            self.assertFalse(ok)
            self.assertIn("too small", msg)

    def test_run_ffmpeg_timeout_kills(self):
        with patch("clipper.subprocess.Popen") as popen:
            proc = MagicMock()
            proc.communicate.side_effect = [
                __import__("subprocess").TimeoutExpired(cmd="ffmpeg", timeout=1),
                ("", ""),
            ]
            popen.return_value = proc
            code, err = self.clipper._run_ffmpeg(
                ["ffmpeg", "-version"], timeout=0.01, label="test"
            )
            self.assertIsNone(code)
            self.assertIn("timeout", err)
            proc.kill.assert_called()


class TestLowMemEncodeLadder(unittest.TestCase):
    def test_sigkill_then_fallback_success(self):
        clipper = FFmpegClipper()
        calls = {"n": 0}

        def fake_encode(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return False, "failed code=-9"
            # Pretend success on second tier
            out = kwargs["out_filename"]
            out.write_bytes(b"0" * 60_000)
            return True, "CPU libx264 (ultrafast/540p)"

        with patch.dict(os.environ, {"FFMPEG_LOW_MEMORY": "1", "FFMPEG_ENCODE_TIER": "720p"}):
            with patch.object(clipper, "_encode_low_memory_letterbox", side_effect=fake_encode):
                with patch.object(
                    clipper,
                    "_validate_output_clip",
                    side_effect=[(True, "540x960, 5.0s, ok")],
                ):
                    with patch.object(clipper, "_get_video_dimensions", return_value=(1280, 720, "40")):
                        with patch("clipper.generate_ass_subtitles"):
                            with tempfile.TemporaryDirectory() as td:
                                # Point clips dir via patching CLIPS_DIR usage — write into job path
                                src = Path(td) / "src.mp4"
                                src.write_bytes(b"0" * 1000)
                                clips = [
                                    {"start_time": 1.0, "end_time": 6.0, "duration": 5.0, "hook_text": "x"},
                                ]
                                # Redirect output into temp via monkeypatch of CLIPS_DIR / TEMP_DIR
                                with patch("clipper.CLIPS_DIR", Path(td)):
                                    with patch("clipper.TEMP_DIR", Path(td)):
                                        out = clipper.cut_clips(
                                            src,
                                            clips,
                                            transcript=[],
                                            job_id="ladder_test",
                                            facecam_region={"x": 0, "y": 0, "w": 1, "h": 1},
                                            facecam_confirmed=True,
                                            resolution_tier="720p",
                                            flash_forward_hook=False,
                                        )
                                        self.assertEqual(len(out), 1)
                                        self.assertEqual(calls["n"], 2)

    def test_all_attempts_fail_returns_empty(self):
        clipper = FFmpegClipper()
        with patch.dict(os.environ, {"FFMPEG_LOW_MEMORY": "1", "FFMPEG_ENCODE_TIER": "480p"}):
            with patch.object(
                clipper,
                "_encode_low_memory_letterbox",
                return_value=(False, "failed code=-9"),
            ):
                with patch.object(clipper, "_get_video_dimensions", return_value=(1280, 720, "40")):
                    with patch("clipper.generate_ass_subtitles"):
                        with tempfile.TemporaryDirectory() as td:
                            src = Path(td) / "src.mp4"
                            src.write_bytes(b"0" * 1000)
                            with patch("clipper.CLIPS_DIR", Path(td)):
                                with patch("clipper.TEMP_DIR", Path(td)):
                                    out = clipper.cut_clips(
                                        src,
                                        [{"start_time": 0.0, "end_time": 5.0, "duration": 5.0}],
                                        job_id="fail_test",
                                        facecam_region={"x": 0, "y": 0, "w": 1, "h": 1},
                                        facecam_confirmed=True,
                                        resolution_tier="480p",
                                        flash_forward_hook=False,
                                    )
                                    self.assertEqual(out, [])


class TestSuccessfulRealEncode(unittest.TestCase):
    """Optional integration: real ffmpeg 480p letterbox if ffmpeg is available."""

    def test_real_480p_letterbox_short_clip(self):
        from utils import ensure_ffmpeg_on_path, resolve_ffmpeg_bin

        if not ensure_ffmpeg_on_path() or not resolve_ffmpeg_bin():
            self.skipTest("ffmpeg not available")

        import subprocess

        ffmpeg = resolve_ffmpeg_bin()
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = td_path / "src.mp4"
            # 2s 640x360 test pattern with silent audio
            gen = [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                str(src),
            ]
            r = subprocess.run(gen, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if r.returncode != 0:
                self.skipTest(f"could not generate test source: {r.stderr[-200:]}")

            # ASS paths must stay under project TEMP_DIR (relative to BASE_DIR) for FFmpeg on Windows
            with patch.dict(
                os.environ,
                {
                    "FFMPEG_LOW_MEMORY": "1",
                    "FFMPEG_ENCODE_TIER": "480p",
                    "FFMPEG_THREADS": "1",
                    "FFMPEG_PRESET": "ultrafast",
                    "FFMPEG_CRF": "28",
                },
            ):
                clipper = FFmpegClipper()
                outs = clipper.cut_clips(
                    src,
                    [{"start_time": 0.0, "end_time": 1.5, "duration": 1.5, "hook_text": "TEST"}],
                    transcript=[{"start": 0.0, "duration": 1.0, "text": "hello"}],
                    job_id="real480",
                    facecam_region={"x": 0, "y": 0, "w": 1, "h": 1},
                    facecam_confirmed=True,
                    resolution_tier="480p",
                    flash_forward_hook=False,
                )
            self.assertEqual(len(outs), 1, "expected one valid 480p clip")
            ok, detail = clipper._validate_output_clip(
                outs[0], expected_min_duration=1.5, expected_w=480, expected_h=854
            )
            self.assertTrue(ok, detail)
            # cleanup test artifact
            for p in outs:
                try:
                    p.unlink()
                except OSError:
                    pass


class TestCgroupHelper(unittest.TestCase):
    def test_read_cgroup_memory_no_crash(self):
        # On Windows / without cgroup this returns None
        result = read_cgroup_memory()
        self.assertTrue(result is None or isinstance(result, str))


if __name__ == "__main__":
    unittest.main()
