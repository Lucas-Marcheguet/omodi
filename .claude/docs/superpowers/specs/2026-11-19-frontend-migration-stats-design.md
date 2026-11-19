# Frontend migration stats (QWeb / OWL / JS) + installable CLI — design

## Intent
Measure the frontend migration effort between two Odoo versions, in addition to the existing
Python/model stats. Main risk to surface: JS exports/classes that move between import paths,
which break imports in custom modules. Also make the tool an installable CLI.

## Goals
- QWeb server templates, OWL templates and JS (tree-sitter) extracted, compared and aggregated,
  following the existing extract -> compare -> stats flow, as specified in the request.
- New format `odoo-module-extract/1` (one JSON per module) next to `odoo-model-extract/1`.
- New `STAT_KEYS`, `stats_frontend.csv`, console lines "Templates" and "JS".
- `pip install .` provides `omodi extract|compare|stats`, equivalent to the three scripts.

## Non-goals / anti-goals
- No existing extraction or diff section changes (identical output for fields/methods/views/...).
- No recursive following of `t-call`. No JS type resolution. No external dependency except
  optional `tree-sitter` + `tree-sitter-javascript`.
- A JS parse failure must never crash a run.

## Approaches (chosen: A)
- **A (chosen)**: package `omodi/` (`extract.py`, `compare.py`, `migration_stats.py`, `js.py`,
  `cli.py`, `__main__.py`); the three root scripts stay as thin wrappers so existing commands work.
- B: keep the 3 files at root + `pyproject` with `py-modules`. Rejected: fragile install.

## Design decisions
- **Layout**: `pyproject.toml` (setuptools, `requires-python>=3.9`, no mandatory deps,
  extra `js` = tree-sitter packages, console script `omodi`). Wrappers do `from omodi.x import main`
  (with a `sys.path` fallback to the repo root, so no install is needed).
- **Single pass**: `Index._scan` walks each module once, including `static/src`
  (separate walk filter; `static/tests`, `static/lib`, `static/src/**/tests` excluded).
  XML under `static/src` -> OWL; other XML -> views / QWeb / reports; JS read once, parsed once.
- **Reports**: `ir.actions.report` records and `<report>` tags give `model` + `report_name`;
  main template + its `inherit_id` chain go in a new `templates` section of the model extraction.
  Remaining QWeb templates go in the module extraction (`qweb_templates`).
- **JS**: level 1 (sha1, line count, difflib line counts only in the orchestrator) is stdlib;
  level 2 (import path, exports, classes, registries, patches) needs tree-sitter, adapted to the
  installed py-tree-sitter API (>=0.22 style); missing import => one warning, structural sections empty.
  Odoo module declaration (`@odoo-module`, alias, `ignore`) checked against real Odoo code first.
- **Moves**: removed export/class name reappearing under another import path (same or other
  module) -> `moved` with `from`/`to`; counted as `js_exports_moved_out/in`.
- **Compare**: format detected from the `format` key; files or directories as today.
- **Orchestrator**: also writes `old|new|diff/modules/<module>.json` and `stats_frontend.csv`.
- **CLI**: `omodi extract|compare|stats` forwards the remaining argv to each module's `main()`.
- **Tests**: `unittest` with fixtures in `tests/fixtures/{old,new}` (report, inherited QWeb,
  OWL `t-inherit`, JS export/class/registry/patch, moved export); JS tests `skipUnless` tree-sitter.
  Real-world run on Odoo 20.0 vs master (sparse clones, outside the repo).

## Left to the builder
Exact JSON field names inside new sections, tree-sitter node handling, CSV column order.
