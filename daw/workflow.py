"""Workflow and step definitions.

A workflow is an ordered list of named steps. A step points at a function by
import path ("package.module:function"); the engine runs that function in a
sandboxed subprocess, never in the engine process. Definitions round-trip
through plain JSON so a run's checkpoint carries its own definition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RUN_INPUT = "input"
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Step:
    name: str
    fn: str
    inputs: tuple[str, ...] = ()
    params: dict = field(default_factory=dict)
    timeout: float = 30.0
    max_attempts: int = 3
    backoff: float = 1.0
    env: tuple[str, ...] = ()  # names of environment variables passed into the sandbox

    def step_id(self, index: int) -> str:
        """Deterministic within a workflow: position plus name."""
        return f"{index:02d}-{self.name}"

    def to_spec(self) -> dict:
        return {
            "name": self.name,
            "fn": self.fn,
            "inputs": list(self.inputs),
            "params": self.params,
            "timeout": self.timeout,
            "max_attempts": self.max_attempts,
            "backoff": self.backoff,
            "env": list(self.env),
        }

    @classmethod
    def from_spec(cls, d: dict) -> "Step":
        return cls(
            name=d["name"],
            fn=d["fn"],
            inputs=tuple(d.get("inputs", ())),
            params=dict(d.get("params", {})),
            timeout=float(d.get("timeout", 30.0)),
            max_attempts=int(d.get("max_attempts", 3)),
            backoff=float(d.get("backoff", 1.0)),
            env=tuple(d.get("env", ())),
        )


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]
    description: str = ""

    def __post_init__(self) -> None:
        validate(self)

    def to_spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [s.to_spec() for s in self.steps],
        }

    @classmethod
    def from_spec(cls, d: dict) -> "Workflow":
        return cls(
            name=d["name"],
            steps=tuple(Step.from_spec(s) for s in d["steps"]),
            description=d.get("description", ""),
        )


def validate(wf: Workflow) -> None:
    if not wf.steps:
        raise ValueError(f"workflow {wf.name!r} has no steps")
    seen: set[str] = set()
    for i, s in enumerate(wf.steps):
        where = f"workflow {wf.name!r} step {i} ({s.name!r})"
        if not _NAME.match(s.name) or s.name == RUN_INPUT:
            raise ValueError(f"{where}: invalid step name")
        if s.name in seen:
            raise ValueError(f"{where}: duplicate step name")
        if ":" not in s.fn:
            raise ValueError(f"{where}: fn must look like 'module:function'")
        for dep in s.inputs:
            if dep != RUN_INPUT and dep not in seen:
                raise ValueError(f"{where}: input {dep!r} is not an earlier step")
        if s.max_attempts < 1:
            raise ValueError(f"{where}: max_attempts must be >= 1")
        if s.timeout <= 0 or s.backoff < 0:
            raise ValueError(f"{where}: timeout must be > 0 and backoff >= 0")
        seen.add(s.name)


def resolve_inputs(step: Step, outputs: dict[str, Any], run_input: Any) -> dict[str, Any]:
    return {dep: (run_input if dep == RUN_INPUT else outputs[dep]) for dep in step.inputs}
