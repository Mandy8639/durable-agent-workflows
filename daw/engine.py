"""The durable execution loop.

Rules the engine enforces:

1. Before an attempt starts, the attempt is recorded (status=running, attempt
   count) in its own transaction.
2. When an attempt succeeds, its output and status=completed are committed in
   one transaction, and only then does the next step start.
3. On resume, completed steps are never executed again: their stored outputs
   are reused. Execution continues at the first step that is not completed.
4. An attempt that was in flight when the worker died counts toward the step's
   max_attempts (so a step that keeps crashing the worker eventually fails the
   run instead of looping forever).
5. A step that exhausts its attempts fails the run cleanly: the step and the
   run are marked failed in one transaction, with the error recorded.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from daw.sandbox import Limits, ToolResult, run_tool
from daw.store import Store, RUN_ACTIVE
from daw.workflow import Step, Workflow, resolve_inputs

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunResult:
    run_id: str
    workflow: str
    status: str
    outputs: dict[str, Any]
    result: Any
    error: str | None
    failed_step: str | None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stderr_log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Engine:
    def __init__(
        self,
        store: Store,
        workspace: str | Path,
        *,
        python: str = sys.executable,
        python_path: list[str] | None = None,
        limits: Limits = Limits(),
        log: Callable[[str], None] = stderr_log,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.workspace = Path(workspace)
        self.python = python
        self.python_path = python_path or [str(PROJECT_ROOT)]
        self.limits = limits
        self.log = log
        self.sleep = sleep

    # --- public API -----------------------------------------------------

    def start(self, wf: Workflow, run_input: Any = None, run_id: str | None = None) -> str:
        run_id = run_id or new_run_id()
        self.store.create_run(run_id, wf.name, wf.to_spec(), run_input)
        return run_id

    def run(self, wf: Workflow, run_input: Any = None, run_id: str | None = None) -> RunResult:
        return self.execute(self.start(wf, run_input, run_id))

    def execute(self, run_id: str, force: bool = False, retry_failed: bool = False) -> RunResult:
        """Run (or resume) `run_id` until it completes or fails.

        `retry_failed` reopens a failed run: its failed step gets a fresh attempt budget and
        every completed step is still reused, never re-executed.
        """
        if retry_failed and self.store.reopen_failed(run_id):
            self.log(f"[{run_id}] reopened failed run; completed steps will be reused")
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"no such run: {run_id}")
        if run["status"] not in RUN_ACTIVE:
            self.log(f"[{run_id}] already {run['status']}; nothing to do")
            return summarize(self.store, run_id)

        alive = (lambda _pid: False) if force else pid_alive
        self.store.claim_run(run_id, os.getpid(), alive)
        wf = Workflow.from_spec(run["spec"])
        rows = {r["step_index"]: r for r in self.store.get_steps(run_id)}
        outputs: dict[str, Any] = {}
        n = len(wf.steps)

        for idx, step in enumerate(wf.steps):
            row = rows.get(idx)
            tag = f"[{run_id}] step {idx + 1}/{n} {step.name}"
            if row is not None and row["step_name"] != step.name:
                raise RuntimeError(f"{tag}: checkpoint has step {row['step_name']!r} at this index")
            if row is not None and row["status"] == "completed":
                outputs[step.name] = row["output"]
                self.log(f"{tag}: completed earlier, reusing checkpointed output")
                continue
            if row is not None and row["status"] == "failed":
                self.store.fail_run(run_id, row["error"] or f"step {step.name} failed")
                return summarize(self.store, run_id)

            inputs = resolve_inputs(step, outputs, run["input"])
            ok, value = self._run_step(run_id, idx, step, inputs, row, tag)
            if not ok:
                return summarize(self.store, run_id)
            outputs[step.name] = value

        self.store.complete_run(run_id, outputs[wf.steps[-1].name])
        self.log(f"[{run_id}] run completed")
        return summarize(self.store, run_id)

    # --- internals ------------------------------------------------------

    def _run_step(
        self, run_id: str, idx: int, step: Step, inputs: dict, row: dict | None, tag: str
    ) -> tuple[bool, Any]:
        attempts = row["attempts"] if row else 0
        last_error = row["error"] if row else None
        if row is not None and row["status"] == "running":
            last_error = f"attempt {attempts}: interrupted, worker process died"
            self.log(f"{tag}: attempt {attempts} was interrupted by a crash (counts as an attempt)")

        while True:
            if attempts >= step.max_attempts:
                error = f"step {step.name!r} used all {step.max_attempts} attempts; last: {last_error}"
                self.store.fail_attempt(run_id, idx, error, final=True)
                self.log(f"{tag}: FAILED, {error}")
                return False, None

            attempts += 1
            self.store.start_attempt(run_id, idx, step.name, inputs, attempts)
            self.log(f"{tag}: attempt {attempts}/{step.max_attempts} started")
            res = self._invoke(run_id, idx, step, inputs, attempts)

            if res.ok:
                try:
                    output = json.loads(res.stdout)
                except json.JSONDecodeError as e:
                    res = ToolResult("error", res.exit_code, res.stdout, f"invalid JSON output: {e}",
                                     res.duration)
                else:
                    self.store.complete_step(run_id, idx, output)
                    self.log(f"{tag}: attempt {attempts} ok in {res.duration:.2f}s, checkpointed")
                    return True, output

            last_error = f"attempt {attempts}: {res.status}: {res.describe()}"
            final = attempts >= step.max_attempts
            if final:
                error = f"step {step.name!r} used all {step.max_attempts} attempts; last: {last_error}"
                self.store.fail_attempt(run_id, idx, error, final=True)
                self.log(f"{tag}: FAILED, {error}")
                return False, None
            self.store.fail_attempt(run_id, idx, last_error, final=False)
            self.log(f"{tag}: {last_error}; retrying in {step.backoff:g}s")
            self.sleep(step.backoff)

    def _invoke(self, run_id: str, idx: int, step: Step, inputs: dict, attempt: int) -> ToolResult:
        workdir = self.workspace / run_id / step.step_id(idx) / f"attempt-{attempt}"
        payload = {
            "fn": step.fn,
            "inputs": inputs,
            "params": step.params,
            "ctx": {
                "run_id": run_id,
                "step_name": step.name,
                "step_index": idx,
                "attempt": attempt,
                "max_attempts": step.max_attempts,
                "workdir": str(workdir),
            },
        }
        env = {"PYTHONPATH": os.pathsep.join(self.python_path)}
        env.update({k: os.environ[k] for k in step.env if k in os.environ})
        return run_tool(
            [self.python, "-s", "-m", "daw.runner"],
            cwd=str(workdir),
            timeout=step.timeout,
            stdin_data=json.dumps(payload).encode(),
            env=env,
            limits=self.limits,
        )


def summarize(store: Store, run_id: str) -> RunResult:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    steps = store.get_steps(run_id)
    failed = [s["step_name"] for s in steps if s["status"] == "failed"]
    return RunResult(
        run_id=run_id,
        workflow=run["workflow"],
        status=run["status"],
        outputs={s["step_name"]: s["output"] for s in steps if s["status"] == "completed"},
        result=run["result"],
        error=run["error"],
        failed_step=failed[0] if failed else None,
    )
