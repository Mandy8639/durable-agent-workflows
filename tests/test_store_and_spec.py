import json
import tempfile
import unittest
from pathlib import Path

from daw import evalspec
from daw.store import Store
from daw.workflow import Step, Workflow


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp(prefix="daw-store-")) / "t.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()

    def test_wal_mode_and_schema(self):
        mode = self.store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        tables = {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"runs", "steps"})

    def test_checkpoint_visible_to_other_connections(self):
        self.store.create_run("r1", "wf", {"steps": []}, {"x": 1})
        self.store.start_attempt("r1", 0, "a", {"in": 1}, 1)
        self.store.complete_step("r1", 0, {"out": 2})
        other = Store(self.path)
        steps = other.get_steps("r1")
        other.close()
        self.assertEqual(steps[0]["status"], "completed")
        self.assertEqual(steps[0]["output"], {"out": 2})

    def test_final_failure_marks_step_and_run_together(self):
        self.store.create_run("r1", "wf", {"steps": []}, None)
        self.store.start_attempt("r1", 0, "a", {}, 1)
        self.store.fail_attempt("r1", 0, "boom", final=True)
        self.assertEqual(self.store.get_run("r1")["status"], "failed")
        self.assertEqual(self.store.get_steps("r1")[0]["status"], "failed")


class WorkflowTests(unittest.TestCase):
    def test_spec_round_trip(self):
        wf = Workflow("w", (Step("a", "m:f", inputs=("input",)), Step("b", "m:f", inputs=("a",), timeout=5)))
        self.assertEqual(Workflow.from_spec(json.loads(json.dumps(wf.to_spec()))), wf)

    def test_validation(self):
        with self.assertRaises(ValueError):
            Workflow("w", ())
        with self.assertRaises(ValueError):
            Workflow("w", (Step("a", "m:f"), Step("a", "m:f")))
        with self.assertRaises(ValueError):
            Workflow("w", (Step("a", "m:f", inputs=("later",)), Step("later", "m:f")))
        with self.assertRaises(ValueError):
            Workflow("w", (Step("a", "nofunction"),))


class FrozenSuiteTests(unittest.TestCase):
    def test_suite_matches_frozen_hash(self):
        suite = evalspec.load_suite()
        self.assertEqual(len(suite["workflows"]), 20)

    def test_all_workflows_build_with_3_to_8_steps(self):
        for spec, wf in evalspec.eval_workflows():
            self.assertTrue(3 <= len(wf.steps) <= 8, spec["name"])
            self.assertIn(spec["kill"]["step"], [s.name for s in wf.steps])

    def test_modified_suite_is_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        edited = tmp / "workflows.json"
        edited.write_text(evalspec.SUITE_PATH.read_text().replace('"delay": 0.0', '"delay": 0.1', 1))
        with self.assertRaises(evalspec.SuiteModified):
            evalspec.load_suite(edited, evalspec.HASH_PATH)


if __name__ == "__main__":
    unittest.main()
