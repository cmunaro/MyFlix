#!/usr/bin/env python3
"""Run an engine media tool with a process-wide, crash-safe concurrency limit."""
import argparse
import fcntl
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--slots", type=int, required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if not 1 <= options.slots <= 10:
        parser.error("Processing slots must be between 1 and 10")
    arguments = options.args[1:] if options.args[:1] == ["--"] else options.args
    directory = Path(options.directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        for index in range(options.slots):
            descriptor = os.open(directory / f"process-{index}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            # exec preserves the PID used by upstream cancellation. The lock
            # survives exec and is released by the kernel on exit or SIGKILL.
            os.set_inheritable(descriptor, True)
            os.execv(options.binary, [options.binary, *arguments])
        time.sleep(0.1)


if __name__ == "__main__":
    main()
