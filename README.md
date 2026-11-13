# durable-agent-workflows

A small, single-machine durable execution engine for agent workflows, in
standard-library Python. A workflow is a list of named steps. After every step,
its output is committed to SQLite before the next step starts. If the worker is
`SIGKILL`ed, `daw resume <run-id>` continues from the checkpoint and **never
re-runs a step that was checkpointed**. Every step runs in a separate,
restricted subprocess with a timeout, retries and resource limits.

It is a demonstration of execution infrastructure, built small enough to read
in one sitting and tested by a frozen kill-test eval. It is not a product, and
not a replacement for Temporal, Inngest, Prefect, DBOS or Restate, which were
used as prior art for ideas only.

## Kill-test eval (frozen suite, 20 workflows)

20 scripted workflows (3–8 steps each, mixing instant, slow, fail-once,
always-fail and timeout steps) were written as data in
[`eval/workflows.json`](eval/workflows.json), hashed, and committed **before any
engine code existed** (commit `2e854a7`, the first commit). The harness refuses
to run if the file no longer matches
[`eval/workflows.sha256`](eval/workflows.sha256). No workflow was edited or
excluded.

For each workflow: a golden run without any kill, then the same workflow
`SIGKILL`ed at a predetermined point (also frozen in the JSON) under two
conditions. (a) **No-checkpoint baseline**: the same engine with an in-memory
store, so a crash loses everything and the restart begins at step 1.
(b) **Checkpointed engine**: kill, then `daw resume`. The eval makes zero model
calls.

Results from [`eval/results/run-2`](eval/results/run-2/report.md), 2026-11-13,
on Darwin arm64 with Python 3.12.14:

