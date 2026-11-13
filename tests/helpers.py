from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from daw.engine import Engine
from daw.store import Store
from daw.workflow import RUN_INPUT, Step, Workflow


def fake(name: str, kind: str = "instant", inputs=None, **kw) -> Step:
    params = {"kind": kind}
    for key in ("sleep", "bad_attempts"):
        if key in kw:
            params[key] = kw.pop(key)
    return Step(name=name, fn="daw.fakesteps:run", inputs=tuple(inputs or ()), params=params,
                env=("DAW_LEDGER",), **{"backoff": 0.0, **kw})


def chain(name: str, *steps: Step) -> Workflow:
    fixed = []
    for i, s in enumerate(steps):
        if not s.inputs:
            deps = (RUN_INPUT,) if i == 0 else (steps[i - 1].name,)
            s = Step(**{**s.__dict__, "inputs": deps})
        fixed.append(s)
    return Workflow(name=name, steps=tuple(fixed))


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="daw-test-"))
        self.ledger = self.tmp / "ledger.jsonl"
        self._old_ledger = os.environ.get("DAW_LEDGER")
        os.environ["DAW_LEDGER"] = str(self.ledger)
        self.db = self.tmp / "daw.db"
        self.store = Store(str(self.db))
        self.engine = Engine(self.store, self.tmp / "ws", log=lambda _m: None)

    def tearDown(self) -> None:
        self.store.close()
        if self._old_ledger is None:
            os.environ.pop("DAW_LEDGER", None)
        else:
            os.environ["DAW_LEDGER"] = self._old_ledger

    def events(self, event: str = "start") -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text().splitlines() if l and json.loads(l)["event"] == event]

    def executions(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.events("start"):
            counts[e["step"]] = counts.get(e["step"], 0) + 1
        return counts
