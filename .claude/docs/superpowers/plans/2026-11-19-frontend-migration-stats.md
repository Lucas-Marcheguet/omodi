# Frontend migration stats (QWeb / OWL / JS) + CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract, compare and aggregate QWeb, OWL and JS migration stats, and ship everything as an installable `omodi` CLI.

**Architecture:** Package `omodi/` holds the three existing scripts as modules plus `js.py` and `cli.py`; root scripts become thin wrappers. `Index` scans each module once (including `static/src`) and feeds both the model extraction (adds a `templates` section) and a new per-module extraction (`odoo-module-extract/1`). Compare dispatches on the `format` key; the orchestrator adds `modules/` outputs and `stats_frontend.csv`.

**Tech Stack:** Python >= 3.9 stdlib; optional `tree-sitter` + `tree-sitter-javascript`; `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-19-frontend-migration-stats-design.md` (+ the original request: stat keys, key formats, exclusions).

## Global Constraints

- Stdlib only; the only allowed external deps are `tree-sitter` and `tree-sitter-javascript`, and they are optional (warning + empty structural JS sections if import fails).
- Python >= 3.9. Messages in French, argparse CLI, JSON with `indent=1`, `loc` kept but never counted as a modification.
- Existing extractions/diffs for existing sections must remain byte-identical.
- Exclusions for OWL and JS: `static/tests`, `static/lib`, `static/src/**/tests`.
- JS parse errors: warning, never a crash; ignore tree-sitter `ERROR` nodes.
- One scan per version in the orchestrator; no repeated file reads.
- Formats: `odoo-model-extract/1` (new section `templates`), `odoo-module-extract/1` (sections `qweb_templates`, `owl_templates`, `js_files`, `js_exports`, `js_classes`, `js_registries`, `js_patches`, `assets`).
- New `STAT_KEYS` (appended in readable order): `templates_removed/added/modified`, `template_nodes_removed/added`, `owl_templates_removed/added/modified`, `js_files_removed/added/modified`, `js_lines_removed/added`, `js_exports_removed/added/moved_out/moved_in`, `js_classes_removed/added/modified`, `js_methods_removed/added`, `js_registries_removed/added/modified`, `js_patches_removed/added/modified`, `assets_entries_removed/added`.

## Review Focus

- Module without `static/` or without `assets` key in the manifest -> empty sections, no crash.
- Manifest `assets` not a literal (`ast.literal_eval` fails) -> warning, empty assets.
- JS file with syntax errors / empty file / non-UTF-8 -> warning, file still counted at level 1.
- XML invalid or with no `t-name` and no `t-inherit` in OWL -> skipped with warning, no crash.
- Report whose template is defined in another module or missing -> report ignored with warning; inherit chain cycle terminates.
- tree-sitter not installed -> level 1 only, one single warning, JS tests skipped.

---

### Task 1: Package skeleton, CLI, packaging (no behavior change)

**Files:**
- Create: `omodi/__init__.py`, `omodi/__main__.py`, `omodi/cli.py`, `omodi/extract.py`, `omodi/compare.py`, `omodi/migration_stats.py`, `pyproject.toml`, `tests/__init__.py`, `tests/test_regression.py`
- Modify: `odoo_extract.py`, `odoo_compare.py`, `odoo_migration_stats.py` (become wrappers)
- Test fixtures: `tests/fixtures/old/`, `tests/fixtures/new/` (minimal Python-model module `sale_demo` with a field, a method and a view, changed between versions)

**Interfaces:**
- Produces: `omodi.extract.main(argv=None)`, `omodi.compare.main(argv=None)`, `omodi.migration_stats.main(argv=None)`; `omodi.cli.main(argv=None) -> None` dispatching `extract|compare|stats` (+ `--version`); console script `omodi`.

- [ ] **Step 1:** Before moving code, run the current scripts on the fixture and save outputs to `tests/fixtures/golden/` (`old.json`, `new.json`, `diff.json`, plus `stats.json` from the orchestrator minus timing).
- [ ] **Step 2:** Write `tests/test_regression.py::test_existing_outputs_identical` comparing fresh outputs to the golden files (parsed JSON equal). Run: `python -m unittest tests.test_regression -v` -> PASS on the untouched scripts.
- [ ] **Step 3:** `git mv` the script contents into `omodi/*.py` (imports become relative: `from . import extract as ex`); root scripts: `from omodi.<x> import main` with `sys.path.insert(0, dirname(__file__))` fallback and `if __name__ == "__main__": main()`.
- [ ] **Step 4:** `omodi/cli.py`: subcommands forward `argv[1:]` to the module `main`; `pyproject.toml` with setuptools, `[project.scripts] omodi = "omodi.cli:main"`, optional extra `js`.
- [ ] **Step 5:** Run regression test + `python odoo_extract.py --help` + `python -m omodi extract --help` -> PASS / help printed.
- [ ] **Step 6:** Commit `refactor: package omodi with CLI wrappers` (only if the user has asked for commits; otherwise skip).

### Task 2: QWeb templates and reports (model extraction)