| Metric | (a) No-checkpoint baseline | (b) Checkpointed engine |
|---|---|---|
| Kills delivered mid-run | 20/20 | 20/20 |
| Runs completed after kill (reached a terminal state) | 20/20 | 20/20 |
| Resume success (terminal status = golden status) | 20/20 (100%) | 20/20 (100%) |
| Output match vs golden (status + every step's output) | 20/20 (100%) | 20/20 (100%) |
| **Redundant executions of already-completed steps** | 54 | **0** |
| Re-executions in the commit window (step had finished, checkpoint not yet committed) | n/a | 5 |
| Step executions after the kill (total) | 106 | 46 |
| Timeout kills handled correctly (process killed, then retried or failed per policy) | 45/45 | 37/37 |
| Median time to finish after the kill | 3.25 s | 1.62 s |
| Median uninterrupted wall-clock (per-workflow medians, 3 reps each) | 3.25 s | 3.25 s |
| Checkpointing overhead vs plain, median over workflows | — | +0.0% |
| …instant-only workflows (no sleeps hiding the cost) | — | +0.4% |

How the numbers are measured:

- **The kill** is `SIGKILL` to the worker's whole process group: the worker
  plus whichever tool subprocess was in flight. No handler runs and nothing is
  cleaned up.
- **Executions are counted outside the engine.** Each fake step appends `start`
  and `end` records to a ledger file itself, so the engine's database never
  grades its own work. A redundant execution means that, after the kill, a step
  started again even though it was already checkpointed (condition b), or had
  already finished (condition a).
- **Outputs are deterministic.** They depend only on the step's name, parameters
  and inputs, never on the attempt number, timing or process. Any deviation from
  the golden output would therefore show up as a mismatch.
- **A timeout counts as handled** only when the timed-out process never wrote
  its `end` record, even after waiting past its sleep (so it really died), and
  the engine then started the next attempt or failed the run on the last one.
- **Overhead** compares uninterrupted checkpointed runs with uninterrupted
  in-memory runs, interleaved, 3 reps each. Both conditions pay the same
  subprocess cost per step.

Per-workflow detail is in the
[run-2 report](eval/results/run-2/report.md): the kill point, how many steps
were checkpointed when the kill landed, which step was in flight, and the
recovery time. The raw data is in
[`results.json`](eval/results/run-2/results.json), the ledgers and worker logs
are next to it, and the report can be regenerated with `python3 -m daw eval`.

### What a checkpoint costs

The overhead row above is in the noise because a checkpoint is much cheaper
than the step around it. [`python3 -m daw.bench`](daw/bench.py) times the two
commits the engine makes per step
([`run-2/bench.md`](eval/results/run-2/bench.md)):

| What | Median | p95 |
|---|---|---|
| In-memory store (the baseline) | 0.01 ms | 0.01 ms |
| WAL + `synchronous=FULL` (engine default) | 0.08 ms | 2.81 ms |
| WAL + `synchronous=FULL` + `fullfsync` (`DAW_FULLFSYNC=1`) | 8.69 ms | 16.16 ms |
| One sandboxed no-op step (subprocess spawn, Python start-up, import) | 21.82 ms | 23.42 ms |

### Honest findings

1. **The hardest failure mode was the commit window, and the engine does not
   close it. No engine can on its own.** In 5 of 20 kills (all kill points on
   instant steps, with no delay) the step's process had already finished, but
   its checkpoint was not yet committed. On resume that step ran again. It was
   not "completed" by the engine's definition, so the zero above holds. Even
   so, it means the engine gives **exactly-once execution only for checkpointed
   steps, and at-least-once for the step in flight**. A step with external side
   effects must be idempotent. The demo's `write` step uses an atomic
   write-and-rename for this reason. This is the same contract that Temporal
   activities have.
2. **On macOS, cheap durability is partly an illusion.** `synchronous=FULL`
   costs about 0.08 ms per step there because macOS `fsync` does not flush the
   drive's write cache. That is enough for this eval, which only kills
   processes. It is not enough for power loss. With `fullfsync` it costs about
   8.7 ms per step, roughly 100 times more, though still under half the cost of
   spawning a step. This eval did not test power loss.
3. **Checkpointing paid for itself on every killed workflow that had progress
   to lose.** The baseline redid 54 completed step executions. The median time
   to finish after a kill halved, from 3.25 s to 1.62 s, and the gap is largest
   when the kill lands late (wf12: 8.42 s vs 2.12 s).

### Runs that were not the result

- A first full run was aborted after 4 workflows because of a harness bug, not
  an engine bug: a relative ledger path did not resolve inside each step's own
  working directory, so every step failed and the engine correctly failed every
  run. Those runs were deleted, and the harness now refuses to report anything
  if steps write no ledger.
- [`eval/results/run-1`](eval/results/run-1/report.md) is a complete run with
  the same headline numbers (0 redundant executions, 5 in the commit window, 54
  in the baseline, 20/20 output match). It was superseded because its
  per-workflow "in flight" column also listed timeout attempts the engine had
  already killed. The same logic slightly undercounted the timeout checks
  (40/32 instead of 45/37). That was fixed in the harness, not in the
  workflows, and the whole suite was rerun as run-2.
- After run-2, the engine gained the opt-in `resume --retry-failed` (for the
  live demo, see below). The default run and resume path that the eval
  exercises did not change.

## Architecture

```
 daw run / daw resume            (one worker process; the CLI is the only entry point)
        |
        v
 Engine loop  ------------------------------------>  SQLite file (WAL, synchronous=FULL)
   for each step, in order:                             runs:  id, workflow, status, input, spec,
     checkpointed?  -> reuse stored output                     result, error, owner_pid, timestamps
     otherwise:                                         steps: run_id, step_index, step_name, status,
       record "attempt N started" ------- commit --->          input, output, error, attempts, timestamps
       run step in sandbox
         ok        -> output + completed -- commit --> (the checkpoint; the next step waits for it)
         fail/timeout -> record error; back off (1s); retry
                         or, after max_attempts (3), fail step + run in one transaction
        |
        v
 Sandbox: subprocess `python -s -m daw.runner`
   own working directory (.daw/workspaces/<run>/<step>/attempt-N), minimal environment
   (PATH, HOME=cwd, locale; secrets only if the step declares them), Unix rlimits
   (CPU seconds, file size, open files, no core dumps), wall-clock timeout (30s default) -> kill.
   The step function ("module:function") gets JSON inputs on stdin and returns JSON on stdout;
   anything it prints goes to stderr. Exit code != 0 means the attempt failed (stderr is recorded).
```

The semantics the engine enforces:

- **Checkpoint before the next step.** A step's output and its `completed`
  status land in one SQLite transaction, and the next step starts only after it
  commits. WAL mode means a kill in the middle of a write leaves either the old
  state or the new one, never a corrupt file.
- **Resume never re-runs a checkpointed step.** It loads the run's stored
  workflow definition and input, reuses every completed output, and continues
  at the first step that is not completed.
- **An interrupted attempt counts toward `max_attempts`.** A step that crashes
  the worker every time therefore ends up failing the run instead of looping
  forever. Backoff is not persisted: after a crash, the next attempt starts
  immediately.
- **Failure is a state, not a crash.** When a step exhausts its attempts, the
  step and the run are marked `failed` together, with the last error recorded.
  `resume --retry-failed` is the explicit way to reopen such a run; it still
  never re-runs completed steps.
- **One worker per run.** A run records its owner PID, and `resume` refuses to
  take over a run whose owner is still alive unless you pass `--force`. The
  PID check only works on a single machine.

### Sandbox: what it is and what it is not

It is process isolation for reliability: a separate process, its own working
directory, a minimal environment, resource limits, captured stdout, stderr and
exit code, and a timeout that kills the process. Tests cover each of these
([`tests/test_sandbox.py`](tests/test_sandbox.py)).

**It is not a security boundary.** Step code runs as your user, can read your
files, can open network connections and can spawn processes. It is not
container-grade or VM-grade isolation, and it is not meant for running
untrusted or malicious code. On macOS, `RLIMIT_AS` (address space) is not
enforced, so it is off by default. On a timeout, only the step's process is
killed, not any grandchildren it may have spawned.

### Limits of scope

- **Single machine, single worker per run.** There is no distributed execution,
  no multi-worker scheduling, no queue and no leases beyond a local PID check.
- Steps run sequentially; there is no parallel fan-out.
- A step's inputs and outputs must be JSON and are stored in full in SQLite, so
  this is not suited to large payloads.
- Durability against power loss on macOS needs `DAW_FULLFSYNC=1` (see the cost
  table above). Power loss was not tested.

## Running it

Requires Python 3.10+ and has no dependencies. Tested on 3.12.14 and 3.10.21.

```bash
python3 -m daw workflows                          # what can be run by name
python3 -m daw run eval/wf04-slow-chain-5         # prints a run id; Ctrl-C or kill -9 it mid-way...
python3 -m daw list                               # ...it shows as `running`, k/5 steps checkpointed
python3 -m daw resume <run-id>                    # reuses those k outputs, continues at step k+1
python3 -m daw resume <run-id> --retry-failed     # reopen a FAILED run: fresh attempts for the failed step only
python3 -m daw show <run-id>                      # per-step status, attempts, timings, outputs, errors

python3 -m daw eval                               # the frozen kill-test suite (~11 min), prints the table
python3 -m daw.bench                              # checkpoint cost micro-benchmark
python3 -m unittest discover -s tests -t .        # 32 tests, ~11 s, no model calls
```

State lives in `.daw/daw.db` (or wherever `--db` / `DAW_DB` points). Running
`pip install -e .` adds a `daw` command, but nothing requires it.

### Demo agent: research brief

The demo is a five-step agent that fetches, extracts, drafts, critiques and
writes a file, all on the same engine ([`demo/`](demo/research_brief.py)).
`fetch` and `extract` are plain tool steps. `draft` and `critique` call a model.
`write` saves the brief atomically. Once a model step is checkpointed, a
resumed run reuses its output instead of asking the model again, so a crash
cannot change the brief.

```bash
# Offline and deterministic: bundled page (SQLite's public-domain WAL docs), fake model
DAW_LLM=fake python3 -m daw run demo/research-brief \
  --input '{"topic": "Write-ahead logging in SQLite", "url": "fixture:sqlite-wal.html", "out": "out/brief.md"}'

# Live: fetches https://sqlite.org/wal.html, calls a free OpenRouter model
export OPENROUTER_API_KEY=...            # DAW_MODEL overrides the default nvidia/nemotron-3.5-lightning:free
python3 -m daw run demo/research-brief
```

The demo refuses any model that is not a `:free` OpenRouter model. The demo is
not part of the kill-test numbers.

**What happened on the live run (2026-11-13).** The output is
[`demo/sample-brief.md`](demo/sample-brief.md). Getting there took three runs,
and all three failures are recorded in the database:

1. `google/gemma-4-31b-it:free` was rate-limited upstream (HTTP 429) on all 3
   attempts of `draft`. The engine failed the run cleanly.
2. With `nvidia/nemotron-3.5-lightning:free`, `draft` succeeded: 24.7 s, 1,702
   prompt tokens and 2,881 completion tokens, of which 2,388 were reasoning.
   Then `critique` returned empty content on all 3 attempts
   (`finish_reason=length`), because the model spent its whole output budget on
   reasoning. The run failed with `fetch`, `extract` and `draft` checkpointed.
3. After raising the output budget to 8,000 tokens, `daw resume <run-id>
   --retry-failed` reopened that same run. It reused the three checkpointed
   steps, so the draft was not regenerated, and gave `critique` a fresh attempt
   budget. Two attempts hit the length limit again, and the third succeeded:
   2,170 prompt tokens and 7,905 completion tokens, of which 7,595 were
   reasoning. That took 400 s in total across the three attempts. `write` then
   saved the brief.

The critique is useful. It flags two claims in the draft as unsupported or
contradicted by the source, and it names two omissions. It also shows the
reason for the demo: a "free" model's output is slow, capped and
nondeterministic, and the checkpoint is what kept the 25-second draft from
being thrown away when a later step failed.

`--retry-failed` is opt-in. Without it, a failed run stays failed.

## Cost

$0.00. The eval and the tests make no model calls. On the live demo run,
OpenRouter reported `cost: 0` on every successful model call; the attempts that
failed returned no usage data. Only `:free` models are allowed.

## Layout

```
daw/engine.py      the durable loop: checkpoint, resume, retry, timeout, fail cleanly
daw/store.py       SQLite schema and the transactions (runs, steps)
daw/sandbox.py     subprocess execution: cwd, env, rlimits, timeout, capture
daw/runner.py      subprocess entry point that calls a step function
daw/workflow.py    Step / Workflow definitions, validation, JSON round-trip
daw/cli.py         run, resume, list, show, eval, workflows
daw/killeval.py    kill-test harness and report
daw/bench.py       checkpoint cost micro-benchmark
daw/fakesteps.py   deterministic fake steps that write the execution ledger
eval/workflows.json    the 20 frozen workflows (data), with workflows.sha256
eval/results/          run-2 (reported), run-1 (superseded): report, results.json, ledgers, logs
demo/              research-brief agent, OpenRouter / fake model client, fixture page, sample-brief.md
tests/             engine, sandbox, store, frozen-suite tests (including a real SIGKILL + resume)
```
