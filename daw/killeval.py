"""Kill-test eval over the frozen suite.

Per workflow:
  golden   run uninterrupted with checkpointing; its outputs are the reference.
  timing   `reps` uninterrupted runs each of checkpointed and plain (no-checkpoint)
           mode, interleaved, for the overhead number.
  kill (a) no-checkpoint baseline: start, SIGKILL at the frozen kill point,
           restart the workflow from step 1.
  kill (b) checkpointed: start, SIGKILL at the same point, `daw resume`.

Every worker is a real `python -m daw ...` process started in its own process
group; the kill is SIGKILL to that group (worker plus its in-flight tool
subprocess), i.e. a crash with no chance to clean up.

Executions are counted from a ledger the fake steps append to themselves, not
from the engine's database, so the engine cannot grade its own homework.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from daw import evalspec
from daw.engine import PROJECT_ROOT
from daw.store import Store

POLL = 0.002
TRIGGER_TIMEOUT = 120.0


# --- process helpers -------------------------------------------------------

class Worker:
    def __init__(self, args: list[str], ledger: Path, log: Path):
        env = dict(os.environ, DAW_LEDGER=str(ledger), PYTHONPATH=str(PROJECT_ROOT))
        env.pop("DAW_DB", None)
        log.parent.mkdir(parents=True, exist_ok=True)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(log, "ab")
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "daw", *args],
            cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=self.log,
            start_new_session=True,
        )
        self.pid = self.proc.pid

    def wait(self) -> tuple[dict | None, float]:
        out, _ = self.proc.communicate()
        wall = time.monotonic() - self.t0
        self.log.close()
        lines = out.decode().strip().splitlines()
        return (json.loads(lines[-1]) if lines else None), wall

    def kill_group(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line torn by SIGKILL
    return events


def wait_for_trigger(ledger: Path, worker: Worker, kill: dict) -> dict | None:
    """Poll the ledger until the kill point's event appears for this worker."""
    offset, buf = 0, b""
    deadline = time.monotonic() + TRIGGER_TIMEOUT
    while time.monotonic() < deadline:
        if ledger.exists():
            with open(ledger, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            offset += len(chunk)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e["worker_pid"] == worker.pid and e["event"] == kill["on"]
                        and e["step"] == kill["step"] and e["attempt"] == kill["attempt"]):
                    return e
        if worker.proc.poll() is not None:
            return None
        time.sleep(POLL)
    return None


# --- comparisons -----------------------------------------------------------

