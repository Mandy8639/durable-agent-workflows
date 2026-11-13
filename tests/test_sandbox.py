import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from daw.sandbox import Limits, run_tool


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="daw-sbx-")) / "work"

    def test_captures_stdout_and_exit_code(self):
        res = run_tool(py("print('hello')"), cwd=str(self.cwd), timeout=10)
        self.assertTrue(res.ok)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.stdout.strip(), "hello")

    def test_nonzero_exit_is_failure_with_stderr(self):
        res = run_tool(py("import sys; sys.stderr.write('boom'); sys.exit(3)"), cwd=str(self.cwd), timeout=10)
        self.assertEqual(res.status, "error")
        self.assertEqual(res.exit_code, 3)
        self.assertIn("boom", res.describe())

    def test_timeout_kills_process(self):
        pidfile = self.cwd.parent / "pid"
        code = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(30)"
        t0 = time.monotonic()
        res = run_tool(py(code), cwd=str(self.cwd), timeout=0.5)
        self.assertEqual(res.status, "timeout")
        self.assertLess(time.monotonic() - t0, 5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_own_working_directory_and_minimal_env(self):
        os.environ["DAW_SBX_SECRET"] = "x"
        try:
            res = run_tool(py("import os; print(os.getcwd()); print(sorted(os.environ))"),
                           cwd=str(self.cwd), timeout=10)
        finally:
            del os.environ["DAW_SBX_SECRET"]
        cwd_line, env_line = res.stdout.strip().splitlines()
        self.assertEqual(os.path.realpath(cwd_line), os.path.realpath(self.cwd))
        self.assertNotIn("DAW_SBX_SECRET", env_line)
        self.assertNotIn("SSH_AUTH_SOCK", env_line)

    def test_explicit_env_is_passed(self):
        res = run_tool(py("import os; print(os.environ['ONLY_THIS'])"), cwd=str(self.cwd), timeout=10,
                       env={"ONLY_THIS": "yes"})
        self.assertEqual(res.stdout.strip(), "yes")

    @unittest.skipIf(sys.platform == "win32", "Unix resource limits")
    def test_resource_limits_applied(self):
        limits = Limits(file_size_bytes=1024 * 1024, open_files=64)
        code = ("import resource as r; print(r.getrlimit(r.RLIMIT_NOFILE)[0], r.getrlimit(r.RLIMIT_FSIZE)[0],"
                " r.getrlimit(r.RLIMIT_CPU)[0], r.getrlimit(r.RLIMIT_CORE)[0])")
        res = run_tool(py(code), cwd=str(self.cwd), timeout=4, limits=limits)
        self.assertEqual(res.stdout.split(), ["64", str(1024 * 1024), "5", "0"])

    @unittest.skipIf(sys.platform == "win32", "Unix resource limits")
    def test_file_size_limit_enforced(self):
        limits = Limits(file_size_bytes=1024 * 1024)
        code = "open('big','wb').write(b'x' * (4 * 1024 * 1024))"
        res = run_tool(py(code), cwd=str(self.cwd), timeout=10, limits=limits)
        self.assertFalse(res.ok)
        self.assertLessEqual((self.cwd / "big").stat().st_size, 1024 * 1024)

    @unittest.skipIf(sys.platform == "win32", "Unix resource limits")
    def test_cpu_limit_stops_busy_loop(self):
        limits = Limits(cpu_seconds=1)
        res = run_tool(py("while True: pass"), cwd=str(self.cwd), timeout=20, limits=limits)
        self.assertFalse(res.ok)
        self.assertNotEqual(res.status, "timeout")
        self.assertLess(res.duration, 10)


if __name__ == "__main__":
    unittest.main()
