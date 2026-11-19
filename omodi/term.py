"""Console rendering helpers: ANSI colors (auto-disabled off a terminal), badges, bars, aligned tables.

Colors are on when stdout is a terminal; NO_COLOR disables them, FORCE_COLOR enables them.
Everything degrades to plain aligned text when colors are off.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
          "magenta": "35", "cyan": "36", "gray": "90", "white": "97"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def color_enabled(stream=None):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text, *styles, on=True):
    """Wrap `text` in the given styles (names of _CODES) when `on`."""
    if not on or not styles:
        return str(text)
    return f"\x1b[{';'.join(_CODES[s] for s in styles)}m{text}\x1b[0m"


def visible_len(text):
    return len(_ANSI.sub("", text))


def pad(text, width, right=False):
    """Pad to `width` visible characters (ANSI codes do not count)."""
    gap = max(width - visible_len(text), 0)
    return " " * gap + text if right else text + " " * gap


def truncate(text, width):
    return text if len(text) <= width else text[:max(width - 1, 1)] + "…"


def term_width(default=100):
    return max(60, min(shutil.get_terminal_size((default, 24)).columns, 140))


class Style:
    """Bound to a color on/off switch; every method returns a string."""

    def __init__(self, on=None):
        self.on = color_enabled() if on is None else on

    def __call__(self, text, *styles):
        return paint(text, *styles, on=self.on)

    def bar(self, parts, width=24):
        """Stacked bar from [(value, style), ...], proportional to the sum; empty if all zero."""
        total = sum(v for v, _ in parts)
        if not total:
            return self("░" * width, "gray")
        cells = [(v * width / total, s) for v, s in parts]
        sizes = [int(c) for c, _ in cells]
        for i in sorted(range(len(cells)), key=lambda i: -(cells[i][0] - sizes[i]))[:width - sum(sizes)]:
            sizes[i] += 1
        for i, (v, _) in enumerate(parts):   # a non-zero part is always visible
            if v and not sizes[i]:
                sizes[i] = 1
        while sum(sizes) > width:
            sizes[sizes.index(max(sizes))] -= 1
        return "".join(self("█" * n, s) for n, (_, s) in zip(sizes, parts) if n)

    def title(self, text, width=None):
        width = width or term_width()
        return self(f"━━ {text} ", "bold", "cyan") + self("━" * max(width - len(text) - 4, 3), "cyan")

    def delta(self, removed, added, modified=None):
        """-N +N ~N with red / green / yellow, dimmed when zero."""
        def one(sign, n, col):
            return self(f"{sign}{n}", col, "bold") if n else self(f"{sign}0", "gray")
        out = [one("-", removed, "red"), one("+", added, "green")]
        if modified is not None:
            out.append(one("~", modified, "yellow"))
        return " ".join(out)

    def num(self, n, col):
        return self(n, col, "bold") if n else self(0, "gray")

    def table(self, header, rows, right=(), indent=2, wrap_last=False):
        """Aligned table: header bold + underline, cells already styled; `right` = right-aligned column indexes.

        With `wrap_last`, the last column holds plain text that is wrapped to the terminal width
        (continuation lines stay aligned under that column).
        """
        widths = [max(visible_len(str(r[i])) for r in ([header] if header else []) + rows)
                  for i in range(len(rows[0]))]
        last = len(widths) - 1
        sp = " " * indent
        wrapped = None
        if wrap_last:
            room = max(term_width() - indent - sum(widths[:last]) - 2 * last, 30)
            wrapped = [textwrap.wrap(str(r[last]), room) or [""] for r in rows]
            widths[last] = max([visible_len(str(header[last])) if header else 0]
                               + [len(x) for w in wrapped for x in w])
        lines = []
        if header:
            lines.append(sp + "  ".join(pad(self(h, "bold"), w, i in right)
                                        for i, (h, w) in enumerate(zip(header, widths))).rstrip())
            lines.append(sp + self("  ".join("─" * w for w in widths), "gray"))
        for n, r in enumerate(rows):
            cells = [pad(str(c), w, i in right) if i < last or i in right else str(c)
                     for i, (c, w) in enumerate(zip(r, widths))]
            if wrapped:
                cells[last] = wrapped[n][0]
            lines.append(sp + "  ".join(cells))
            for extra in (wrapped[n][1:] if wrapped else []):
                lines.append(sp + "  ".join(" " * w for w in widths[:last]) + "  " + extra)
        return lines
