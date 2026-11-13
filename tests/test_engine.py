import json
import os
import signal
import subprocess
import sys
import time
import unittest

from daw.engine import PROJECT_ROOT, Engine
from daw.store import RunLocked
from daw.workflow import Step, Workflow

from tests.helpers import EngineCase, chain, fake


class RunTests(EngineCase):
    def test_linear_run_completes_and_executes_each_step_once(self):
        wf = chain("t/linear", fake("a"), fake("b"), fake("c"))
        res = self.engine.run(wf, {"x": 1})
        self.assertEqual(res.status, "completed")
        self.assertEqual(list(res.outputs), ["a", "b", "c"])
        self.assertEqual(res.result, res.outputs["c"])
        self.assertEqual(self.executions(), {"a": 1, "b": 1, "c": 1})

    def test_outputs_are_deterministic_across_runs(self):
        wf = chain("t/det", fake("a"), fake("b", "fail_once"), fake("c"))
        r1 = self.engine.run(wf, {"x": 1})
        r2 = self.engine.run(wf, {"x": 1})
        r3 = self.engine.run(wf, {"x": 2})
        self.assertEqual(r1.outputs, r2.outputs)
        self.assertNotEqual(r1.outputs["c"], r3.outputs["c"])

    def test_fail_once_is_retried(self):
        wf = chain("t/retry", fake("a", "fail_once"), fake("b"))
        res = self.engine.run(wf)
        self.assertEqual(res.status, "completed")
        steps = self.store.get_steps(res.run_id)
        self.assertEqual(steps[0]["attempts"], 2)
        self.assertIn("fail_once", steps[0]["error"])

    def test_always_fail_fails_run_cleanly(self):
        wf = chain("t/fail", fake("a"), fake("b", "always_fail", max_attempts=2), fake("c"))
        res = self.engine.run(wf)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.failed_step, "b")
        self.assertIn("used all 2 attempts", res.error)
        self.assertEqual(self.executions(), {"a": 1, "b": 2})
        steps = self.store.get_steps(res.run_id)
        self.assertEqual([s["status"] for s in steps], ["completed", "failed"])

    def test_timeout_kills_and_retries(self):
        wf = chain("t/timeout", fake("a", "timeout", sleep=5.0, timeout=0.5))
        t0 = time.monotonic()
        res = self.engine.run(wf)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(res.status, "completed")
        self.assertEqual(self.store.get_steps(res.run_id)[0]["attempts"], 2)
        timed_out = [e for e in self.events("start") if e.get("will_timeout")]
        self.assertEqual(len(timed_out), 1)
        time.sleep(0.2)
        ended = {e["pid"] for e in self.events("end")}
        self.assertNotIn(timed_out[0]["pid"], ended)

    def test_timeout_exhausting_attempts_fails_run(self):
        wf = chain("t/timeout-fail", fake("a", "timeout", sleep=5.0, timeout=0.3, bad_attempts=9, max_attempts=2))
        res = self.engine.run(wf)
        self.assertEqual(res.status, "failed")
        self.assertIn("timeout", res.error)

    def test_non_json_output_fails_step(self):
        wf = Workflow("t/nonjson", (Step("a", "tests.stepfns:not_json", max_attempts=1),))
        res = self.engine.run(wf)
        self.assertEqual(res.status, "failed")
        self.assertIn("JSON", res.error)

    def test_secrets_are_not_inherited_unless_declared(self):
        os.environ["DAW_TEST_SECRET"] = "hunter2"
        try:
            wf = Workflow("t/env", (
                Step("hidden", "tests.stepfns:read_env", params={"var": "DAW_TEST_SECRET"}),
                Step("passed", "tests.stepfns:read_env", params={"var": "DAW_TEST_SECRET"},
                     env=("DAW_TEST_SECRET",)),
            ))
            res = self.engine.run(wf)
        finally:
            del os.environ["DAW_TEST_SECRET"]
        self.assertIsNone(res.outputs["hidden"])
        self.assertEqual(res.outputs["passed"], "hunter2")


