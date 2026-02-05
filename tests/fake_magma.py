#!/usr/bin/env python3
"""Fake Magma script for integration tests.

Mimics Magma's I/O behavior when run with piped stdin and -n flag:
  - Prints a banner line with version/seed
  - Processes each stdin line
  - Prints footer with timing/memory info

NOTE: Uses eval() intentionally — this is a test-only script that simulates
a computer algebra system. It is never exposed to untrusted input.
"""
import random
import re
import sys
import time
from datetime import datetime

# Randomize seed per invocation (like real Magma)
SEED = random.randint(1, 2**32 - 1)
VERSION = random.choice(["2.28-1", "2.29-3", "2.29-5"])
HOSTNAME = random.choice(["fake", "test", "mock"])


def make_banner() -> str:
    timestamp = datetime.now().strftime("%a %b %d %Y %H:%M:%S")
    return f"Magma V{VERSION}     {timestamp} on {HOSTNAME}    [Seed = {SEED}]"


def make_footer(elapsed: float, memory_mb: float) -> str:
    return f"Total time: {elapsed:.3f} seconds, Total memory usage: {memory_mb:.2f}MB"

_RE_ALARM = re.compile(r"^Alarm\(\d+\);$")
_RE_ASSIGN = re.compile(r"^(\w+)\s*:=\s*(.+);$")
_RE_PRINT = re.compile(r"^print\s+(.+);$")

env: dict[str, object] = {}


def evaluate(expr: str) -> object:
    """Evaluate a simple expression with variable substitution."""
    for name, val in env.items():
        expr = re.sub(rf"\b{name}\b", repr(val), expr)
    return eval(expr)  # noqa: S307 — test-only, no untrusted input


def main() -> None:
    # Buffer output lines; real Magma prints them after "quit."
    output_lines: list[str] = []
    start_time = time.time()

    print(make_banner())

    for line in sys.stdin:
        line = line.strip()

        if not line or line == ";":
            continue

        if _RE_ALARM.match(line):
            continue

        if line == "SetIgnorePrompt(true);":
            continue

        if line == "quit;":
            elapsed = time.time() - start_time
            memory_mb = random.uniform(10.0, 50.0)
            print("quit.")
            for out in output_lines:
                print(out)
            print(make_footer(elapsed, memory_mb))
            sys.exit(0)

        if "while true" in line.lower():
            time.sleep(999)
            continue

        m = _RE_ASSIGN.match(line)
        if m:
            name, expr = m.group(1), m.group(2)
            try:
                env[name] = evaluate(expr)
            except Exception as e:
                output_lines.append(f"User error: {e}")
            continue

        m = _RE_PRINT.match(line)
        if m:
            expr = m.group(1)
            try:
                output_lines.append(str(evaluate(expr)))
            except Exception as e:
                output_lines.append(f"User error: {e}")
            continue

        # Unrecognized line — try to evaluate
        try:
            result = evaluate(line.rstrip(";"))
            output_lines.append(str(result))
        except Exception as e:
            output_lines.append(f"User error: {e}")


if __name__ == "__main__":
    main()
