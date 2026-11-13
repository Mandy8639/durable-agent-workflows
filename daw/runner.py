"""Subprocess entry point: `python -m daw.runner`, payload as JSON on stdin.

Imports the step function, calls it, and writes its JSON output to stdout.
Anything the step prints goes to stderr, so stdout carries only the result.
Exit code 0 means success; anything else is a failed attempt.
"""

from __future__ import annotations

import importlib
import json
import sys
import traceback

from daw.context import StepContext, StepError


def resolve(ref: str):
    module_name, _, attr = ref.partition(":")
    obj = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def main() -> int:
    payload = json.loads(sys.stdin.buffer.read())
    result_stream = sys.stdout
    sys.stdout = sys.stderr
    try:
        fn = resolve(payload["fn"])
        ctx = StepContext(**payload["ctx"])
        result = fn(payload["inputs"], payload["params"], ctx)
        text = json.dumps(result, sort_keys=True)
    except StepError as e:
        print(f"StepError: {e}", file=sys.stderr)
        return 1
    except TypeError as e:
        if "JSON serializable" in str(e):
            print(f"step output is not JSON-serializable: {e}", file=sys.stderr)
            return 2
        traceback.print_exc()
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    result_stream.write(text)
    result_stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