class ResumeTests(EngineCase):
    def test_resume_completed_run_is_a_no_op(self):
        wf = chain("t/done", fake("a"), fake("b"))
        res = self.engine.run(wf)
        again = self.engine.execute(res.run_id)
        self.assertEqual(again.status, "completed")
        self.assertEqual(self.executions(), {"a": 1, "b": 1})

    def test_resume_reuses_checkpoints_and_never_reexecutes(self):
        wf = chain("t/partial", fake("a"), fake("b"), fake("c"), fake("d"))
        run_id = self.engine.start(wf, {"x": 1})
        # Simulate a crash after b's checkpoint and during c's first attempt.
        for idx, name in enumerate(["a", "b"]):
            self.store.start_attempt(run_id, idx, name, {}, 1)
            self.store.complete_step(run_id, idx, {"stored": name})
        self.store.start_attempt(run_id, 2, "c", {}, 1)
        res = self.engine.execute(run_id)
        self.assertEqual(res.status, "completed")
        self.assertEqual(self.executions(), {"c": 1, "d": 1})
        self.assertEqual(res.outputs["a"], {"stored": "a"})
        self.assertEqual(self.store.get_steps(run_id)[2]["attempts"], 2)

    def test_interrupted_attempt_counts_toward_max_attempts(self):
        wf = chain("t/poison", fake("a", max_attempts=2))
        run_id = self.engine.start(wf)
        self.store.start_attempt(run_id, 0, "a", {}, 2)  # died during its last attempt
        res = self.engine.execute(run_id)
        self.assertEqual(res.status, "failed")
        self.assertIn("interrupted", res.error)
        self.assertEqual(self.executions(), {})

    def test_failed_run_stays_failed_unless_retry_failed(self):
        wf = chain("t/reopen", fake("a"), fake("b", "always_fail", max_attempts=2), fake("c"))
        res = self.engine.run(wf)
        self.assertEqual(self.engine.execute(res.run_id).status, "failed")
        self.assertEqual(self.executions(), {"a": 1, "b": 2})
        again = self.engine.execute(res.run_id, retry_failed=True)
        self.assertEqual(again.status, "failed")
        self.assertEqual(self.executions(), {"a": 1, "b": 4})
        self.assertEqual(self.store.get_steps(res.run_id)[1]["attempts"], 2)

    def test_retry_failed_reuses_completed_steps(self):
        wf = chain("t/reopen-ok", fake("a"), fake("b"), fake("c"))
        run_id = self.engine.start(wf)
        self.store.start_attempt(run_id, 0, "a", {}, 1)
        self.store.complete_step(run_id, 0, {"stored": "a"})
        self.store.start_attempt(run_id, 1, "b", {}, 3)
        self.store.fail_attempt(run_id, 1, "boom", final=True)
        res = self.engine.execute(run_id, retry_failed=True)
        self.assertEqual(res.status, "completed")
        self.assertIsNone(res.error)
        self.assertEqual(res.outputs["a"], {"stored": "a"})
        self.assertEqual(self.executions(), {"b": 1, "c": 1})

    def test_resume_refuses_while_owner_is_alive(self):
        wf = chain("t/locked", fake("a"))
        run_id = self.engine.start(wf)
        self.store.conn.execute("UPDATE runs SET status='running', owner_pid=? WHERE id=?",
                                (os.getppid(), run_id))
        with self.assertRaises(RunLocked):
            self.engine.execute(run_id)
        self.assertEqual(self.engine.execute(run_id, force=True).status, "completed")

    def test_checkpoint_mismatch_is_detected(self):
        wf = chain("t/mismatch", fake("a"), fake("b"))
        run_id = self.engine.start(wf)
        self.store.start_attempt(run_id, 0, "zzz", {}, 1)
        with self.assertRaises(RuntimeError):
            self.engine.execute(run_id)


class RealKillTest(EngineCase):
    """SIGKILL a real worker process mid-step, then resume through the CLI."""

    def test_sigkill_then_resume(self):
        env = dict(os.environ, DAW_LEDGER=str(self.ledger), PYTHONPATH=str(PROJECT_ROOT))
        cli = [sys.executable, "-m", "daw"]
        proc = subprocess.Popen(cli + ["run", "eval/wf04-slow-chain-5", "--run-id", "k1", "--db", str(self.db)],
                                cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(e["step"] == "s3" for e in self.events("start")):
                break
            time.sleep(0.01)
        time.sleep(0.5)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

        before = self.executions()
        self.assertEqual(before, {"s1": 1, "s2": 1, "s3": 1})
        done = subprocess.run(cli + ["resume", "k1", "--db", str(self.db), "--json"], cwd=PROJECT_ROOT,
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.executions(), {"s1": 1, "s2": 1, "s3": 2, "s4": 1, "s5": 1})


if __name__ == "__main__":
    unittest.main()
