"""Load the frozen eval suite (eval/workflows.json) and turn it into Workflows.

The suite file is checked against eval/workflows.sha256 before every use, so
an edited suite cannot silently produce numbers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from daw.engine import PROJECT_ROOT
from daw.workflow import RUN_INPUT, Step, Workflow

SUITE_PATH = PROJECT_ROOT / "eval" / "workflows.json"
HASH_PATH = PROJECT_ROOT / "eval" / "workflows.sha256"
PREFIX = "eval/"


class SuiteModified(RuntimeError):
    pass


def suite_sha256(path: Path = SUITE_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_suite(path: Path = SUITE_PATH, hash_path: Path = HASH_PATH) -> dict:
    expected = hash_path.read_text().split()[0]
    actual = suite_sha256(path)
    if actual != expected:
        raise SuiteModified(
            f"{path} does not match its frozen hash ({actual[:12]} != {expected[:12]}); "
            "the suite must not be edited after freezing"
        )
    return json.loads(path.read_text())


def build_workflow(wspec: dict, defaults: dict) -> Workflow:
    steps = []
    for i, s in enumerate(wspec["steps"]):
        kind = s["kind"]
        params: dict = {"kind": kind}
        timeout = float(defaults["timeout"])
        if kind == "slow":
            params["sleep"] = float(defaults["slow_sleep"])
        if kind == "timeout":
            params["sleep"] = float(defaults["timeout_sleep"])
            params["bad_attempts"] = int(s.get("bad_attempts", 1))
            timeout = float(defaults["timeout_step_timeout"])
        default_inputs = [RUN_INPUT] if i == 0 else [wspec["steps"][i - 1]["name"]]
        steps.append(
            Step(
                name=s["name"],
                fn="daw.fakesteps:run",
                inputs=tuple(s.get("inputs", default_inputs)),
                params=params,
                timeout=timeout,
                max_attempts=int(defaults["max_attempts"]),
                backoff=float(defaults["backoff"]),
                env=("DAW_LEDGER",),
            )
        )
    return Workflow(name=PREFIX + wspec["name"], steps=tuple(steps),
                    description=f"frozen kill-test workflow (expect {wspec['expect']})")


def eval_workflows(suite: dict | None = None) -> list[tuple[dict, Workflow]]:
    suite = suite or load_suite()
    return [(w, build_workflow(w, suite["defaults"])) for w in suite["workflows"]]


def default_input(workflow_name: str) -> dict:
    return {"workflow": workflow_name}
