#!/usr/bin/env python3
"""Run a command under a pty, feeding it scripted answers at each prompt.

The numbered fallback in `_claude_profile_pick` reads from /dev/tty and only
engages when stderr is a terminal — both are deliberate (see the comments
there), and both make it untestable from a plain pipe. This driver gives the
child a real terminal and types into it, so the fallback can be tested the
same way a user drives it.

Sends are triggered by the prompt itself rather than by sleeping: the Nth
occurrence of --prompt-re in the output so far releases the Nth --send line.
That makes a re-prompt (invalid answer → "enter 1-N" → prompt again) an
ordinary trigger and keeps the whole thing deterministic — no timing races,
identical on macOS and Linux (unlike `script`, whose flags differ).

Usage:
  pty_run.py --prompt-re 'profile> ' --send 2 -- zsh -c '…'

Stdout is the child's combined output (\\r stripped); exit status is the
child's.
"""
import argparse
import os
import pty
import re
import select
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-re", required=True,
                    help="regex whose Nth match releases the Nth --send line")
    ap.add_argument("--send", action="append", default=[],
                    help="a line to type at a prompt (repeatable, in order)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        ap.error("no command given")

    pid, fd = pty.fork()
    if pid == 0:                                  # child: becomes the session
        os.execvp(cmd[0], cmd)                    # leader with fd as its tty
        os._exit(127)

    prompt = re.compile(args.prompt_re)
    buf, sent, deadline = "", 0, args.timeout
    while True:
        r, _, _ = select.select([fd], [], [], deadline)
        if not r:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:                           # EIO = child closed the pty
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        # Each new prompt occurrence releases the next queued answer.
        while sent < len(args.send) and len(prompt.findall(buf)) > sent:
            os.write(fd, (args.send[sent] + "\n").encode())
            sent += 1

    os.close(fd)
    _, status = os.waitpid(pid, 0)
    sys.stdout.write(buf.replace("\r", ""))
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
