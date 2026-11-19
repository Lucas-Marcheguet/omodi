#!/usr/bin/env python3
"""
omodi stats — chains extraction + comparison for a list of modules.

Input : two Odoo versions (addons paths) + the list of modules to check.
Steps : 1. indexes both versions once (extract.Index: Python, XML, QWeb, OWL, JS, assets)
        2. finds every model touched by these modules (in either version)
        3. extracts each model in both versions            -> OUT/old/<model>.json, OUT/new/<model>.json
           and each listed module (unattached templates, OWL, JS, assets)
                                                            -> OUT/old/modules/<module>.json, OUT/new/modules/...
        4. compares                                         -> OUT/diff/<model>.json, OUT/diff/modules/<module>.json
           (exact JS lines added / removed via difflib; exports moved between modules are detected,
           including towards unlisted modules)
        5. aggregates the stats                             -> OUT/stats.json + CSV
Model extractions contain every module touching the model (useful to detect moves); the stats are
restricted to the requested modules, except with --all-modules (models only).

Usage:
  omodi stats \\
      --old odoo-20/addons odoo-20/odoo/addons enterprise-20 \\
      --new odoo-21/addons odoo-21/odoo/addons enterprise-21 \\
      --modules sale stock account          # or --modules-file modules.txt (one per line, # = comment)
      --old-label 20.0 --new-label 21.0 -o migration_20_21/

Output: the console summary is always printed. Files are written only with -o [DIR] (default ./omodi-output),
in the format(s) chosen by -f {json,csv,all}:
  json: stats.json (totals, per module, per model, per (module, model)) and the intermediate JSON in
        old/ new/ diff/ (models at the root, modules in modules/)
  csv : stats_by_module.csv (one row per module; frontend columns on the right), stats_by_model.csv
        (one row per model), stats_detail.csv (one row per (module, model)), stats_frontend.csv
        (one row per module: QWeb / OWL templates, JS, assets; FRONTEND_KEYS)
Console summary: "Templates" and "JS" lines in addition to the existing ones.
Stdlib only, Python >= 3.9; tree-sitter + tree-sitter-javascript optional (pip install omodi[js])
for the structural JS stats (exports, classes, registries, patches). Package omodi.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import sys
import time
from collections import Counter, defaultdict

from . import compare as cmp
from . import extract as ex
from . import term
from .output import Progress, add_output_args, check_output_args, wants

KEYS = cmp.STAT_KEYS


def read_modules(args):
    mods = list(args.modules or [])
    if args.modules_file:
        with open(args.modules_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    mods.extend(x.strip() for x in line.replace(",", " ").split() if x.strip())
    seen, out = set(), []
    for m in mods:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def write_csv(path, key_cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(key_cols + KEYS)
        for key, c in rows:
            key = key if isinstance(key, tuple) else (key,)
            w.writerow(list(key) + [c.get(k, 0) for k in KEYS])


def js_line_counter(old_idx, new_idx):
    """line_counter for compare_modules: exact added / removed lines (difflib), files re-read
    only when their sha1 differs."""
    def read(idx, module, key):
        with open(idx.js_paths[(module, key)], encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()

    def count(module, key, old_info, new_info):
        try:
            a, b = read(old_idx, module, key), read(new_idx, module, key)
        except (KeyError, OSError):
            delta = (new_info.get("lines") or 0) - (old_info.get("lines") or 0)
            return max(delta, 0), max(-delta, 0)
        added = removed = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
        return added, removed
    return count


def unlisted_added(old_idx, new_idx, listed):
    """Exports / classes added in unlisted modules: possible destinations of moves."""
    pool = {"js_exports": [], "js_classes": []}
    for module in new_idx.modules:
        if module in listed:
            continue
        for section in pool:
            new = new_idx.mod[module][section]
            old = old_idx.mod[module][section] if module in old_idx.mod else {}
            for key in sorted(set(new) - set(old)):
                path, name = key.split("::", 1)
                pool[section].append({"module": module, "path": path, "name": name,
                                      "kind": new[key].get("kind"), "loc": new[key].get("loc")})
    return pool


def print_table(title, rows, top, st=None):
    """Top-N table: one row per module / model, one column per family, each cell `-removed +added ~modified`."""
    st = st or term.Style()
    fams = [("Fields", "fields"), ("Methods", "methods"), ("Views", "views")]
    suffixes = ("removed", "added", "modified")
    rows = [r for r in rows if r[0] != "TOTAL"]
    total = lambda c: sum(c.get(f"{f}_{x}", 0) for _, f in fams for x in suffixes)
    rows = sorted((r for r in rows if total(r[1])), key=lambda kv: -total(kv[1]))[:top]
    if not rows:
        return
    print("\n" + st.title(title))
    body = []
    for name, c in rows:
        cells = [st.delta(*(c.get(f"{f}_{x}", 0) for x in suffixes)) if any(c.get(f"{f}_{x}", 0) for x in suffixes)
                 else st("·", "gray") for _, f in fams]
        body.append([st(term.truncate(str(name), 40), "cyan")] + cells)
    print("\n".join(st.table(["Name"] + [h for h, _ in fams], body)))


def print_summary(a, total, modules, models, gone, elapsed, st=None):
    """Colored summary: one row per family that changed, with removed / added / modified columns."""
    st = st or term.Style()
    g = lambda k: total.get(k, 0)
    print("\n" + st(" OMODI ", "bold", "white") + "  " + st("migration stats", "bold") + "  "
          + st(a.old_label, "cyan", "bold") + st(" ➜ ", "gray") + st(a.new_label, "green", "bold")
          + st(f"   {len(modules)} modules · {len(models)} models · {elapsed:.1f}s", "gray"))
    if gone:
        print(st("  ✖ modules removed in the new version: ", "red", "bold") + st(", ".join(gone), "red"))
    # (label, key prefix, has "modified" column, details shown only when non-zero)
    families = [
        ("Fields", "fields", True, [("attributes added", "field_attrs_added"), ("attributes removed", "field_attrs_removed"),
                                    ("type changed", "field_type_changed"), ("moved to another module", "fields_moved_out")]),
        ("Methods", "methods", True, [("moved to another module", "methods_moved_out")]),
        ("Views", "views", True, [("nodes removed", "view_nodes_removed"), ("nodes added", "view_nodes_added")]),
        ("Constraints", "constraints", True, []),
        ("QWeb templates", "templates", True, [("nodes removed", "template_nodes_removed"),
                                               ("nodes added", "template_nodes_added")]),
        ("OWL templates", "owl_templates", True, []),
        ("JS files", "js_files", True, [("lines removed", "js_lines_removed"), ("lines added", "js_lines_added")]),
        ("JS exports", "js_exports", False, [("moved to another module", "js_exports_moved_out")]),
        ("JS classes", "js_classes", True, [("methods removed", "js_methods_removed"), ("methods added", "js_methods_added")]),
        ("JS registries", "js_registries", True, []),
        ("JS patches", "js_patches", True, []),
        ("Assets", "assets_entries", False, []),
    ]
    body = []
    for label, key, has_mod, details in families:
        r, ad, mo = g(f"{key}_removed"), g(f"{key}_added"), g(f"{key}_modified") if has_mod else 0
        if not (r or ad or mo):
            continue
        body.append([st(label, "bold"), st.num(r, "red"), st.num(ad, "green"),
                     st.num(mo, "yellow") if has_mod else st("-", "gray"),
                     ", ".join(f"{n} {g(k)}" for n, k in details if g(k))])
    print()
    if not body:
        print(st("  No change detected.", "gray"))
        return
    print("\n".join(st.table(["", "Removed", "Added", "Modified", "Details"], body, right=(1, 2, 3), wrap_last=True)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="omodi stats", description="Odoo migration stats for a list of modules")
    p.add_argument("--old", nargs="+", required=True, help="addons paths of the old version")
    p.add_argument("--new", nargs="+", required=True, help="addons paths of the new version")
    p.add_argument("--modules", nargs="+", help="modules to check")
    p.add_argument("--modules-file", help="file listing the modules (one per line or comma-separated)")
    p.add_argument("--old-label", default="old", help="label of the old version (e.g. 20.0)")
    p.add_argument("--new-label", default="new", help="label of the new version (e.g. 21.0)")
    add_output_args(p, "stats and intermediate extractions")
    p.add_argument("--all-modules", action="store_true",
                   help="also count unlisted modules touching the same models")
    p.add_argument("--no-moves", action="store_true", help="do not detect moves between modules")
    p.add_argument("--top", type=int, default=20, help="rows shown in the summary")
    a = p.parse_args(argv)
    check_output_args(p, a)
    progress = Progress(enabled=not a.quiet)
    write_json_files, write_csv_files = wants(a, "json"), wants(a, "csv")

    modules = read_modules(a)
    if not modules:
        p.error("give at least one module (--modules or --modules-file)")

    t0 = time.time()
    old_idx = ex.Index(a.old, a.old_label, progress=lambda n: progress.task(f"Indexing {a.old_label}", n))
    new_idx = ex.Index(a.new, a.new_label, progress=lambda n: progress.task(f"Indexing {a.new_label}", n))

    gone = [m for m in modules if m in old_idx.modules and m not in new_idx.modules]
    new_only = [m for m in modules if m not in old_idx.modules and m in new_idx.modules]
    unknown = [m for m in modules if m not in old_idx.modules and m not in new_idx.modules]
    for m in unknown:
        print(term.paint("⚠ warn ", "yellow", "bold", on=term.color_enabled(sys.stderr))
              + f"module {m} not found in either version", file=sys.stderr)

    models = sorted(old_idx.models_of(modules) | new_idx.models_of(modules))
    progress.info(f"{len(models)} models touched by {len(modules)} modules")

    wanted = set(modules)
    by_module = defaultdict(Counter)
    by_model = defaultdict(Counter)
    detail = {}
    total = Counter()

    # module level: unattached templates, OWL, JS, assets (extraction + diff per listed module)
    present = [m for m in modules if m in old_idx.modules or m in new_idx.modules]
    module_diffs = {}
    counter = js_line_counter(old_idx, new_idx)
    update = progress.task("Modules", len(present))
    for done, module in enumerate(present):
        update(done, module)
        old_m = old_idx.extract_module(module) or {"module": module, "label": a.old_label}
        new_m = new_idx.extract_module(module) or {"module": module, "label": a.new_label}
        if write_json_files and module in old_idx.modules:
            ex.write_json(old_m, os.path.join(a.output, "old", ex.module_filename(module)))
        if write_json_files and module in new_idx.modules:
            ex.write_json(new_m, os.path.join(a.output, "new", ex.module_filename(module)))
        module_diffs[module] = cmp.compare_modules(old_m, new_m, detect_moves=not a.no_moves,
                                                   line_counter=counter)
    update(len(present))
    if not a.no_moves:
        cmp.resolve_cross_module_moves(module_diffs, unlisted_added(old_idx, new_idx, set(present)))
    for module, mdiff in module_diffs.items():
        if write_json_files:
            cmp.write_json(mdiff, os.path.join(a.output, "diff", ex.module_filename(module)))
        c = Counter(mdiff["stats"].get(module, {}))
        if c:   # as for models: no empty row in by_module
            by_module[module].update(c)
            total.update(c)

    update = progress.task("Models", len(models))
    for done, model in enumerate(models):
        update(done, model)
        fname = ex.model_filename(model)
        old_ext, new_ext = old_idx.extract(model), new_idx.extract(model)
        diff = cmp.compare_models(old_ext, new_ext, detect_moves=not a.no_moves)
        if write_json_files:
            ex.write_json(old_ext, os.path.join(a.output, "old", fname))
            ex.write_json(new_ext, os.path.join(a.output, "new", fname))
            cmp.write_json(diff, os.path.join(a.output, "diff", fname))
        for module, c in diff["stats"].items():
            if not a.all_modules and module not in wanted:
                continue
            c = Counter(c)
            by_module[module].update(c)
            by_model[model].update(c)
            detail[(module, model)] = c
            total.update(c)
    update(len(models))

    def clean(c):
        return {k: c[k] for k in KEYS if c.get(k)}

    stats = {
        "old_label": a.old_label,
        "new_label": a.new_label,
        "modules_requested": modules,
        "modules_removed": gone,
        "modules_new": new_only,
        "modules_unknown": unknown,
        "models_checked": models,
        "total": clean(total),
        "by_module": {m: clean(c) for m, c in sorted(by_module.items())},
        "by_model": {m: clean(c) for m, c in sorted(by_model.items())},
        "by_module_model": [{"module": m, "model": mo, **clean(c)} for (m, mo), c in sorted(detail.items())],
    }
    if write_json_files:
        ex.write_json(stats, os.path.join(a.output, "stats.json"))
    frontend_total = Counter()
    frontend_rows = []
    for module in present:
        c = by_module.get(module, Counter())
        frontend_total.update({k: c[k] for k in cmp.FRONTEND_KEYS})
        frontend_rows.append([module] + [c.get(k, 0) for k in cmp.FRONTEND_KEYS])
    if write_csv_files:
        write_csv(os.path.join(a.output, "stats_by_module.csv"), ["module"],
                  sorted(by_module.items()) + [("TOTAL", total)])
        write_csv(os.path.join(a.output, "stats_by_model.csv"), ["model"],
                  sorted(by_model.items()) + [("TOTAL", total)])
        write_csv(os.path.join(a.output, "stats_detail.csv"), ["module", "model"], sorted(detail.items()))
        with open(os.path.join(a.output, "stats_frontend.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["module"] + cmp.FRONTEND_KEYS)
            w.writerows(frontend_rows)
            w.writerow(["TOTAL"] + [frontend_total.get(k, 0) for k in cmp.FRONTEND_KEYS])

    # console summary
    print_summary(a, total, modules, models, gone, time.time() - t0)
    print_table("By module", list(by_module.items()), a.top)
    print_table("Most affected models", list(by_model.items()), a.top)
    if a.output is not None:
        files = (["stats.json", "old/ new/ diff/"] if write_json_files else []) + \
                (["stats_by_module.csv", "stats_by_model.csv", "stats_detail.csv", "stats_frontend.csv"]
                 if write_csv_files else [])
        print(term.paint(f"\n✔ results ➜ {a.output}/ ", "green", "bold", on=term.color_enabled(sys.stderr))
              + f"({', '.join(files)})", file=sys.stderr)

if __name__ == "__main__":
    main()
