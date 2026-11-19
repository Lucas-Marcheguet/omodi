#!/usr/bin/env python3
"""
omodi compare — compares two extractions produced by omodi extract.

Usage:
  omodi compare v20/sale.order.json v21/sale.order.json -o sale.order.diff.json
  omodi compare v20/modules/web.json v21/modules/web.json -o web.diff.json
  omodi compare v20/ v21/ -o diffs/    # every .json of the two folders (and of their modules/ subfolder)
  omodi compare ...                            # same thing through the installed CLI

The format of each file is read from its `format` key.

odoo-model-extract/1 -> odoo-model-diff/1. Module by module (key (module, name)):
  - fields / methods / constraints : removed, added, modified, moved
  - attributes                     : modified (addition / removal / value change)
  - views, templates (reports)     : removed, added, modified (arch nodes added / removed)
"moved" = same name that disappears from one module and appears in another module of the same model.

odoo-module-extract/1 -> odoo-module-diff/1 :
  - qweb_templates, owl_templates, js_files, js_registries, js_patches : removed, added, modified
  - js_exports, js_classes : removed, added, (modified for classes), moved. An export / class that
    disappears from an import path and reappears elsewhere (same module) is "moved" with from / to;
    "default" and "*" are never paired. Across modules: resolve_cross_module_moves() (orchestrator).
  - assets : entries added / removed per bundle
  js_files: modified = different sha1. js_lines_*: estimated from the difference in line count (exact
  count through difflib provided by omodi stats via line_counter).

Locations (loc) are kept for information but do not count as a modification.
Stats keys: STAT_KEYS (the frontend keys start at FRONTEND_KEYS).
Stdlib only, Python >= 3.9.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

FORMAT = "odoo-model-diff/1"
EMPTY = {"modules": {}}

STAT_KEYS = [
    "fields_removed", "fields_added", "fields_modified", "fields_moved_out", "fields_moved_in",
    "field_attrs_added", "field_attrs_removed", "field_attrs_changed", "field_type_changed",
    "methods_removed", "methods_added", "methods_modified", "methods_moved_out", "methods_moved_in",
    "constraints_removed", "constraints_added", "constraints_modified",
    "attributes_modified",
    "views_removed", "views_added", "views_modified", "view_nodes_removed", "view_nodes_added",
    # QWeb templates (attached to a model or not) and OWL
    "templates_removed", "templates_added", "templates_modified",
    "template_nodes_removed", "template_nodes_added",
    "owl_templates_removed", "owl_templates_added", "owl_templates_modified",
    # JS
    "js_files_removed", "js_files_added", "js_files_modified", "js_lines_removed", "js_lines_added",
    "js_exports_removed", "js_exports_added", "js_exports_moved_out", "js_exports_moved_in",
    "js_classes_removed", "js_classes_added", "js_classes_modified",
    "js_methods_removed", "js_methods_added",
    "js_registries_removed", "js_registries_added", "js_registries_modified",
    "js_patches_removed", "js_patches_added", "js_patches_modified",
    "assets_entries_removed", "assets_entries_added",
]
FRONTEND_KEYS = STAT_KEYS[STAT_KEYS.index("templates_removed"):]
MODULE_FORMAT = "odoo-module-diff/1"
NO_MOVE_NAMES = {"default", "*"}   # too generic to detect a move


def _strip_loc(d):
    return {k: v for k, v in d.items() if k != "loc"}


def _dict_changes(old, new):
    out = {}
    for k in sorted(set(old) | set(new)):
        if k not in old:
            out[k] = {"added": new[k]}
        elif k not in new:
            out[k] = {"removed": old[k]}
        elif old[k] != new[k]:
            out[k] = {"old": old[k], "new": new[k]}
    return out


def _flatten(ext, section):
    """{(module, name): data}"""
    return {(m, n): d for m, mod in ext.get("modules", {}).items()
            for n, d in mod.get(section, {}).items()}


# --- comparison of one item -------------------------------------------------
def _cmp_field(a, b, st):
    ch = {}
    if a["type"] != b["type"]:
        ch["type"] = {"old": a["type"], "new": b["type"]}
        st["field_type_changed"] += 1
    attrs = _dict_changes(a["attrs"], b["attrs"])
    if attrs:
        ch["attrs"] = attrs
        for v in attrs.values():
            st["field_attrs_added" if "added" in v else
               "field_attrs_removed" if "removed" in v else "field_attrs_changed"] += 1
    return ch


def _cmp_method(a, b, st):
    ch = {}
    for k in ("signature", "decorators"):
        if a[k] != b[k]:
            ch[k] = {"old": a[k], "new": b[k]}
    return ch


def _cmp_constraint(a, b, st):
    return _dict_changes(_strip_loc(a), _strip_loc(b))


def _cmp_view(a, b, st):
    ch = {}
    for k in ("inherit_id", "type", "mode", "priority"):
        if a.get(k) != b.get(k):
            ch[k] = {"old": a.get(k), "new": b.get(k)}
    if a["arch_hash"] != b["arch_hash"]:
        ca, cb = Counter(a["nodes"]), Counter(b["nodes"])
        rem, add = sorted((ca - cb).elements()), sorted((cb - ca).elements())
        ch["arch"] = {"nodes_removed": rem, "nodes_added": add}
        st["view_nodes_removed"] += len(rem)
        st["view_nodes_added"] += len(add)
    return ch


def _cmp_template(a, b, st):
    """Server QWeb template: same rules as views, plus primary and the t-call targets."""
    ch = {}
    for k in ("inherit_id", "primary", "mode", "priority", "calls"):
        if a.get(k) != b.get(k):
            ch[k] = {"old": a.get(k), "new": b.get(k)}
    if a.get("arch_hash") != b.get("arch_hash"):
        ca, cb = Counter(a.get("nodes", [])), Counter(b.get("nodes", []))
        rem, add = sorted((ca - cb).elements()), sorted((cb - ca).elements())
        ch["arch"] = {"nodes_removed": rem, "nodes_added": add}
        st["template_nodes_removed"] += len(rem)
        st["template_nodes_added"] += len(add)
    return ch


SECTION_CMP = {
    "fields": (_cmp_field, True),
    "methods": (_cmp_method, True),
    "constraints": (_cmp_constraint, False),
    "views": (_cmp_view, False),
    "templates": (_cmp_template, False),
}


def _brief(section, d):
    if section == "fields":
        return {"type": d["type"], "loc": d.get("loc")}
    return {"loc": d.get("loc")}


# --- comparison of a model --------------------------------------------------
def compare_models(old, new, detect_moves=True):
    model = old.get("model") or new.get("model")
    stats = defaultdict(Counter)  # stats[module][key]
    out = {
        "format": FORMAT,
        "model": model,
        "old_label": old.get("label"),
        "new_label": new.get("label"),
        "modules": {
            "only_old": sorted(set(old.get("modules", {})) - set(new.get("modules", {}))),
            "only_new": sorted(set(new.get("modules", {})) - set(old.get("modules", {}))),
        },
    }

    for section, (cmp, moves_ok) in SECTION_CMP.items():
        o, n = _flatten(old, section), _flatten(new, section)
        removed = sorted(set(o) - set(n))
        added = sorted(set(n) - set(o))
        moved = []
        if detect_moves and moves_ok:
            pool = defaultdict(list)
            for k in added:
                pool[k[1]].append(k)
            still = []
            for k in removed:
                cand = [x for x in pool.get(k[1], []) if x[0] != k[0]]
                if cand:
                    pool[k[1]].remove(cand[0])
                    moved.append((k, cand[0]))
                else:
                    still.append(k)
            removed = still
            used = {b for _, b in moved}
            added = [k for k in added if k not in used]

        modified = []
        for k in sorted(set(o) & set(n)):
            ch = cmp(o[k], n[k], stats[k[0]])
            if ch:
                modified.append({"module": k[0], "name": k[1], "changes": ch})
                stats[k[0]][f"{section}_modified"] += 1

        for k in removed:
            stats[k[0]][f"{section}_removed"] += 1
        for k in added:
            stats[k[0]][f"{section}_added"] += 1
        for a, b in moved:
            stats[a[0]][f"{section}_moved_out"] += 1
            stats[b[0]][f"{section}_moved_in"] += 1

        sec = {
            "removed": [{"module": m, "name": nm, **_brief(section, o[(m, nm)])} for m, nm in removed],
            "added": [{"module": m, "name": nm, **_brief(section, n[(m, nm)])} for m, nm in added],
            "modified": modified,
        }
        if moves_ok:
            sec["moved"] = [{"name": a[1], "from": a[0], "to": b[0]} for a, b in moved]
        out[section] = sec

    # class attributes
    o, n = _flatten(old, "attributes"), _flatten(new, "attributes")
    attr_mod = []
    for k in sorted(set(o) | set(n)):
        ov = o[k]["value"] if k in o else None
        nv = n[k]["value"] if k in n else None
        if ov != nv:
            attr_mod.append({"module": k[0], "name": k[1], "old": ov, "new": nv})
            stats[k[0]]["attributes_modified"] += 1
    out["attributes"] = {"modified": attr_mod}

    out["stats"] = {m: {k: c[k] for k in STAT_KEYS if c[k]} for m, c in sorted(stats.items())
                    if any(c.values())}
    total = Counter()
    for c in out["stats"].values():
        total.update(c)
    out["stats_total"] = {k: total[k] for k in STAT_KEYS if total[k]}
    return out


# --- comparison of a module (odoo-module-extract/1) ---------------------------
def _cmp_owl(a, b, st):
    ch = {}
    for k in ("t_inherit", "t_inherit_mode"):
        if a.get(k) != b.get(k):
            ch[k] = {"old": a.get(k), "new": b.get(k)}
    if a.get("arch_hash") != b.get("arch_hash"):
        ca, cb = Counter(a.get("nodes", [])), Counter(b.get("nodes", []))
        ch["arch"] = {"nodes_removed": sorted((ca - cb).elements()),
                      "nodes_added": sorted((cb - ca).elements())}
    return ch


def _cmp_class(a, b, st):
    ch = {}
    if a.get("extends") != b.get("extends"):
        ch["extends"] = {"old": a.get("extends"), "new": b.get("extends")}
    static = _dict_changes(a.get("static", {}), b.get("static", {}))
    if static:
        ch["static"] = static
    am, bm = a.get("methods", {}), b.get("methods", {})
    added, removed = sorted(set(bm) - set(am)), sorted(set(am) - set(bm))
    changed = {k: {"old": am[k], "new": bm[k]} for k in sorted(set(am) & set(bm)) if am[k] != bm[k]}
    if added or removed or changed:
        ch["methods"] = {"added": added, "removed": removed, "changed": changed}
        st["js_methods_added"] += len(added)
        st["js_methods_removed"] += len(removed)
    return ch


def _cmp_registry(a, b, st):
    return _dict_changes({"value": a.get("value")}, {"value": b.get("value")})


def _cmp_patch(a, b, st):
    pick = lambda d: {**{k: d.get(k, []) for k in ("methods", "properties")},
                      **({"extension": d["extension"]} if "extension" in d else {})}
    return _dict_changes(pick(a), pick(b))


def _simple_section(o, n, cmp, st, prefix):
    """removed / added / modified for a {key: data} dict; counters {prefix}_*."""
    removed = [{"name": k, "loc": o[k].get("loc")} for k in sorted(set(o) - set(n))]
    added = [{"name": k, "loc": n[k].get("loc")} for k in sorted(set(n) - set(o))]
    modified = []
    for k in sorted(set(o) & set(n)):
        ch = cmp(o[k], n[k], st)
        if ch:
            modified.append({"name": k, "changes": ch})
    st[f"{prefix}_removed"] += len(removed)
    st[f"{prefix}_added"] += len(added)
    st[f"{prefix}_modified"] += len(modified)
    return {"removed": removed, "added": added, "modified": modified}


def _named_item(module, key, data):
    path, name = key.split("::", 1)
    item = {"module": module, "path": path, "name": name, "loc": data.get("loc")}
    if "kind" in data:
        item["kind"] = data["kind"]
    return item


def _pick(item, candidates):
    """Candidate of the same kind if possible (then stable order)."""
    same = [c for c in candidates if c.get("kind") == item.get("kind")]
    return (same or candidates)[0] if candidates else None


def _match_moves(removed, added, allow_same_module):
    """Pairs removed items with added items of the same name. -> (moves, removed, added)."""
    pool = defaultdict(list)
    for it in added:
        if it["name"] not in NO_MOVE_NAMES:
            pool[it["name"]].append(it)
    moves, still, used = [], [], set()
    for it in removed:
        cands = [c for c in pool.get(it["name"], []) if id(c) not in used
                 and (allow_same_module or c["module"] != it["module"])
                 and (c["module"], c["path"]) != (it["module"], it["path"])]
        dest = _pick(it, cands) if it["name"] not in NO_MOVE_NAMES else None
        if dest is None:
            still.append(it)
            continue
        used.add(id(dest))
        moves.append((it, dest))
    return moves, still, [it for it in added if id(it) not in used]


def _move_entry(src, dst):
    return {"name": src["name"], "from": {"module": src["module"], "path": src["path"]},
            "to": {"module": dst["module"], "path": dst["path"]}}


def _named_section(o, n, module, detect_moves):
    """Exports / classes: keys "import_path::name"."""
    removed = [_named_item(module, k, o[k]) for k in sorted(set(o) - set(n))]
    added = [_named_item(module, k, n[k]) for k in sorted(set(n) - set(o))]
    moved = []
    if detect_moves:
        moves, removed, added = _match_moves(removed, added, allow_same_module=True)
        moved = [_move_entry(a, b) for a, b in moves]
    return removed, added, moved


def _restat_names(diff):
    """Recomputes the removed/added/moved counters of exports and classes from the diff lists."""
    module = diff["module"]
    st = Counter(diff["stats"].get(module, {}))
    for section, prefix in (("js_exports", "js_exports"), ("js_classes", "js_classes")):
        sec = diff[section]
        st[f"{prefix}_removed"] = len(sec["removed"])
        st[f"{prefix}_added"] = len(sec["added"])
    st["js_exports_moved_out"] = sum(1 for m in diff["js_exports"]["moved"] if m["from"]["module"] == module)
    st["js_exports_moved_in"] = sum(1 for m in diff["js_exports"]["moved"] if m["to"]["module"] == module)
    _store_stats(diff, st)


def _store_stats(diff, st):
    module = diff["module"]
    clean = {k: st[k] for k in STAT_KEYS if st[k]}
    if clean:
        diff["stats"][module] = clean
    else:
        diff["stats"].pop(module, None)
    total = Counter()
    for c in diff["stats"].values():
        total.update(c)
    diff["stats_total"] = {k: total[k] for k in STAT_KEYS if total[k]}


def compare_modules(old, new, detect_moves=True, line_counter=None):
    """Compares two odoo-module-extract/1 extractions of the same module -> odoo-module-diff/1.

    line_counter(module, file_key, old, new) -> (lines_added, lines_removed) allows providing an exact
    count (difflib); by default js_lines_* is estimated from the difference in line count
    (added file: all its lines added; removed: all removed).
    Moves across modules are resolved afterwards: resolve_cross_module_moves().
    """
    module = old.get("module") or new.get("module")
    st = Counter()
    g = lambda ext, s: ext.get(s, {})
    out = {"format": MODULE_FORMAT, "module": module,
           "old_label": old.get("label"), "new_label": new.get("label")}

    out["qweb_templates"] = _simple_section(g(old, "qweb_templates"), g(new, "qweb_templates"),
                                            _cmp_template, st, "templates")
    out["owl_templates"] = _simple_section(g(old, "owl_templates"), g(new, "owl_templates"),
                                           _cmp_owl, st, "owl_templates")

    # JS files (level 1)
    o, n = g(old, "js_files"), g(new, "js_files")
    files = {"removed": [{"name": k, "lines": o[k].get("lines")} for k in sorted(set(o) - set(n))],
             "added": [{"name": k, "lines": n[k].get("lines")} for k in sorted(set(n) - set(o))],
             "modified": []}
    st["js_lines_removed"] += sum(f["lines"] or 0 for f in files["removed"])
    st["js_lines_added"] += sum(f["lines"] or 0 for f in files["added"])
    for k in sorted(set(o) & set(n)):
        if o[k].get("sha1") == n[k].get("sha1"):
            continue
        files["modified"].append({"name": k, "changes": {
            "sha1": {"old": o[k].get("sha1"), "new": n[k].get("sha1")},
            "lines": {"old": o[k].get("lines"), "new": n[k].get("lines")}}})
        if line_counter:
            add, rem = line_counter(module, k, o[k], n[k])
        else:
            delta = (n[k].get("lines") or 0) - (o[k].get("lines") or 0)
            add, rem = max(delta, 0), max(-delta, 0)
        st["js_lines_added"] += add
        st["js_lines_removed"] += rem
    st["js_files_removed"] += len(files["removed"])
    st["js_files_added"] += len(files["added"])
    st["js_files_modified"] += len(files["modified"])
    out["js_files"] = files

    # exports and classes (keys "path::name"): moves inside the module
    for section in ("js_exports", "js_classes"):
        removed, added, moved = _named_section(g(old, section), g(new, section), module, detect_moves)
        modified = []
        if section == "js_classes":
            oc, nc = g(old, section), g(new, section)
            for k in sorted(set(oc) & set(nc)):
                ch = _cmp_class(oc[k], nc[k], st)
                if ch:
                    path, name = k.split("::", 1)
                    modified.append({"module": module, "path": path, "name": name, "changes": ch})
            st["js_classes_modified"] += len(modified)
        out[section] = {"removed": removed, "added": added, "modified": modified, "moved": moved}

    out["js_registries"] = _simple_section(g(old, "js_registries"), g(new, "js_registries"),
                                           _cmp_registry, st, "js_registries")
    out["js_patches"] = _simple_section(g(old, "js_patches"), g(new, "js_patches"),
                                        _cmp_patch, st, "js_patches")

    # assets: entries added / removed per bundle
    oa, na = g(old, "assets"), g(new, "assets")
    mods = []
    for bundle in sorted(set(oa) | set(na)):
        co, cn = Counter(oa.get(bundle, [])), Counter(na.get(bundle, []))
        rem, add = sorted((co - cn).elements()), sorted((cn - co).elements())
        if rem or add:
            mods.append({"bundle": bundle, "removed": rem, "added": add})
            st["assets_entries_removed"] += len(rem)
            st["assets_entries_added"] += len(add)
    out["assets"] = {"modified": mods}

    out["stats"], out["stats_total"] = {}, {}
    _store_stats(out, st)
    _restat_names(out)
    return out


def resolve_cross_module_moves(diffs, extra_added=None):
    """Detects exports / classes that change module (same name elsewhere). Modifies `diffs` in place.

    diffs       : {module: odoo-module-diff/1 diff} (already processed by compare_modules)
    extra_added : {"js_exports": [...], "js_classes": [...]} : items added in modules without a diff
                  ({"module","path","name","kind"}), used only as a possible destination.
    `moved` entries are added to the source diff and to the destination diff if it exists.
    """
    for section in ("js_exports", "js_classes"):
        removed = [it for d in diffs.values() for it in d[section]["removed"]]
        added = [it for d in diffs.values() for it in d[section]["added"]] + [dict(x) for x in (extra_added or {}).get(section, [])]
        moves, _, _ = _match_moves(removed, added, allow_same_module=False)
        for src, dst in moves:
            entry = _move_entry(src, dst)
            sd, dd = diffs[src["module"]], diffs.get(dst["module"])
            sd[section]["removed"] = [x for x in sd[section]["removed"] if x is not src]
            sd[section]["moved"].append(entry)
            if dd is not None:
                dd[section]["added"] = [x for x in dd[section]["added"]
                                        if (x["path"], x["name"]) != (dst["path"], dst["name"])]
                dd[section]["moved"].append(entry)
    for d in diffs.values():
        _restat_names(d)
    return diffs


def compare(old, new, detect_moves=True, line_counter=None):
    """Compares two extractions of the same format (the format is read from the `format` key)."""
    fo, fn = old.get("format"), new.get("format")
    if fo and fn and fo.split("/")[0] != fn.split("/")[0]:
        raise ValueError(f"incompatible formats : {fo} / {fn}")
    fmt = fo or fn or ""
    if fmt.startswith("odoo-module-extract"):
        return compare_modules(old, new, detect_moves, line_counter)
    return compare_models(old, new, detect_moves)


# --- I/O ----------------------------------------------------------------------
def load(path):
    if path is None or not os.path.exists(path):
        return EMPTY
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(obj, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def summary_line(diff):
    t = diff["stats_total"]
    g = lambda k: t.get(k, 0)
    if diff["format"] == MODULE_FORMAT:
        return (f"{diff['module']}: templates -{g('templates_removed')} +{g('templates_added')} "
                f"~{g('templates_modified')} | OWL -{g('owl_templates_removed')} +{g('owl_templates_added')} "
                f"~{g('owl_templates_modified')} | JS files -{g('js_files_removed')} +{g('js_files_added')} "
                f"~{g('js_files_modified')} exports -{g('js_exports_removed')} +{g('js_exports_added')} "
                f"moved {g('js_exports_moved_out')}")
    return (f"{diff['model']}: fields -{g('fields_removed')} +{g('fields_added')} ~{g('fields_modified')} "
            f"| methods -{g('methods_removed')} +{g('methods_added')} ~{g('methods_modified')} "
            f"| views -{g('views_removed')} +{g('views_added')} ~{g('views_modified')}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="omodi compare", description="Compare two omodi extract extractions")
    p.add_argument("old", help="JSON file or folder (old version)")
    p.add_argument("new", help="JSON file or folder (new version)")
    p.add_argument("-o", "--output", required=True, help="output .json file, or folder when comparing folders")
    p.add_argument("--no-moves", action="store_true", help="do not detect moves between modules")
    a = p.parse_args(argv)

    try:
        _main(p, a)
    except ValueError as e:
        p.error(str(e))


def _main(p, a):
    if os.path.isdir(a.old) and os.path.isdir(a.new):
        names = sorted({os.path.join(sub, f) for d in (a.old, a.new) for sub in ("", "modules")
                        if os.path.isdir(os.path.join(d, sub))
                        for f in os.listdir(os.path.join(d, sub)) if f.endswith(".json")})
        for f in names:
            diff = compare(load(os.path.join(a.old, f)), load(os.path.join(a.new, f)),
                           detect_moves=not a.no_moves)
            write_json(diff, os.path.join(a.output, f))
            print(summary_line(diff))
    elif os.path.isfile(a.old) or os.path.isfile(a.new):
        diff = compare(load(a.old), load(a.new), detect_moves=not a.no_moves)
        write_json(diff, a.output)
        print(summary_line(diff))
    else:
        p.error("old and new must be two files or two folders")


if __name__ == "__main__":
    main()
