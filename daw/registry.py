"""Workflows that `daw run <name>` can start by name."""

from __future__ import annotations

from typing import Any

from daw.workflow import Workflow

DEMO = "demo/research-brief"


def names() -> list[str]:
    from daw import evalspec

    return [DEMO] + [wf.name for _, wf in evalspec.eval_workflows()]


def get(name: str) -> tuple[Workflow, Any]:
    """Return (workflow, default run input)."""
    if name == DEMO:
        from demo import research_brief

        return research_brief.build_workflow(), research_brief.DEFAULT_INPUT
    if name.startswith("eval/"):
        from daw import evalspec

        for _, wf in evalspec.eval_workflows():
            if wf.name == name:
                return wf, evalspec.default_input(wf.name)
    raise KeyError(f"unknown workflow {name!r}; try `daw workflows`")
