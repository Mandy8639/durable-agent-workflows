"""Deterministic fake steps for tests and the kill-test eval. No model calls.

A step's output depends only on its name, kind, params and inputs, never on
the attempt number, timing or process, so a resumed run must reproduce the
golden run's outputs exactly.

If DAW_LEDGER is set, every attempt appends `start` and `end` records to that
file. The ledger is written by the step itself, outside the engine's
database, so the eval can count real executions independently of what the
engine believes happened.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from daw.context import StepContext, StepError

KINDS = ("instant", "slow", "fail_once", "always_fail", "timeout")


def run(inputs: dict, params: dict, ctx: StepContext) -> dict:
    kind = params["kind"]
    if kind not in KINDS:
        raise StepError(f"unknown fake step kind {kind!r}")
    will_timeout = kind == "timeout" and ctx.attempt <= int(params.get("bad_attempts", 1))
    _ledger("start", ctx, kind, will_timeout=will_timeout)

    if kind == "slow":
        time.sleep(float(params.get("sleep", 2.0)))
    elif kind == "fail_once" and ctx.attempt == 1:
        _ledger("end", ctx, kind, ok=False)
        raise StepError("fail_once: failing attempt 1 on purpose")
    elif kind == "always_fail":
        _ledger("end", ctx, kind, ok=False)
        raise StepError(f"always_fail: failing attempt {ctx.attempt} on purpose")
    elif will_timeout:
        time.sleep(float(params.get("sleep", 6.0)))  # the engine should kill us first

    digest_src = json.dumps({"step": ctx.step_name, "params": params, "inputs": inputs}, sort_keys=True)
    output = {
        "step": ctx.step_name,
        "kind": kind,
        "digest": hashlib.sha256(digest_src.encode()).hexdigest()[:16],
    }
    _ledger("end", ctx, kind, ok=True)
    return output


def _ledger(event: str, ctx: StepContext, kind: str, **extra) -> None:
    path = os.environ.get("DAW_LEDGER")
    if not path:
        return
    record = {
        "event": event,
        "run_id": ctx.run_id,
        "step": ctx.step_name,
        "index": ctx.step_index,
        "attempt": ctx.attempt,
        "kind": kind,
        "worker_pid": os.getppid(),
        "pid": os.getpid(),
        "t": time.time(),
        **extra,
    }
    line = (json.dumps(record, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
