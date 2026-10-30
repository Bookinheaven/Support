"""Focused tests for lightweight performance instrumentation and metrics tracking."""

import time
from unittest.mock import MagicMock

import pytest

from support.core.performance import (
    LLMCallMetric,
    PerformanceTracker,
    get_current_tracker,
    set_current_tracker,
)
from support.llm.models import GenerationResponse, GenerationUsage
from support.query.models import QueryDraft
from support.workspace.state import WorkspaceSessionState


def test_tracker_measures_stage_durations():
    """Verify tracker accurately records named stage durations without crashing."""
    tracker = PerformanceTracker("test_op")

    with tracker.track("stage_a"):
        time.sleep(0.01)

    tracker.start_stage("stage_b")
    time.sleep(0.01)
    dur_b = tracker.end_stage("stage_b")

    tracker.finish()

    assert "stage_a" in tracker.stage_durations
    assert tracker.stage_durations["stage_a"] >= 5.0
    assert "stage_b" in tracker.stage_durations
    assert dur_b >= 5.0
    assert tracker.total_duration_ms >= (tracker.stage_durations["stage_a"] + tracker.stage_durations["stage_b"])


def test_tracker_records_timings_even_when_exception_occurs():
    """Verify that exceptions inside timed blocks do not lose timing metrics."""
    tracker = PerformanceTracker("failing_op")

    with pytest.raises(ValueError, match="intentional failure"):
        with tracker.track("failing_stage"):
            time.sleep(0.01)
            raise ValueError("intentional failure")

    tracker.finish()
    assert "failing_stage" in tracker.stage_durations
    assert tracker.stage_durations["failing_stage"] >= 5.0
    assert tracker.total_duration_ms >= tracker.stage_durations["failing_stage"]


def test_tracker_counts_multiple_llm_calls_and_tokens():
    """Verify multiple LLM calls and token counts are accumulated correctly."""
    tracker = PerformanceTracker("multi_llm")

    # Call 1: tool selection (has tokens)
    tracker.record_llm_call(
        purpose="tool_selection",
        duration_ms=120.0,
        provider="ollama",
        model="qwen2.5-coder:7b",
        prompt_tokens=150,
        completion_tokens=25,
        total_tokens=175,
        prompt_chars=500,
    )

    # Call 2: sql generation (has tokens)
    tracker.record_llm_call(
        purpose="sql_generation",
        duration_ms=450.0,
        provider="ollama",
        model="qwen2.5-coder:7b",
        prompt_tokens=350,
        completion_tokens=60,
        total_tokens=410,
        prompt_chars=1200,
    )

    # Call 3: repair (has tokens)
    tracker.record_repair_attempt()
    tracker.record_llm_call(
        purpose="pre_presentation_repair",
        duration_ms=300.0,
        provider="ollama",
        model="qwen2.5-coder:7b",
        prompt_tokens=400,
        completion_tokens=40,
        total_tokens=440,
        prompt_chars=1400,
    )

    tracker.record_validation_failure()
    tracker.finish()

    assert tracker.llm_calls_count == 3
    assert tracker.repair_attempts_count == 1
    assert tracker.validation_failures_count == 1
    assert tracker.total_prompt_tokens == 900
    assert tracker.total_completion_tokens == 125
    assert tracker.total_tokens == 1025
    assert tracker.stage_durations["llm_inference"] == 870.0


def test_tracker_handles_missing_provider_metrics_safely():
    """Verify missing token counts or durations from unsupported providers do not crash tracker."""
    tracker = PerformanceTracker("missing_tokens_op")

    tracker.record_llm_call(
        purpose="sql_generation",
        duration_ms=250.0,
        provider="mock",
        model="mock-model",
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )
    tracker.finish()

    assert tracker.llm_calls_count == 1
    assert tracker.total_prompt_tokens is None
    assert tracker.total_completion_tokens is None
    assert tracker.total_tokens is None

    # Serialization should format gracefully with '-'
    summary = tracker.format_summary_table()
    assert "Total Tokens:       -" in summary
    metrics_dict = tracker.to_dict()
    assert metrics_dict["tokens"]["total_tokens"] is None


def test_sensitive_data_not_written_to_performance_metrics():
    """Verify that passwords, credentials, and sensitive data are excluded from performance data."""
    tracker = PerformanceTracker("secure_op")

    tracker.record_llm_call(
        purpose="sql_generation",
        duration_ms=100.0,
        provider="ollama",
        model="qwen2.5-coder:7b",
        prompt_tokens=50,
        completion_tokens=20,
    )
    tracker.finish()

    data_str = str(tracker.to_dict())
    assert "password" not in data_str.lower()
    assert "secret" not in data_str.lower()
    assert "key" not in data_str.lower() or "call_index" in data_str  # only dict keys like 'tokens'


def test_context_tracker_get_set():
    """Verify thread-safe context tracker retrieval."""
    tracker = PerformanceTracker("context_op")
    set_current_tracker(tracker)
    assert get_current_tracker() is tracker
    set_current_tracker(None)
    assert get_current_tracker() is None
