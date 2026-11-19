"""Shared CLI helpers: output options (-o / --format / --quiet) and progress reporting."""
from __future__ import annotations

import sys

DEFAULT_OUTPUT = "omodi-output"
FORMATS = ("json", "csv", "all")


def add_output_args(parser, what):
    """Add -o/--output [DIR], -f/--format and -q/--quiet to an argparse parser.

    Without -o nothing is written: the Markdown/console report is the only output.
    -o without a value writes into DEFAULT_OUTPUT.
    """
    parser.add_argument("-o", "--output", nargs="?", const=DEFAULT_OUTPUT, default=None, metavar="DIR",
                        help=f"also write {what} to files in DIR (default DIR when no value: ./{DEFAULT_OUTPUT}); "
                             "without -o, only the console report is produced")
    parser.add_argument("-f", "--format", choices=FORMATS, default=None,
                        help="file format(s) written with -o (default: all). The colored report is "
                             "always printed on the console, never written to a file")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not display the progress")


def check_output_args(parser, args):
    """-f only makes sense with -o."""
    if args.format is not None and args.output is None:
        parser.error("--format needs -o/--output (without -o, nothing is written to files)")


def wants(args, fmt):
    """True when files of format `fmt` must be written."""
    return args.output is not None and args.format in (None, fmt, "all")


class Progress:
    """Progress reporting on stderr (stdout stays reserved for the report).

    On a terminal the line is refreshed in place; otherwise one line is printed per ~10 %.
    """

    def __init__(self, enabled=True, stream=None):
        self.stream = stream or sys.stderr
        self.enabled = enabled
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()

    def task(self, label, total):
        """Return update(done, name="") for a stage of `total` steps; the stage is closed at done == total."""
        total = max(total, 0)
        state = {"last": -1, "width": 0}

        def update(done, name=""):
            if not self.enabled:
                return
            pct = 100 if total == 0 else int(done * 100 / total)
            finished = done >= total
            if self.tty:
                text = f"{label} [{done}/{total}] {pct:>3}%" + (f" {name}" if name and not finished else "")
                self.stream.write("\r" + text.ljust(state["width"]) + ("\n" if finished else ""))
                state["width"] = 0 if finished else max(state["width"], len(text))
            elif finished or pct // 10 != state["last"] // 10:
                self.stream.write(f"{label} [{done}/{total}] {pct}%\n")
            state["last"] = pct
            self.stream.flush()

        if total == 0:
            update(0)
        return update

    def info(self, message):
        if self.enabled:
            print(message, file=self.stream)