**Files:** Modify `omodi/extract.py`; Create `tests/test_qweb.py`; fixtures add a report XML (`ir.actions.report` record + `<report>` tag), a QWeb template and an inheriting template (`inherit_id`, `mode`, `priority`, `t-call`) in `old` and `new` (with a changed node in `new`).

**Interfaces:**
- Produces: `parse_qweb(path, module, module_path) -> Iterator[(xml_id, info)]` where info = `{inherit_id, primary, mode, priority, arch_hash, nodes, calls, loc}` (`calls` sorted `t-call` targets, reusing `_arch`); `parse_reports(path, module) -> Iterator[(report_name, model, xml_id_of_template)]`; `Index.qweb` (`{xml_id: info+module}`), `Index.reports`; `SECTIONS` gains `"templates"` (model extraction only).
- Consumes: `_arch`, `_full_id`, `_REF_EVAL`.

- [ ] **Step 1:** Tests `test_report_template_attached_to_model` (template of report with `model=sale.order` appears in `Index.extract("sale.order")["modules"][m]["templates"]`, as well as its inheriting template via the `inherit_id` chain), `test_tag_report_supported`, `test_non_report_template_not_in_model_extract`, `test_calls_sorted`, `test_inherit_cycle_terminates`.
- [ ] **Step 2:** Run them -> FAIL.
- [ ] **Step 3:** Implement `parse_qweb`/`parse_reports` (handle `<template>` and `ir.ui.view` records with `type=qweb`, files outside `static/`); extend `Index._scan` to fill `qweb`/`reports`; attach after the scan (same pass as views resolution); full `report_name` -> template xml_id resolution (`module.name`).
- [ ] **Step 4:** Run `tests.test_qweb` and `tests.test_regression` -> PASS (existing sections unchanged; `templates` is a new key — update golden regression to ignore only that key).
- [ ] **Step 5:** Commit (if asked).

### Task 3: OWL templates and module-extraction format

**Files:** Modify `omodi/extract.py`; Create `tests/test_owl.py`, `tests/test_module_extract.py`; fixtures add `static/src/xml/*.xml` (a `t-name` template, a `t-inherit` template without `t-name`, a file under `static/tests` that must be excluded) and `__manifest__.py` with `assets`.

**Interfaces:**
- Produces: `MODULE_FORMAT = "odoo-module-extract/1"`, `MODULE_SECTIONS` (tuple of the 8 sections), `parse_owl(path, module, module_path) -> Iterator[(key, info)]` with key = `t-name` or `f"{t_inherit}|{relpath}"`, info = `{t_inherit, t_inherit_mode, arch_hash, nodes, loc}`; `parse_assets(manifest_path) -> dict[str, list[str]]` (stringified entries; tuples like `("replace", a, b)` -> `"replace a b"`); `Index.extract_module(module) -> dict`; `Index.modules_list`; `static_files(module_path)` generator yielding `(abs_path, rel_to_static_src)` honoring the exclusions.
- Consumes: Task 2 (`Index.qweb`, `Index.reports`): non-report QWeb templates go in `qweb_templates`.

- [ ] **Step 1:** Tests: `test_owl_key_t_name`, `test_owl_inherit_without_name_key`, `test_static_tests_and_lib_excluded`, `test_assets_literal_eval`, `test_assets_non_literal_warns`, `test_module_extract_has_all_sections`, `test_qweb_not_attached_goes_to_module`.
- [ ] **Step 2:** Run -> FAIL. **Step 3:** Implement; module extraction JSON has `format`, `module`, `label`, `paths` and the 8 sections (JS sections empty until Task 4). **Step 4:** Run -> PASS. **Step 5:** Commit (if asked).

### Task 4: JS level 1 and level 2 (tree-sitter)

**Files:** Create `omodi/js.py`, `tests/test_js.py`; Modify `omodi/extract.py` (call `js.analyze` from the single scan); fixtures add `static/src/**/*.js` with an export class, a function, a const, `export default`, `export { a as b }`, `export * from`, a registry `.add`, a `patch(...)`, a file with a syntax error, and in `new` the same export moved to another file.

**Interfaces:**
- Produces: `js.HAS_TREE_SITTER: bool`; `js.file_info(text) -> {"sha1", "lines"}`; `js.import_path(module, rel_to_static_src, header_text) -> Optional[str]` (`@mod/path` without `.js`, alias from `@odoo-module alias=...`, `None` if `@odoo-module ignore`); `js.analyze(text, module, import_path, loc_prefix) -> {"exports": {...}, "classes": {...}, "registries": {...}, "patches": {...}}` keyed by `(name)` strings with the data from the request (`kind`/`loc`; `extends`/`methods`/`static`; `value` truncated to 200; patch `members`); never raises (returns empty + warning on failure).
- Dict keys in JSON: `"<import_path>::<name>"` for exports and classes, `"<category>::<key>"` for registries, `"<import_path>::<target>"` for patches (module is the outer level).
- Consumes: Task 3 `static_files`, `MODULE_SECTIONS`.

