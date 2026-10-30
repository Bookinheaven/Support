"""Minimal, non-intrusive performance instrumentation and metrics tracking."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import time
from typing import Any, Generator, Optional

from support.core.formatting import format_duration


@dataclass
class LLMCallMetric:
    """Detailed metrics for a single LLM generation call."""

    call_index: int
    purpose: str
    duration_ms: float
    provider: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    prompt_chars: int = 0
    finish_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to a clean dictionary without sensitive data."""
        return {
            "call_index": self.call_index,
            "purpose": self.purpose,
            "duration_ms": round(self.duration_ms, 2),
            "formatted_duration": format_duration(duration_ms=self.duration_ms),
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "prompt_chars": self.prompt_chars,
            "finish_reason": self.finish_reason,
        }


class PerformanceTracker:
    """Lightweight, non-intrusive timing and metrics tracker for request lifecycles."""

    def __init__(self, operation_name: str = "request") -> None:
        self.operation_name: str = operation_name
        self.start_time: float = time.perf_counter()
        self.end_time: Optional[float] = None

        # Stage durations in milliseconds
        self.stage_durations: dict[str, float] = {}
        self._active_stages: dict[str, float] = {}

        # LLM inference metrics
        self.llm_calls: list[LLMCallMetric] = []
        self.repair_attempts_count: int = 0
        self.validation_failures_count: int = 0

        # Diagnostics & metadata (strictly non-sensitive)
        self.metadata: dict[str, Any] = {}

    @property
    def total_duration_ms(self) -> float:
        """Total duration in milliseconds."""
        end = self.end_time if self.end_time is not None else time.perf_counter()
        return max(0.0, (end - self.start_time) * 1000.0)

    @property
    def llm_calls_count(self) -> int:
        """Number of LLM requests made."""
        return len(self.llm_calls)

    @property
    def total_prompt_tokens(self) -> Optional[int]:
        """Total prompt tokens across all LLM calls, or None if unavailable."""
        tokens = [c.prompt_tokens for c in self.llm_calls if c.prompt_tokens is not None]
        return sum(tokens) if tokens else None

    @property
    def total_completion_tokens(self) -> Optional[int]:
        """Total completion tokens across all LLM calls, or None if unavailable."""
        tokens = [c.completion_tokens for c in self.llm_calls if c.completion_tokens is not None]
        return sum(tokens) if tokens else None

    @property
    def total_tokens(self) -> Optional[int]:
        """Total tokens across all LLM calls, or None if unavailable."""
        p = self.total_prompt_tokens
        c = self.total_completion_tokens
        if p is not None or c is not None:
            return (p or 0) + (c or 0)
        return None

    def start_stage(self, stage: str) -> None:
        """Record the start of a timed stage."""
        self._active_stages[stage] = time.perf_counter()

    def end_stage(self, stage: str) -> float:
        """Record the end of a timed stage and accumulate duration in ms."""
        start = self._active_stages.pop(stage, None)
        if start is None:
            return 0.0
        elapsed_ms = max(0.0, (time.perf_counter() - start) * 1000.0)
        self.stage_durations[stage] = self.stage_durations.get(stage, 0.0) + elapsed_ms
        return elapsed_ms

    @contextmanager
    def track(self, stage: str) -> Generator[None, None, None]:
        """Context manager to measure a stage duration, resilient to exceptions."""
        self.start_stage(stage)
        try:
            yield
        finally:
            self.end_stage(stage)

    def record_llm_call(
        self,
        purpose: str,
        duration_ms: float,
        provider: str = "",
        model: str = "",
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        prompt_chars: int = 0,
        finish_reason: Optional[str] = None,
    ) -> None:
        """Record metrics for an executed LLM generation call."""
        # Also accumulate into llm_inference stage duration if not tracked separately
        self.stage_durations["llm_inference"] = (
            self.stage_durations.get("llm_inference", 0.0) + duration_ms
        )

        metric = LLMCallMetric(
            call_index=len(self.llm_calls) + 1,
            purpose=purpose,
            duration_ms=duration_ms,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            prompt_chars=prompt_chars,
            finish_reason=finish_reason,
        )
        self.llm_calls.append(metric)

    def record_repair_attempt(self) -> None:
        """Increment repair attempts count."""
        self.repair_attempts_count += 1

    def record_validation_failure(self) -> None:
        """Increment validation failures count."""
        self.validation_failures_count += 1

    def finish(self) -> None:
        """Complete the tracker measurement."""
        if self.end_time is None:
            self.end_time = time.perf_counter()
            self.stage_durations["total_duration"] = self.total_duration_ms

    def to_dict(self) -> dict[str, Any]:
        """Serialize tracker state into clean metrics."""
        self.finish()
        formatted_stages = {
            stage: {
                "duration_ms": round(dur, 2),
                "formatted": format_duration(duration_ms=dur),
            }
            for stage, dur in self.stage_durations.items()
        }

        return {
            "operation": self.operation_name,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "formatted_total_duration": format_duration(duration_ms=self.total_duration_ms),
            "stages": formatted_stages,
            "llm_calls_count": self.llm_calls_count,
            "repair_attempts_count": self.repair_attempts_count,
            "validation_failures_count": self.validation_failures_count,
            "tokens": {
                "prompt_tokens": self.total_prompt_tokens,
                "completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
            },
            "llm_calls": [c.to_dict() for c in self.llm_calls],
            "metadata": self.metadata,
        }

    def format_summary_table(self) -> str:
        """Generate a clean ASCII/Rich text table summarizing performance stages."""
        self.finish()
        lines = []
        lines.append(f"Performance Summary ({self.operation_name}):")
        lines.append(f"  Total Duration:     {format_duration(duration_ms=self.total_duration_ms)}")
        lines.append(f"  LLM Calls:          {self.llm_calls_count}")
        lines.append(f"  Repair Attempts:    {self.repair_attempts_count}")
        lines.append(f"  Validation Errors:  {self.validation_failures_count}")

        tok_str = "-"
        if self.total_tokens is not None:
            tok_str = f"{self.total_tokens} (in: {self.total_prompt_tokens or '-'}, out: {self.total_completion_tokens or '-'})"
        lines.append(f"  Total Tokens:       {tok_str}")
        lines.append("  Stages Breakdown:")

        # Desired ordering of canonical stages
        ordered_stages = [
            ("request_processing", "Request Processing"),
            ("tool_selection", "Tool Selection / Intent"),
            ("schema_retrieval", "Schema Retrieval"),
            ("prompt_construction", "Prompt Construction"),
            ("llm_inference", "LLM Inference"),
            ("sql_validation", "SQL AST Validation"),
            ("db_connection", "DB Connection Acquisition"),
            ("db_execution", "DB Execution"),
            ("sql_repair", "SQL Repair & Recovery"),
            ("result_formatting", "Result Formatting"),
        ]

        for key, label in ordered_stages:
            if key in self.stage_durations:
                dur = self.stage_durations[key]
                lines.append(f"    • {label:26}: {format_duration(duration_ms=dur):>10} ({dur:.1f} ms)")

        # Any extra stages
        for key, dur in self.stage_durations.items():
            if key not in [k for k, _ in ordered_stages] and key != "total_duration":
                lines.append(f"    • {key.capitalize():26}: {format_duration(duration_ms=dur):>10} ({dur:.1f} ms)")

        return "\n".join(lines)


# Thread-local / Contextual current tracker retrieval
_CURRENT_TRACKER: Optional[PerformanceTracker] = None


def get_current_tracker() -> Optional[PerformanceTracker]:
    """Retrieve the currently active performance tracker, if any."""
    return _CURRENT_TRACKER


def set_current_tracker(tracker: Optional[PerformanceTracker]) -> None:
    """Set the currently active performance tracker."""
    global _CURRENT_TRACKER
    _CURRENT_TRACKER = tracker
