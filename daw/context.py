"""What a step function receives besides its inputs and params."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepContext:
    run_id: str
    step_name: str
    step_index: int
    attempt: int
    max_attempts: int
    workdir: str


class StepError(Exception):
    """Raise from a step to fail the current attempt (it will be retried per policy)."""
