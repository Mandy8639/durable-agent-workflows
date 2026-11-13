"""Command line: run, resume, list, show, eval, workflows.

The SQLite file plus these commands are the whole observability story.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from daw.engine import Engine, summarize
from daw.store import RunLocked, Store

DEFAULT_DB = os.environ.get("DAW_DB", ".daw/daw.db")


def _open(db: str) -> tuple[Store, Path]:
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    return Store(db, fullfsync=os.environ.get("DAW_FULLFSYNC") == "1"), Path(db).parent / "workspaces"


def _finish(result, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(f"run {result.run_id} {result.status}")
        if result.error:
            print(f"error: {result.error}")
    return 0 if result.status == "completed" else 1


def cmd_workflows(args) -> int:
    from daw import registry

    for name in registry.names():
        print(name)
    return 0


def cmd_run(args) -> int:
    from daw import registry

    wf, default_input = registry.get(args.workflow)
    run_input = json.loads(args.input) if args.input is not None else default_input
    if args.no_checkpoint:
        tmp = tempfile.mkdtemp(prefix="daw-nockpt-")
        try:
            engine = Engine(Store(":memory:"), Path(tmp) / "workspaces")
            return _finish(engine.run(wf, run_input, args.run_id), args.json)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    store, ws = _open(args.db)
    engine = Engine(store, ws)
    run_id = engine.start(wf, run_input, args.run_id)
    print(f"run {run_id} started ({wf.name}); resume with: daw resume {run_id}",
          file=sys.stderr, flush=True)
    return _finish(engine.execute(run_id), args.json)


def cmd_resume(args) -> int:
    store, ws = _open(args.db)
    try:
        result = Engine(store, ws).execute(args.run_id, force=args.force)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    except RunLocked as e:
        print(f"{e}; pass --force if you are sure it is not running", file=sys.stderr)
        return 2
    return _finish(result, args.json)


def _fmt_time(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def _clip(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_list(args) -> int:
    store, _ = _open(args.db)
    runs = store.list_runs(args.status, args.limit)
    print(f"{'RUN ID':<30} {'WORKFLOW':<34} {'STATUS':<10} {'STEPS':<7} {'CREATED':<19} DURATION")
    for r in runs:
        steps = store.get_steps(r["id"])
        done = sum(s["status"] == "completed" for s in steps)
        total = len(r["spec"]["steps"])
        end = r["finished_at"] or r["updated_at"]
        print(f"{r['id']:<30} {_clip(r['workflow'], 34):<34} {r['status']:<10} "
              f"{f'{done}/{total}':<7} {_fmt_time(r['created_at']):<19} {end - r['created_at']:.2f}s")
    return 0


def cmd_show(args) -> int:
    store, _ = _open(args.db)
    run = store.get_run(args.run_id)
    if run is None:
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 2
    steps = {s["step_index"]: s for s in store.get_steps(args.run_id)}
    if args.json:
        print(json.dumps({"run": run, "steps": list(steps.values())}, sort_keys=True, indent=2))
        return 0
    print(f"run       {run['id']}")
    print(f"workflow  {run['workflow']}")
    print(f"status    {run['status']}" + (f" (owner pid {run['owner_pid']})" if run["owner_pid"] else ""))
    print(f"created   {_fmt_time(run['created_at'])}   finished {_fmt_time(run['finished_at'])}")
    if run["error"]:
        print(f"error     {run['error']}")
    print()
    print(f"{'#':<3} {'STEP':<14} {'STATUS':<10} {'TRIES':<6} {'SECS':<7} DETAIL")
    for i, spec in enumerate(run["spec"]["steps"]):
        s = steps.get(i)
        if s is None:
            print(f"{i:<3} {spec['name']:<14} {'pending':<10}")
            continue
        secs = f"{(s['finished_at'] or s['updated_at']) - s['started_at']:.2f}" if s["started_at"] else "-"
        detail = json.dumps(s["output"], sort_keys=True) if s["status"] == "completed" else (s["error"] or "")
        print(f"{i:<3} {s['step_name']:<14} {s['status']:<10} {s['attempts']:<6} {secs:<7} {_clip(detail, 90)}")
    return 0


def cmd_eval(args) -> int:
    from daw import killeval

    return killeval.main(reps=args.reps, only=args.only, out=args.out)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB, help=f"SQLite checkpoint file (default {DEFAULT_DB})")

    p = argparse.ArgumentParser(prog="daw", description="Durable agent workflows (single machine).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("workflows", help="list workflows that can be run by name")
    sp.set_defaults(fn=cmd_workflows)

    sp = sub.add_parser("run", parents=[common], help="start a workflow and run it to the end")
    sp.add_argument("workflow")
    sp.add_argument("--input", help="run input as JSON (default: the workflow's default)")
    sp.add_argument("--run-id")
    sp.add_argument("--no-checkpoint", action="store_true",
                    help="baseline mode: in-memory store, nothing survives a crash")
    sp.add_argument("--json", action="store_true", help="print the final result as JSON")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("resume", parents=[common], help="continue a run from its checkpoint")
    sp.add_argument("run_id")
    sp.add_argument("--force", action="store_true", help="ignore the owner-pid liveness check")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("list", parents=[common], help="list runs")
    sp.add_argument("--status", choices=["pending", "running", "completed", "failed"])
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", parents=[common], help="show one run and its steps")
    sp.add_argument("run_id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("eval", help="run the frozen kill-test suite and print the table")
    sp.add_argument("--reps", type=int, default=3, help="uninterrupted runs per condition for timing")
    sp.add_argument("--only", nargs="*", help="subset of workflow names (for debugging only)")
    sp.add_argument("--out", help="results directory (default eval/results/<timestamp>)")
    sp.set_defaults(fn=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
