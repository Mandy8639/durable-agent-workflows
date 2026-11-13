"""Micro-benchmark: what does one step's checkpoint cost, next to running the step?

    python -m daw.bench [--steps 500] [--spawns 50] [--out FILE]

Per step the engine makes two commits (attempt started, output checkpointed).
This times exactly those two calls against three stores, and separately times
one sandboxed no-op step (subprocess spawn + Python start + import), which is
the cost every step pays in both eval conditions.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from daw.engine import PROJECT_ROOT
from daw.sandbox import run_tool
from daw.store import Store


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def time_checkpoints(store: Store, n: int) -> list[float]:
    store.create_run("bench", "bench", {"steps": []}, None)
    output = {"step": "x", "kind": "instant", "digest": "0" * 16}
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        store.start_attempt("bench", i, f"s{i}", {"prev": output}, 1)
        store.complete_step("bench", i, output)
        out.append(1000 * (time.perf_counter() - t0))
    return out


def time_spawns(n: int, workdir: Path) -> list[float]:
    payload = json.dumps({
        "fn": "daw.fakesteps:run", "inputs": {}, "params": {"kind": "instant"},
        "ctx": {"run_id": "bench", "step_name": "s", "step_index": 0, "attempt": 1,
                "max_attempts": 1, "workdir": str(workdir)},
    }).encode()
    out = []
    for _ in range(n):
        res = run_tool([sys.executable, "-s", "-m", "daw.runner"], cwd=str(workdir), timeout=30,
                       stdin_data=payload, env={"PYTHONPATH": str(PROJECT_ROOT)})
        assert res.ok, res.describe()
        out.append(1000 * res.duration)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m daw.bench")
    ap.add_argument("--steps", type=int, default=500)
    ap.add_argument("--spawns", type=int, default=50)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="daw-bench-"))

    rows = {}
    for label, factory in [
        ("in-memory (no-checkpoint baseline)", lambda: Store(":memory:")),
        ("WAL + synchronous=FULL (engine default)", lambda: Store(str(tmp / "a.db"))),
        ("WAL + synchronous=FULL + fullfsync", lambda: Store(str(tmp / "b.db"), fullfsync=True)),
    ]:
        xs = time_checkpoints(factory(), args.steps)
        rows[label] = {"median_ms": statistics.median(xs), "p95_ms": _pct(xs, 0.95), "n": len(xs)}
    spawn = time_spawns(args.spawns, tmp / "spawn")
    rows["sandboxed no-op step (subprocess spawn)"] = {
        "median_ms": statistics.median(spawn), "p95_ms": _pct(spawn, 0.95), "n": len(spawn)}

    meta = {"when": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "platform": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"}
    lines = [f"Checkpoint cost per step (2 commits), {meta['platform']}, {meta['when']}:", "",
             "| What | Median | p95 | n |", "|---|---|---|---|"]
    for label, r in rows.items():
        lines.append(f"| {label} | {r['median_ms']:.2f} ms | {r['p95_ms']:.2f} ms | {r['n']} |")
    print("\n".join(lines))
    if args.out:
        Path(args.out).write_text(json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
