# Upgrade impact analysis (`omodi impact`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, chosen by the user for this repo). Steps use checkbox syntax.

**Goal:** `omodi impact` tells, per custom module, what to watch for when upgrading from OLD to NEW Odoo, by cross-referencing the diff of its dependency closure with what the module overrides, inherits, references or imports.

**Architecture:** `Index` gets a two-phase API (`scan=False` then `.scan(only, focus)`), `depends`, `xml_ids` and focus-only collection (XML refs, `env.ref`, JS imports). New `omodi/impact.py` computes closures, reuses `compare_models` / `compare_modules` / move resolution, applies the rules to produce findings, writes `impact.json` + `impact.md`.

**Tech Stack:** Python >= 3.9 stdlib; optional tree-sitter (JS rules degrade gracefully); unittest.

**Spec:** `docs/superpowers/specs/2026-11-19-impact-analysis-design.md`

## Global Constraints
- Stdlib only (tree-sitter optional); French messages; argparse; JSON `indent=1`.
- Existing formats and outputs of `extract` / `compare` / `stats` unchanged (existing 66 tests stay green).
- OLD index scans only custom + upstream closure; NEW index scans the whole NEW tree.
- Custom modules are searched in `--custom` paths first, then in `--old`; a copy in `--new` is ignored.
- Heuristic findings carry `confidence: "heuristic"`; the report lists known limits.
- Finding shape: `{level: breaking|review|info, category, rule, message, custom: {module, loc, name}, upstream: {...}, confidence}`.

## Review Focus
- Custom module not found anywhere -> clear error, exit 2; dependency in neither tree -> `unresolved`, no crash.
- Dependency cycle in manifests terminates; manifest with non-literal `depends` -> warning, treated as no deps.
- Custom module with no `static/`, no Python models, empty -> empty findings, valid report.
- tree-sitter missing -> JS rules skipped with one warning, other rules still run.
- Same finding must not be reported twice (override + textual mention on the same `file:line`).
- A moved item that is still reachable through the NEW closure is `review`, not `breaking`.

---

### Task 1: Index extensions (two-phase scan, depends, xml ids, focus collection)
**Files:** Modify `omodi/extract.py`, `omodi/js.py`; Test `tests/test_index_impact.py`; fixtures `tests/fixtures/impact/{old,new,custom}` (see Task 2 for content; Task 1 needs `custom/cust` + `old/up`).
**Interfaces (produces):**
- `read_depends(manifest_path) -> list[str]` (warn + `[]` if not literal).
- `Index(addons_paths, label=None, only=None, focus=None, scan=True)`; `Index.scan(only=None, focus=None)`; `Index.depends: dict[str, list[str]]` (all modules); `Index.xml_ids: dict[str, str]` (full id -> module, scanned modules); focus modules only: `Index.xml_refs: dict[module, list[{"xml_id","kind","loc"}]]`, `Index.py_refs` (same shape, `env.ref` literals), `Index.js_imports: dict[module, list[{"file","source","imported","local","loc"}]]` (`imported` = name | "default" | "*" | None for side-effect imports).
- `js.analyze(data, ip, loc, with_imports=False)`: result gains `"imports"` only when `with_imports`.
- `js.resolve_source(source, module, rel_file) -> str` (relative `./x` -> `@module/dir/x`).
- [ ] Tests (names): `test_depends_read_for_all_modules`, `test_two_phase_scan_only_scans_selected`, `test_xml_ids_collected`, `test_xml_refs_collected_for_focus_only` (kinds: ref, inherit_id, groups, parent, action, t-call, eval-ref, %()d; unqualified ids ignored), `test_py_env_ref_collected`, `test_js_imports_named_default_namespace_side_effect`, `test_relative_import_resolved`, `test_analyze_without_imports_unchanged`.
- [ ] Run RED; implement; run full suite GREEN.

