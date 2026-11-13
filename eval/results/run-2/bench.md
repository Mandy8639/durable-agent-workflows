Checkpoint cost per step (2 commits), Darwin arm64, Python 3.12.14, 2026-11-13 15:52:01 EST:

| What | Median | p95 | n |
|---|---|---|---|
| in-memory (no-checkpoint baseline) | 0.01 ms | 0.01 ms | 500 |
| WAL + synchronous=FULL (engine default) | 0.08 ms | 2.81 ms | 500 |
| WAL + synchronous=FULL + fullfsync | 8.69 ms | 16.16 ms | 500 |
| sandboxed no-op step (subprocess spawn) | 21.82 ms | 23.42 ms | 50 |
