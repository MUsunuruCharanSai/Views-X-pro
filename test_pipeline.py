"""Unit & Integration Tests for AI Viral Shorts Clipper Improvements."""

import os
import pytest
from pathlib import Path
from analyzer import VideoAnalyzer
from config import MIN_CLIP_SPACING_SEC, MIN_CONFIDENCE_THRESHOLD


def test_confidence_and_spacing_filters():
    analyzer = VideoAnalyzer(api_key="test_key")

    raw_candidates = [
        # Candidate 1: High confidence at 100s
        {"start_time": 100.0, "end_time": 150.0, "confidence_score": 0.95, "explanation": "Top hook"},
        # Candidate 2: High confidence at 150s (only 50s after #1 -> should be dropped due to < 180s spacing)
        {"start_time": 150.0, "end_time": 200.0, "confidence_score": 0.90, "explanation": "Close hook"},
        # Candidate 3: Low confidence at 500s (score 0.40 < 0.60 -> should be dropped due to low confidence)
        {"start_time": 500.0, "end_time": 550.0, "confidence_score": 0.40, "explanation": "Weak hook"},
        # Candidate 4: Good confidence at 600s (450s after #1 -> accepted)
        {"start_time": 600.0, "end_time": 650.0, "confidence_score": 0.88, "explanation": "Second good hook"},
        # Candidate 5: Good confidence at 1000s (400s after #4 -> accepted)
        {"start_time": 1000.0, "end_time": 1050.0, "confidence_score": 0.82, "explanation": "Third good hook"},
    ]

    filtered = analyzer.filter_and_rank_candidates(raw_candidates)

    print(f"Filtered clips count: {len(filtered)}")
    for c in filtered:
        print(f"  - Start: {c['start_time']}s, Score: {c['confidence_score']}")

    # Assertions
    assert len(filtered) == 3
    assert filtered[0]["start_time"] == 100.0
    assert filtered[1]["start_time"] == 600.0
    assert filtered[2]["start_time"] == 1000.0

    # Ensure no two accepted clips are within MIN_CLIP_SPACING_SEC
    for i in range(len(filtered)):
        for j in range(i + 1, len(filtered)):
            diff = abs(filtered[i]["start_time"] - filtered[j]["start_time"])
            assert diff >= MIN_CLIP_SPACING_SEC


def test_empty_candidates_on_low_confidence():
    analyzer = VideoAnalyzer(api_key="test_key")

    raw_candidates = [
        {"start_time": 100.0, "end_time": 150.0, "confidence_score": 0.35},
        {"start_time": 400.0, "end_time": 450.0, "confidence_score": 0.50},
    ]

    filtered = analyzer.filter_and_rank_candidates(raw_candidates)
    assert len(filtered) == 0


if __name__ == "__main__":
    test_confidence_and_spacing_filters()
    test_empty_candidates_on_low_confidence()
    print("All unit tests passed successfully!")