def outcome(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {"status": result["status"], "outputs": result["outputs"], "failed_step": result["failed_step"]}


def timeout_checks(events: list[dict], worker_pid: int, final: dict | None, max_attempts: int,
                   skip_pids: set[int] = frozenset()) -> tuple[int, int, list[str]]:
    """(handled, total, problems) for will_timeout attempts run by `worker_pid`."""
    ended = {e["pid"] for e in events if e["event"] == "end"}
    starts = [e for e in events if e["worker_pid"] == worker_pid and e["event"] == "start"]
    handled, total, problems = 0, 0, []
    for e in starts:
        if not e.get("will_timeout") or e["pid"] in skip_pids:
            continue
        total += 1
        killed = e["pid"] not in ended
        if e["attempt"] < max_attempts:
            moved_on = any(s["step"] == e["step"] and s["attempt"] == e["attempt"] + 1
                           and s["run_id"] == e["run_id"] for s in starts)
        else:
            moved_on = bool(final and final["status"] == "failed" and final["failed_step"] == e["step"])
        if killed and moved_on:
            handled += 1
        else:
            problems.append(f"{e['step']} attempt {e['attempt']}: killed={killed} policy_followed={moved_on}")
    return handled, total, problems


# --- one workflow ----------------------------------------------------------

def run_workflow(wspec: dict, wf, out: Path, db: Path, reps: int, max_attempts: int) -> dict:
    name = wf.name
    short = wspec["name"]
    wdir = out / "ledgers" / short
    ldir = out / "logs" / short
    rec: dict = {"workflow": short, "steps": len(wf.steps), "kill": wspec["kill"],
                 "expect": wspec["expect"], "timeouts": {"ckpt": [0, 0], "plain": [0, 0]},
                 "timeout_problems": []}

    def add_timeouts(cond: str, events, pid, final, skip_pids=frozenset()):
        h, t, probs = timeout_checks(events, pid, final, max_attempts, skip_pids)
        rec["timeouts"][cond][0] += h
        rec["timeouts"][cond][1] += t
        rec["timeout_problems"] += [f"{cond}: {p}" for p in probs]

    # golden + timing reps, interleaved so drift hits both conditions alike
    ckpt_walls, plain_walls, golden, repro = [], [], None, True
    for rep in range(reps):
        for cond in ("ckpt", "plain"):
            label = f"{cond}-rep{rep}"
            ledger = wdir / f"{label}.jsonl"
            run_id = f"{short}-{label}"
            args = ["run", name, "--run-id", run_id, "--json"]
            args += ["--db", str(db)] if cond == "ckpt" else ["--no-checkpoint"]
            w = Worker(args, ledger, ldir / f"{label}.log")
            result, wall = w.wait()
            (ckpt_walls if cond == "ckpt" else plain_walls).append(wall)
            add_timeouts(cond, read_ledger(ledger), w.pid, result)
            if golden is None:
                golden = outcome(result)  # first checkpointed run is the golden result
            elif outcome(result) != golden:
                repro = False
    rec["golden"] = golden
    rec["golden_status_as_expected"] = golden is not None and golden["status"] == wspec["expect"]
    rec["uninterrupted_reproducible"] = repro
    rec["wall"] = {"ckpt": ckpt_walls, "plain": plain_walls}

    for cond in ("plain", "ckpt"):
        rec[f"kill_{cond}"] = kill_trial(cond, wspec, wf, out, db, golden, add_timeouts)
    return rec


def kill_trial(cond: str, wspec: dict, wf, out: Path, db: Path, golden: dict | None, add_timeouts) -> dict:
    short = wspec["name"]
    kill = wspec["kill"]
    ledger = out / "ledgers" / short / f"kill-{cond}.jsonl"
    logs = out / "logs" / short
    run_id = f"{short}-kill-{cond}"
    args = ["run", wf.name, "--run-id", run_id, "--json"]
    args += ["--db", str(db)] if cond == "ckpt" else ["--no-checkpoint"]

    w1 = Worker(args, ledger, logs / f"kill-{cond}-1.log")
    trig = wait_for_trigger(ledger, w1, kill)
    delivered = False
    if trig is not None:
        time.sleep(max(0.0, trig["t"] + kill["delay"] - time.time()))
        if w1.proc.poll() is None:
            w1.kill_group()
            delivered = True
    first_result, _ = w1.wait()
    rc = w1.proc.returncode

    checkpointed: set[str] = set()
    if cond == "ckpt":
        store = Store(str(db))
        checkpointed = {s["step_name"] for s in store.get_steps(run_id) if s["status"] == "completed"}
        store.close()

    if delivered:
        args2 = (["resume", run_id, "--db", str(db), "--json"] if cond == "ckpt"
                 else ["run", wf.name, "--run-id", f"{run_id}-restart", "--json", "--no-checkpoint"])
        w2 = Worker(args2, ledger, logs / f"kill-{cond}-2.log")
        final, recovery_wall = w2.wait()
    else:
        w2, final, recovery_wall = None, first_result, 0.0

    events = read_ledger(ledger)
    p1 = [e for e in events if e["worker_pid"] == w1.pid]
    p2 = [e for e in events if w2 is not None and e["worker_pid"] == w2.pid]
    finished_before = {e["step"] for e in p1 if e["event"] == "end" and e.get("ok")}
    started_p1 = {e["pid"]: e for e in p1 if e["event"] == "start"}
    ended_p1 = {e["pid"] for e in p1 if e["event"] == "end"}
    in_flight = sorted({e["step"] for pid, e in started_p1.items() if pid not in ended_p1})
    starts_p2 = [e for e in p2 if e["event"] == "start"]

    if cond == "ckpt":
        redundant = sum(e["step"] in checkpointed for e in starts_p2)
        window = sum(e["step"] in finished_before - checkpointed for e in starts_p2)
    else:
        redundant = sum(e["step"] in finished_before for e in starts_p2)
        window = None

    # timeout attempts: the in-flight one at kill time was ended by our SIGKILL, not the engine
    add_timeouts(cond, events, w1.pid, first_result, skip_pids={pid for pid in started_p1 if pid not in ended_p1})
    if w2 is not None:
        add_timeouts(cond, events, w2.pid, final)

    final_o = outcome(final)
    return {
        "kill_delivered": delivered,
        "worker_returncode": rc,
        "checkpointed_at_kill": sorted(checkpointed),
        "finished_before_kill": sorted(finished_before),
        "in_flight_at_kill": in_flight,
        "terminal": final is not None and final["status"] in ("completed", "failed"),
        "status_match": final_o is not None and golden is not None and final_o["status"] == golden["status"],
        "output_match": final_o == golden,
        "redundant_executions": redundant,
        "commit_window_reexecutions": window,
        "executions_after_kill": len(starts_p2),
        "recovery_wall": recovery_wall,
        "final": final_o,
    }


# --- report ----------------------------------------------------------------

def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def build_report(recs: list[dict], meta: dict) -> tuple[str, dict]:
    n = len(recs)
    agg: dict = {}
    for cond in ("plain", "ckpt"):
        ks = [r[f"kill_{cond}"] for r in recs]
        delivered = [k for k in ks if k["kill_delivered"]]
        d = len(delivered)
        agg[cond] = {
            "delivered": sum(k["kill_delivered"] for k in ks),
            "terminal": sum(k["terminal"] for k in delivered),
            "status_match": sum(k["status_match"] for k in delivered),
            "output_match": sum(k["output_match"] for k in delivered),
            "redundant": sum(k["redundant_executions"] for k in delivered),
            "window": sum(k["commit_window_reexecutions"] or 0 for k in delivered),
            "after_kill": sum(k["executions_after_kill"] for k in delivered),
            "recovery_median": statistics.median([k["recovery_wall"] for k in delivered]) if d else 0.0,
            "timeouts": [sum(r["timeouts"][cond][0] for r in recs), sum(r["timeouts"][cond][1] for r in recs)],
        }
    ratios = []
    per_step_ms = []
    instant_ratios = []
    for r in recs:
        c, p = statistics.median(r["wall"]["ckpt"]), statistics.median(r["wall"]["plain"])
        ratios.append(c / p - 1)
        per_step_ms.append(1000 * (c - p) / r["steps"])
        if all(k == "instant" for k in r["step_kinds"]):
            instant_ratios.append(c / p - 1)
    ov = {
        "median_overhead_pct": 100 * statistics.median(ratios),
        "median_overhead_pct_instant_only": 100 * statistics.median(instant_ratios) if instant_ratios else None,
        "median_extra_ms_per_step": statistics.median(per_step_ms),
        "median_wall_ckpt": statistics.median(statistics.median(r["wall"]["ckpt"]) for r in recs),
        "median_wall_plain": statistics.median(statistics.median(r["wall"]["plain"]) for r in recs),
    }
    a, b = agg["plain"], agg["ckpt"]
    da, db_ = a["delivered"], b["delivered"]
    L = []
    L.append(f"Suite `{meta['suite']}` (sha256 `{meta['suite_sha256'][:16]}…`), {n} workflows, "
             f"{meta['reps']} uninterrupted reps per condition, {meta['when']}, {meta['platform']}.")
    L.append("")
    L.append("| Metric | (a) No-checkpoint baseline | (b) Checkpointed engine |")
    L.append("|---|---|---|")
    L.append(f"| Kills delivered mid-run | {pct(da, n)} | {pct(db_, n)} |")
    L.append(f"| Runs completed after kill (reached a terminal state) | {pct(a['terminal'], da)} | {pct(b['terminal'], db_)} |")
    L.append(f"| Resume success (terminal status = golden status) | {pct(a['status_match'], da)} | {pct(b['status_match'], db_)} |")
    L.append(f"| Output match vs golden (status + every step output) | {pct(a['output_match'], da)} | {pct(b['output_match'], db_)} |")
    L.append(f"| Redundant executions of already-completed steps | {a['redundant']} | {b['redundant']} |")
    L.append(f"| Re-executions in the commit window (step finished, checkpoint not yet committed) | n/a | {b['window']} |")
    L.append(f"| Step executions after the kill (total) | {a['after_kill']} | {b['after_kill']} |")
    L.append(f"| Timeout kills handled correctly (killed + retried/failed per policy) | {pct(*a['timeouts'])} | {pct(*b['timeouts'])} |")
    L.append(f"| Median recovery wall-clock after kill | {a['recovery_median']:.2f}s | {b['recovery_median']:.2f}s |")
    L.append(f"| Median uninterrupted wall-clock (per-workflow medians) | {ov['median_wall_plain']:.2f}s | {ov['median_wall_ckpt']:.2f}s |")
    L.append(f"| Checkpointing overhead vs plain, median over workflows | — | {ov['median_overhead_pct']:+.1f}% |")
    if ov["median_overhead_pct_instant_only"] is not None:
        L.append(f"| …same, instant-only workflows (no sleeps to hide the cost) | — | {ov['median_overhead_pct_instant_only']:+.1f}% |")
    L.append(f"| Median extra wall-clock per step | — | {ov['median_extra_ms_per_step']:+.1f} ms |")
    L.append("")
    L.append("Per workflow (kill point; state when killed; result after recovery):")
    L.append("")
    L.append("| Workflow | Steps | Kill point | Golden | Ckpt at kill | In flight | (b) match | (b) redundant | (b) window | (a) redundant | (a) recovery | (b) recovery | Overhead |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in recs:
        kb, ka = r["kill_ckpt"], r["kill_plain"]
        k = r["kill"]
        c, p = statistics.median(r["wall"]["ckpt"]), statistics.median(r["wall"]["plain"])
        golden = r["golden"]["status"] if r["golden"] else "none"
        if not r["golden_status_as_expected"]:
            golden += f" (expected {r['expect']})"
        L.append(
            f"| {r['workflow']} | {r['steps']} | {k['on']} {k['step']}#{k['attempt']} +{k['delay']}s | {golden} | "
            f"{len(kb['checkpointed_at_kill'])}/{r['steps']} | {','.join(kb['in_flight_at_kill']) or '-'} | "
            f"{'yes' if kb['output_match'] else 'NO'}{'' if kb['kill_delivered'] else ' (not killed)'} | "
            f"{kb['redundant_executions']} | {kb['commit_window_reexecutions']} | {ka['redundant_executions']} | "
            f"{ka['recovery_wall']:.2f}s | {kb['recovery_wall']:.2f}s | {100 * (c / p - 1):+.1f}% |"
        )
    problems = [(r["workflow"], p) for r in recs for p in r["timeout_problems"]]
    flags = [r["workflow"] for r in recs if not r["golden_status_as_expected"] or not r["uninterrupted_reproducible"]]
    L.append("")
    L.append(f"Timeout-handling problems: {problems or 'none'}. "
             f"Workflows whose golden status differed from the frozen expectation or whose uninterrupted runs "
             f"disagreed: {flags or 'none'}.")
    return "\n".join(L), {"conditions": agg, "overhead": ov}


def main(reps: int = 3, only: list[str] | None = None, out: str | None = None) -> int:
    suite = evalspec.load_suite()
    items = evalspec.eval_workflows(suite)
    if only:
        items = [(w, wf) for w, wf in items if w["name"] in only]
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    out_dir = Path(out) if out else PROJECT_ROOT / "eval" / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "eval.db"
    max_attempts = int(suite["defaults"]["max_attempts"])
    meta = {
        "suite": suite["suite"], "suite_sha256": evalspec.suite_sha256(), "reps": reps, "when": when,
        "partial": bool(only), "workflows": [w["name"] for w, _ in items],
        "platform": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "model_calls": 0,
    }
    recs = []
    t0 = time.monotonic()
    for i, (wspec, wf) in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {wspec['name']} ...", file=sys.stderr, flush=True)
        rec = run_workflow(wspec, wf, out_dir, db, reps, max_attempts)
        rec["step_kinds"] = [s["kind"] for s in wspec["steps"]]
        recs.append(rec)
        kb = rec["kill_ckpt"]
        print(f"    golden={rec['golden']['status'] if rec['golden'] else None} "
              f"ckpt: match={kb['output_match']} redundant={kb['redundant_executions']} "
              f"window={kb['commit_window_reexecutions']}  baseline redundant="
              f"{rec['kill_plain']['redundant_executions']}", file=sys.stderr, flush=True)

    # A timed-out attempt that was not really killed would write its `end` record
    # once its sleep finished; wait that long, then re-check every ledger.
    time.sleep(float(suite["defaults"]["timeout_sleep"]) + 1)
    for rec in recs:
        late = [e for f in (out_dir / "ledgers" / rec["workflow"]).glob("*.jsonl") for e in read_ledger(f)
                if e["event"] == "end" and e["kind"] == "timeout"
                and any(s["pid"] == e["pid"] and s.get("will_timeout") for s in read_ledger(f))]
        if late:
            rec["timeout_problems"].append(f"{len(late)} timed-out attempt(s) finished after being 'killed'")
    meta["eval_wall_seconds"] = time.monotonic() - t0

    report, summary = build_report(recs, meta)
    (out_dir / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "workflows": recs},
                                                     indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(report + "\n")
    print(report)
    print(f"\nresults: {out_dir}", file=sys.stderr)
    return 0