- [ ] **Step 1:** Check installed py-tree-sitter API in a venv (`pip install tree-sitter tree-sitter-javascript`), record the working constructor in a comment; verify in real Odoo code how modules are declared (check `/** @odoo-module */` vs default) and encode that in `import_path`.
- [ ] **Step 2:** Tests with exact expectations: `test_import_path_default`, `test_import_path_alias`, `test_exports_all_forms` (class/function/const/let/default/`{a as b}`/reexport), `test_class_extends_methods_static_fields`, `test_registry_chain_category_add`, `test_patch_members`, `test_value_truncated_200`, `test_syntax_error_does_not_crash`, `test_no_tree_sitter_fallback` (monkeypatch `HAS_TREE_SITTER=False`: level 1 filled, structural empty, one warning).
- [ ] **Step 3:** Run -> FAIL. **Step 4:** Implement (single parse per file; skip `ERROR`/`MISSING` nodes). **Step 5:** Run -> PASS. **Step 6:** Commit (if asked).

### Task 5: `--module` option in extract

**Files:** Modify `omodi/extract.py` (`main`), `tests/test_module_extract.py`.

**Interfaces:** CLI `-m MODEL` and `--module MODULE` both repeatable; output file `<module>.json` for modules (new function `module_filename(module)`); summary line in French with counts.

- [ ] **Step 1:** Test `test_cli_module_option_writes_json` (run `main([...])` into a temp dir; check `format == "odoo-module-extract/1"`, JS sections non-empty when tree-sitter is available). **Step 2-4:** fail, implement, pass (`-o` required with `--module`). **Step 5:** commit (if asked).

### Task 6: Compare module extractions, templates section, new stats

**Files:** Modify `omodi/compare.py`; Create `tests/test_compare_frontend.py`.

**Interfaces:**
- Produces: `STAT_KEYS` extended (see Global Constraints); `compare_modules(old, new, detect_moves=True) -> dict` with `format = "odoo-module-diff/1"`, per-section `removed/added/modified` (+ `moved` for exports and classes), `stats`, `stats_total`; `compare(old, new, detect_moves=True)` dispatches on `format`; `compare_models` gains the `templates` section (removed/added/modified + `template_nodes_*`, same node-diff logic as views); `compare_js_lines(old_path_text, new_path_text) -> (added, removed)` using `difflib` is in the orchestrator, not here.
- Moves: export/class removed from `(module, path, name)` and added elsewhere with the same name (same or other module) -> `moved` with `from`/`to` (`module` + `path`), stats `js_exports_moved_out/in`. Classes moved count in `js_classes_*`? No: classes moves are listed but only exports counted as moved keys (as in the request).
- `main` of compare: works with files/dirs of either format.

- [ ] **Step 1:** Tests asserting every expected counter on the fixtures: `templates_*`, `template_nodes_*`, `owl_templates_*`, `js_files_*` (sha1/lines only), `js_exports_removed/added/moved_out/moved_in`, `js_classes_*`, `js_methods_*`, `js_registries_*` (removed key, modified value), `js_patches_*`, `assets_entries_*`; `test_loc_change_is_not_a_modification`; `test_old_stats_unchanged` (regression).
- [ ] **Step 2-4:** fail, implement, pass. **Step 5:** commit (if asked).

### Task 7: Orchestrator

**Files:** Modify `omodi/migration_stats.py`; Create `tests/test_orchestrator.py`.

**Interfaces:** produces `OUT/{old,new,diff}/modules/<module>.json` for each requested module, `stats.json` gains `by_module`/`total` frontend keys and a `frontend` per-module block, `stats_frontend.csv` (one row per module, frontend keys only), line counts `js_lines_added/removed` computed with `difflib` from the two index file paths (read only for files whose sha1 differs); console lines `Templates :` and `JS :`.

- [ ] **Step 1:** Test `test_end_to_end_fixture` (run `main` on fixtures; assert files exist, expected `stats.json` totals, `stats_frontend.csv` header/row, console output contains `Templates` and `JS`); `test_existing_csv_columns_prefix_unchanged`.
- [ ] **Step 2-4:** fail, implement (stats module-level are merged into `by_module` Counters), pass. **Step 5:** commit (if asked).

### Task 8: Docstrings, real-world run, report

**Files:** Modify headers of `omodi/extract.py`, `omodi/compare.py`, `omodi/migration_stats.py` and root wrappers; Create `README.md` only if the user requests it (not requested: skip).

- [ ] **Step 1:** Update docstrings (usage + both formats + new stats).
- [ ] **Step 2:** In the scratchpad (outside the repo): sparse clones of `20.0` and `master` of github.com/odoo/odoo with `addons/web`, `addons/sale`, `addons/account`, `addons/stock`, `odoo/addons/base`; run `omodi stats ... --modules sale stock account web`.
- [ ] **Step 3:** Manually verify 3-4 results in the source (a moved export, a removed registry key, a modified report template).
- [ ] **Step 4:** Run the full suite: `python -m unittest discover -s tests -v` -> all PASS (JS tests skipped only without tree-sitter).
- [ ] **Step 5:** Report to the user: modified files, usage commands, real-world console summary, known limits.
