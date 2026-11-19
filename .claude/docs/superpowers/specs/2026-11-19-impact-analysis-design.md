# Upgrade impact analysis for custom modules — design

## Intent
Given one or more custom modules, tell the developer what to watch for when upgrading them from
version OLD to version NEW of Odoo: compute the dependency closure of each custom module, diff those
dependencies between the two versions (existing extract/compare machinery), then cross-reference the diff
with what the custom module actually overrides, inherits, references or imports.

## Goals
- New command `omodi impact --old PATH... --new PATH... [--custom PATH...] --modules M... [--modules-file F]
  [--old-label L --new-label L] -o OUT` (several custom modules allowed).
- Custom modules are looked up in `--custom` paths first, then in the `--old` tree. They are analysed in their
  current (old) state; any copy in the `--new` tree is ignored.
- Outputs `OUT/impact.json` (format `odoo-impact/1`) and `OUT/impact.md` (human report, one section per
  custom module, findings sorted by level), each finding pointing to the custom `file:line` and to the upstream item
  that changed. Also a totals block of the upstream diff (reuse of the existing stat keys).
- Levels: **breaking** (target removed / moved / renamed, dependency missing in NEW), **review** (signature,
  attributes, arch, static fields changed; name clash with a new upstream item; textual mention of a removed name),
  **info**.

## Non-goals / anti-goals
- No Python data-flow analysis; no evaluation of XPath against real arch; no auto-fix; no LLM.
- A heuristic finding must say it is a heuristic (confidence shown); the report lists its known limits.
- No change to the existing formats (`odoo-model-extract/1`, `odoo-module-extract/1`) or to the output of
  `extract`, `compare`, `stats`.

## Approaches (chosen: A)
- **A (chosen)**: deterministic cross-reference in a new `omodi/impact.py`, reusing `Index`, `compare_models`,
  `compare_modules`. Impact-only data (depends, xml ids, references, JS imports) is kept on `Index` attributes,
  not in the extract formats.
- B: run `stats` on the dependency closure and report counts only. Rejected: does not say what to do.
- C: LLM triage of the diff. Rejected for the core (non deterministic, costly); can be layered on `impact.md` later.

## Design decisions
- **Dependency closure**: `depends` read from manifests (all modules, cheap). BFS from the custom modules over OLD
  manifests; custom modules (requested or found in `--custom`) are traversed but not diffed; the others are the
  *upstream closure*. A dependency absent from NEW is a *breaking* finding ("module removed or renamed"); a name
  found in neither tree is reported as unresolved. The NEW closure is computed the same way from the NEW manifests.
- **Scan scope (performance)**: OLD index scans only custom + upstream closure (`Index(only=...)`); NEW index scans the
  whole NEW tree, so that things moved to a module outside the closure are detected ("moved to Y: add the dependency").
- **Model level**: for every model touched by a custom module, extract OLD (closure modules only) and NEW (all modules)
  and `compare_models`; match custom items against upstream diff entries:
  field / method removed, moved, type or signature changed, added upstream with the same name (clash);
  `_inherit` of a model that no longer exists; custom views: `inherit_id` removed/modified, `field[name=X]` nodes
  of the custom arch whose field was removed, xpath `@name='X'` tokens matching nodes removed from the parent view;
  custom QWeb templates: `inherit_id` and `t-call` targets removed/modified, xpath tokens vs removed nodes.
- **Module level**: `compare_modules` on each upstream dependency: OWL `t-inherit` and `t-call` targets,
  JS imports (`import {X} from "@mod/path"` -> export removed / moved with `to`), custom classes `extends` upstream
  classes (methods removed/changed), custom `patch()` members absent from the new upstream class, registry
  entries overridden upstream and removed (or added upstream with the same key), custom `assets` referencing
  upstream files that were removed or bundles that no longer exist.
- **XML ids and `env.ref`**: all XML ids defined in scanned modules are indexed; custom XML references (`ref`,
  `inherit_id`, `parent`, `action`, `groups`, `%(x.y)d`, `eval="ref('x.y')"`) and Python `env.ref("x.y")`
  string literals are checked: defined in OLD but not in NEW -> breaking, with "moved to module Y" when the same
  local id exists elsewhere in NEW. Unknown in OLD -> ignored (no false positive).
- **Textual mentions (heuristic, review level)**: names of upstream fields / methods / JS exports removed on models the
  custom module touches (length >= 4) are searched with word boundaries in the custom module's .py / .xml / .js files.
- **New collection on `Index` (focus modules only)**: `depends` for all modules; `xml_ids`; for the custom modules,
  XML references, Python `env.ref` references and JS imports (tree-sitter `import_statement`, same single parse).
- **CLI**: `omodi impact` forwards to `omodi.impact.main`; no root wrapper script.
- **Tests**: `unittest` with small fixtures `tests/fixtures/impact/{old,new,custom}` covering each rule, plus a real
  run on Odoo 20.0 vs master with a small custom module written for the test, checked by hand against the source.

## Left to the builder
Exact JSON field names of findings, wording of messages (French), Markdown layout, XPath token extraction details,
ordering of findings inside a level.
