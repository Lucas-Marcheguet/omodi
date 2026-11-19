"""Single entry point: omodi extract|compare|stats|impact [options...]."""
from __future__ import annotations

import sys

from . import __version__

COMMANDS = {
    "extract": ("extract", "extract the definition of Odoo models / modules"),
    "compare": ("compare", "compare two extractions"),
    "stats": ("migration_stats", "migration stats for a list of modules"),
    "impact": ("impact", "what to watch out for when upgrading custom modules"),
}


def _usage():
    lines = ["usage: omodi {extract,compare,stats,impact} [options...]", "", "commands:"]
    lines += [f"  {name:<8} {desc}" for name, (_, desc) in COMMANDS.items()]
    lines += ["",
              "stats / impact options common to both:",
              "  -o [DIR]      also write files to DIR (default ./omodi-output); without -o, console only",
              "  -f FORMAT     json, csv or all (default) - file format written with -o; the colored",
              "                report is always printed on the console (NO_COLOR=1: no colors)",
              "  -q            no progress display",
              "",
              "omodi <command> --help for the options of a command; --version for the version."]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return
    if argv[0] == "--version":
        print(f"omodi {__version__}")
        return
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n\n{_usage()}", file=sys.stderr)
        sys.exit(2)
    import importlib
    module = importlib.import_module(f".{COMMANDS[cmd][0]}", __package__)
    module.main(argv[1:])


if __name__ == "__main__":
    main()
