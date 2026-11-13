"""Run tool code in a separate, restricted subprocess.

What this gives you: a separate process, its own working directory, a minimal
environment (no inherited secrets unless passed explicitly), Unix resource
limits where the platform honours them, a wall-clock timeout that kills the
process, and captured stdout / stderr / exit code.

What it does NOT give you: a security boundary. Code in the subprocess runs as
the same user, can read the same files, and can open network connections.
This is process isolation for reliability, not containment of malicious code.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass, field

try:
    import resource
except ImportError:  # non-Unix
    resource = None  # type: ignore[assignment]

STDERR_TAIL = 4000
MiB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    cpu_seconds: int | None = None  # None: derived from the wall-clock timeout
    file_size_bytes: int | None = 64 * MiB
    open_files: int | None = 256
    address_space_bytes: int | None = None  # off by default: macOS does not enforce it
    core_dumps: bool = False


@dataclass
class ToolResult:
    status: str  # ok | error | timeout
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    limits_applied: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "timeout":
            return f"timeout after {self.duration:.2f}s; process killed"
        if self.status == "error":
            tail = self.stderr.strip()[-STDERR_TAIL:]
            return f"exit code {self.exit_code}: {tail}" if tail else f"exit code {self.exit_code}"
        return "ok"


def minimal_env(cwd: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": cwd,
        "TMPDIR": cwd,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if extra:
        env.update(extra)
    return env


def _rlimits(limits: Limits, timeout: float) -> list[tuple[str, int]]:
    if resource is None:
        return []
    cpu = limits.cpu_seconds if limits.cpu_seconds is not None else math.ceil(timeout) + 1
    wanted = [
        ("RLIMIT_CPU", cpu),
        ("RLIMIT_FSIZE", limits.file_size_bytes),
        ("RLIMIT_NOFILE", limits.open_files),
        ("RLIMIT_AS", limits.address_space_bytes),
        ("RLIMIT_CORE", None if limits.core_dumps else 0),
    ]
    return [(name, value) for name, value in wanted if value is not None and hasattr(resource, name)]


def _preexec(pairs: list[tuple[str, int]]):
    def apply() -> None:
        os.umask(0o077)
        for name, value in pairs:
            res = getattr(resource, name)
            try:
                _, hard = resource.getrlimit(res)
                if hard != resource.RLIM_INFINITY:
                    value = min(value, hard)
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):
                pass  # best effort: some limits are not settable on some platforms

    return apply


def run_tool(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    stdin_data: bytes | None = None,
    env: dict[str, str] | None = None,
    limits: Limits = Limits(),
) -> ToolResult:
    os.makedirs(cwd, exist_ok=True)
    pairs = _rlimits(limits, timeout)
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=minimal_env(cwd, env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        preexec_fn=_preexec(pairs) if pairs else None,
    )
    try:
        out, err = proc.communicate(input=stdin_data, timeout=timeout)
        status = "ok" if proc.returncode == 0 else "error"
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a grandchild is holding the pipes open
            out, err = b"", b""
        status = "timeout"
    return ToolResult(
        status=status,
        exit_code=proc.returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace")[-STDERR_TAIL:],
        duration=time.monotonic() - start,
        limits_applied=[name for name, _ in pairs],
    )
