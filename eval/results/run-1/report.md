Suite `kill-test-v1` (sha256 `7a4268899c385d0f…`), 20 workflows, 3 uninterrupted reps per condition, 2026-11-13 15:29:05 EST, Darwin arm64, Python 3.12.14.

| Metric | (a) No-checkpoint baseline | (b) Checkpointed engine |
|---|---|---|
| Kills delivered mid-run | 20/20 (100%) | 20/20 (100%) |
| Runs completed after kill (reached a terminal state) | 20/20 (100%) | 20/20 (100%) |
| Resume success (terminal status = golden status) | 20/20 (100%) | 20/20 (100%) |
| Output match vs golden (status + every step output) | 20/20 (100%) | 20/20 (100%) |
| Redundant executions of already-completed steps | 54 | 0 |
| Re-executions in the commit window (step finished, checkpoint not yet committed) | n/a | 5 |
| Step executions after the kill (total) | 106 | 46 |
| Timeout kills handled correctly (killed + retried/failed per policy) | 40/40 (100%) | 32/32 (100%) |
| Median recovery wall-clock after kill | 3.25s | 1.62s |
| Median uninterrupted wall-clock (per-workflow medians) | 3.27s | 3.27s |
| Checkpointing overhead vs plain, median over workflows | — | -0.1% |
| …same, instant-only workflows (no sleeps to hide the cost) | — | +0.5% |
| Median extra wall-clock per step | — | -0.4 ms |

Per workflow (kill point; state when killed; result after recovery):

| Workflow | Steps | Kill point | Golden | Ckpt at kill | In flight | (b) match | (b) redundant | (b) window | (a) redundant | (a) recovery | (b) recovery | Overhead |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| wf01-instant-3 | 3 | start c#1 +0.0s | completed | 2/3 | - | yes | 0 | 1 | 3 | 0.10s | 0.05s | +0.6% |
| wf02-instant-8 | 8 | start s5#1 +0.0s | completed | 4/8 | - | yes | 0 | 1 | 5 | 0.22s | 0.12s | +0.3% |
| wf03-slow-middle | 3 | start b#1 +1.0s | completed | 1/3 | b | yes | 0 | 0 | 1 | 2.16s | 2.14s | -0.1% |
| wf04-slow-chain-5 | 5 | start s3#1 +1.0s | completed | 2/5 | s3 | yes | 0 | 0 | 2 | 6.31s | 4.21s | -0.3% |
| wf05-fail-once-first | 4 | start s4#1 +1.0s | completed | 3/4 | s4 | yes | 0 | 0 | 4 | 3.26s | 2.11s | -0.2% |
| wf06-kill-in-backoff | 4 | end s2#1 +0.3s | completed | 1/4 | - | yes | 0 | 0 | 1 | 1.24s | 0.13s | +0.3% |
| wf07-kill-during-timeout | 4 | start s2#1 +0.5s | completed | 1/4 | s2 | yes | 0 | 0 | 1 | 2.21s | 0.12s | +0.2% |
| wf08-kill-after-timeout-retry | 3 | start s2#2 +0.0s | completed | 1/3 | s2 | yes | 0 | 1 | 3 | 6.22s | 2.11s | -0.2% |
| wf09-always-fail-last | 3 | start s3#2 +0.2s | failed | 2/3 | - | yes | 0 | 0 | 2 | 4.31s | 0.09s | +1.5% |
| wf10-always-fail-first | 3 | end s1#1 +0.5s | failed | 0/3 | - | yes | 0 | 0 | 0 | 2.20s | 1.13s | -0.1% |
| wf11-mixed-6 | 6 | start s4#1 +0.0s | completed | 3/6 | - | yes | 0 | 1 | 5 | 5.34s | 2.15s | -0.1% |
| wf12-mixed-7 | 7 | start s5#1 +0.0s | completed | 4/7 | - | yes | 0 | 0 | 5 | 8.45s | 2.13s | -0.1% |
| wf13-mixed-8 | 8 | start s7#1 +1.5s | completed | 6/8 | s4,s7 | yes | 0 | 0 | 8 | 7.43s | 2.14s | -0.3% |
| wf14-fan-in | 4 | start c#1 +0.5s | completed | 2/4 | c | yes | 0 | 0 | 2 | 2.18s | 2.14s | -0.1% |
| wf15-kill-near-end | 4 | start s4#1 +1.9s | completed | 3/4 | s4 | yes | 0 | 0 | 3 | 4.21s | 2.10s | -0.1% |
| wf16-kill-first-step | 3 | start s1#1 +0.5s | completed | 0/3 | s1 | yes | 0 | 0 | 0 | 2.17s | 2.17s | +0.2% |
| wf17-always-fail-middle | 5 | start s3#1 +0.0s | failed | 2/5 | - | yes | 0 | 0 | 3 | 3.25s | 1.11s | +0.2% |
| wf18-double-timeout | 3 | start s2#2 +0.5s | completed | 1/3 | s1,s2 | yes | 0 | 0 | 2 | 6.23s | 0.10s | -0.1% |
| wf19-timeout-exhausts | 3 | start s2#2 +0.5s | failed | 1/3 | s2 | yes | 0 | 0 | 1 | 5.14s | 1.08s | +0.1% |
| wf20-commit-window | 6 | end s3#1 +0.0s | completed | 2/6 | - | yes | 0 | 1 | 3 | 0.17s | 0.12s | +0.5% |

Timeout-handling problems: none. Workflows whose golden status differed from the frozen expectation or whose uninterrupted runs disagreed: none.