### Task 2: Closure + model-level rules (`omodi/impact.py`)
**Files:** Create `omodi/impact.py`, `tests/test_impact_model.py`; fixtures under `tests/fixtures/impact/` (module `up` old/new, `up_new` in new (receives `f_moved`, `m_moved`, `act_moved`), `gone_mod` old only, custom `cust` overriding: removed / moved / type-changed / attr-changed field, removed / signature-changed / moved method, clash field `f_clash`, `_inherit` of a removed model, view inheriting `up.thing_form` with `field[name=f_removed]` and xpath on a removed node, template inheriting a removed template and one inheriting a modified one with xpath token on a removed class, `t-call` of a removed template).
**Interfaces (produces):** `closure(index, roots) -> (members: list[str], unresolved: list[{"module","required_by"}])`; `finding(level, category, rule, message, module, loc, name=None, upstream=None, confidence="certain") -> dict`; `Context` dataclass-like holding old/new indexes, custom list, upstream sets, new closure; `model_findings(ctx, module) -> list[dict]`; `dependency_findings(ctx, module) -> list[dict]` (missing in NEW = breaking `depends-removed`, unresolved = info).
- [ ] Tests: `test_closure_transitive_and_cycle_safe`, `test_unresolved_depends`, `test_missing_dependency_in_new_is_breaking`, `test_field_removed_breaking`, `test_field_moved_outside_new_closure_breaking_with_target_module`, `test_field_moved_inside_new_closure_review`, `test_field_type_changed_review`, `test_method_removed_breaking`, `test_method_signature_changed_review`, `test_clash_with_new_upstream_field_review`, `test_inherited_model_removed_breaking`, `test_view_parent_removed_breaking`, `test_view_field_removed_breaking`, `test_view_xpath_target_removed_breaking_heuristic`, `test_template_parent_removed_breaking`, `test_template_parent_modified_review`, `test_template_t_call_removed_breaking`, `test_custom_only_model_ignored`.
- [ ] RED; implement (per-model `compare_models` with OLD filtered to upstream closure, NEW unfiltered; cache per model); GREEN.

### Task 3: Module-level, XML-id and mention rules
**Files:** Modify `omodi/impact.py`; Test `tests/test_impact_module.py`; fixtures: custom JS importing removed / moved / kept exports, class extending a modified upstream class, `patch()` with removed and changed members, registry add of a removed key and of a newly added upstream key, OWL `t-inherit` of removed and modified templates, assets referencing a removed upstream file and a removed bundle, XML refs to a removed / moved / unchanged / unknown id, `env.ref` of a removed id, `util.py` mentioning a removed field name.
**Interfaces (produces):** `module_findings(ctx, module) -> list[dict]` (OWL, JS imports/extends/patches/registries, assets), `xmlid_findings(ctx, module)`, `mention_findings(ctx, module, existing)`; `Context.module_diffs: dict[str, dict]` (upstream `compare_modules` results after `resolve_cross_module_moves` with a pool limited to modules absent from OLD).
- [ ] Tests: `test_js_import_removed_breaking`, `test_js_import_moved_breaking_with_new_path`, `test_js_import_kept_no_finding`, `test_class_extends_modified_class_review`, `test_class_override_of_removed_method_breaking`, `test_patch_removed_method_breaking`, `test_registry_key_removed_review`, `test_registry_clash_review`, `test_owl_inherit_removed_breaking`, `test_owl_inherit_modified_review`, `test_asset_removed_file_breaking`, `test_asset_bundle_removed_breaking`, `test_xml_ref_removed_breaking`, `test_xml_ref_moved_breaking_with_target`, `test_xml_ref_unchanged_and_unknown_ignored`, `test_env_ref_removed_breaking`, `test_mention_heuristic_review`, `test_mention_not_duplicated_on_covered_line`, `test_no_tree_sitter_skips_js_rules_with_warning`.
- [ ] RED; implement; GREEN.

### Task 4: Report, CLI, docstring
**Files:** Modify `omodi/impact.py` (`analyze`, `render_markdown`, `main`), `omodi/cli.py` (`impact` command), `omodi/__init__.py` unchanged; Test `tests/test_impact_cli.py`.
**Interfaces (produces):** `analyze(old_paths, new_paths, custom_paths, modules, old_label, new_label) -> dict` (`format: "odoo-impact/1"`, per-module `closure`, `findings` sorted breaking > review > info then category/loc, `counts`; top-level `upstream_stats`, `limits`); `render_markdown(report) -> str`; `main(argv=None)`: `--old`, `--new`, `--custom`, `--modules`, `--modules-file`, `--old-label`, `--new-label`, `-o`.
- [ ] Tests: `test_end_to_end_report_json_and_markdown` (files exist, counts per level for `cust`, markdown has one section per module and the three levels), `test_unknown_module_exits_2`, `test_modules_file_and_several_modules`, `test_custom_in_old_tree_equals_custom_path`, `test_omodi_impact_dispatch`.
- [ ] RED; implement; GREEN; header docstring (usage, finding levels, limits).

### Task 5: Real run and report
- [ ] Write a small custom module (outside the repo, in the scratchpad) depending on `sale`, `stock`, `web` with overrides/imports of items known to have changed between Odoo 20.0 and master (found from the existing real diff); run `omodi impact`; verify 3-4 findings by hand in the source; run the full suite (with and without tree-sitter); report files, commands, console/markdown excerpt, limits.
