"""Unit & Integration Tests for Project Brief v3 Features."""

import json
import os
import pytest
from pathlib import Path

from history_store import (
    add_job_to_history,
    delete_history_entry,
    load_history,
    prune_missing_files,
)
from settings_store import DEFAULT_SETTINGS, load_settings, save_settings, validate_settings


def test_settings_validation_and_persistence():
    print("Testing settings loading and validation...")
    settings = load_settings()
    assert "target_clip_count" in settings

    # Valid save
    updated = save_settings({"target_clip_count": 6, "min_clip_spacing_seconds": 240})
    assert updated["target_clip_count"] == 6
    assert updated["min_clip_spacing_seconds"] == 240

    # Test invalid target count (< 1)
    invalid_count = dict(DEFAULT_SETTINGS)
    invalid_count["target_clip_count"] = 0
    with pytest.raises(ValueError, match="between 1 and 10"):
        validate_settings(invalid_count)

    # Test invalid min/max duration (min >= max)
    invalid_dur = dict(DEFAULT_SETTINGS)
    invalid_dur["min_clip_duration_sec"] = 60.0
    invalid_dur["max_clip_duration_sec"] = 45.0
    with pytest.raises(ValueError, match="strictly less than"):
        validate_settings(invalid_dur)

    print("Settings store tests passed successfully!")


def test_history_store():
    print("Testing job history store...")
    test_record = {
        "job_id": "test_v3_01",
        "youtube_url": "https://youtu.be/test",
        "video_title": "Test Video Title",
        "timestamp": "2026-08-09T18:00:00Z",
        "status": "completed",
        "analysis_method": "gemini_transcript",
        "clips": [{"name": "test_v3_01_Clip_01.mp4", "url": "/clips/test_v3_01_Clip_01.mp4", "size_mb": 5.0}]
    }

    add_job_to_history(test_record)
    history = load_history()
    assert len(history) > 0
    assert history[0]["job_id"] == "test_v3_01"

    # Delete history entry
    deleted = delete_history_entry("test_v3_01")
    assert deleted is True
    history_after = load_history()
    assert not any(r["job_id"] == "test_v3_01" for r in history_after)

    print("Job history store tests passed successfully!")


if __name__ == "__main__":
    test_settings_validation_and_persistence()
    test_history_store()
    print("All Project Brief v3 tests passed!")
