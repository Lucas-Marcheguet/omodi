"""
omodi impact — what to watch out for when upgrading one or more custom modules.

For each custom module:
  1. closure of its dependencies (`depends` of the OLD manifests); custom modules are traversed but
     not compared, the others form the "upstream";
  2. diff of the upstream between OLD and NEW (extract + compare: models, QWeb / OWL templates, JS, assets);
  3. cross-check with what the module overrides, inherits, references or imports -> findings.
OLD is scanned only on the custom module and its closure; NEW is scanned entirely (to see that an item
was moved to a module outside the closure: "add the dependency").

Usage:
  omodi impact --old ODOO-20/addons ODOO-20/odoo/addons ENTERPRISE-20 \\
                --new ODOO-21/addons ODOO-21/odoo/addons ENTERPRISE-21 \\
                --custom MY_ADDONS --modules my_module other_module
  (--modules-file modules.txt: one module per line or comma-separated, # = comment)
A custom module is looked up first in --custom, then in --old; a copy in --new is ignored.

Output: the colored report (one block per module) is always printed on the console, never written to a file.
With -o [DIR] (default ./omodi-output), -f {json,csv,all} also writes OUT/impact.json (format odoo-impact/1)
and/or OUT/impact.csv (one row per finding).

Levels: breaking (target removed / moved / renamed, dependency missing from NEW),
        review (signature, arguments, arch or static fields modified; name clash with a new upstream
        item; textual mention of a removed name), info (the rest, e.g. unresolved dependency).
Rules (`rule` field): depends-*, model-removed, field-* / method-* (removed, moved, moved-out-of-closure,
modified, clash), view-* / template-* (parent-removed, parent-modified, field-removed, call-removed,
xpath-target-removed), owl-*, js-import-*, js-class-modified, js-method-*, js-patch-method-*, registry-*,
asset-*, xmlid-removed / xmlid-moved (ref, inherit_id, groups, %(x.y)d, env.ref), mention-removed-name.
Findings with `confidence: heuristic` rely on a heuristic (xpath, bundles, textual mentions).
Limits: see LIMITS (repeated in the report). Stdlib only; tree-sitter optional (JS rules).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter

from . import compare as cmp
from . import extract as ex
from . import js
from .migration_stats import read_modules
from . import term
from .output import Progress, add_output_args, check_output_args, wants

LEVELS = ("breaking", "review", "info")


class ImpactError(Exception):
    """Usage error (module not found...): displayed by the CLI without a traceback."""


def finding(level, category, rule, message, module, loc, name=None, upstream=None, confidence="certain"):
    """A finding: level, category, rule, message, custom side (module, loc, name) and upstream side."""
    return {"level": level, "category": category, "rule": rule, "message": message,
            "custom": {"module": module, "loc": loc, "name": name},
            "upstream": upstream or {}, "confidence": confidence}


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
def closure(index, roots):
    """Transitive closure of `depends` from `roots`.

    Returns (members, unresolved): members = modules found in the index (roots included);
    unresolved = [{"module", "required_by"}] for the names missing from the index.
    """
    members, unresolved, seen = [], [], set()
    queue = [(r, None) for r in roots]
    while queue:
        name, parent = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name not in index.modules:
            unresolved.append({"module": name, "required_by": parent})
            continue
        members.append(name)
        queue.extend((d, name) for d in index.depends.get(name, []) if d not in seen)
    return members, unresolved


def _under(path, folders):
    path = os.path.realpath(path)
    return any(path == f or path.startswith(f + os.sep) for f in folders)


class Context:
    """State shared by all the rules: two indexes, upstream scopes, per-model / per-module diffs."""

    def __init__(self):
        self.old = self.new = None
        self.modules = []            # requested custom modules
        self.custom_set = set()      # requested + modules found in --custom
        self.closure_old = {}        # requested module -> closure members (OLD)
        self.unresolved = {}         # requested module -> unresolved dependencies
        self.upstream_old = {}       # requested module -> upstream modules of its closure (OLD)
        self.closure_new = {}        # requested module -> closure of the same modules in NEW
        self.upstream_union = set()
        self.module_diffs = {}       # upstream module -> odoo-module-diff/1 diff
        self._model_diffs = {}
        self._local_ids = None

    def model_diff(self, model):
        """{"diff", "old", "new"} for a model: OLD restricted to the upstream, NEW complete (moves)."""
        if model not in self._model_diffs:
            old_ext = self.old.extract(model)
            old_ext["modules"] = {m: v for m, v in old_ext["modules"].items() if m in self.upstream_union}
            new_ext = self.new.extract(model)
            self._model_diffs[model] = {"diff": cmp.compare_models(old_ext, new_ext),
                                        "old": old_ext, "new": new_ext}
        return self._model_diffs[model]

    def local_template_ids(self):
        """{local name: [xml ids]} of the QWeb templates of NEW (to suggest a move)."""
        if self._local_ids is None:
            self._local_ids = {}
            for xid in self.new.qweb:
                self._local_ids.setdefault(xid.split(".", 1)[-1], []).append(xid)
        return self._local_ids


def _new_module_pool(old, new):
    """Exports / classes of the modules absent from OLD: possible destinations of a move."""
    pool = {"js_exports": [], "js_classes": []}
    for module in new.modules:
        if module in old.modules:
            continue
        for section in pool:
            for key, data in new.mod[module][section].items():
                path, name = key.split("::", 1)
                pool[section].append({"module": module, "path": path, "name": name,
                                      "kind": data.get("kind"), "loc": data.get("loc")})
    return pool


def build_context(old_paths, new_paths, custom_paths, modules, old_label=None, new_label=None, progress=None):
    """Indexes OLD (custom + upstream closure only) and NEW (whole tree), computes the upstream diffs."""
    ctx = Context()
    ctx.modules = list(modules)
    ctx.old = ex.Index(list(custom_paths) + list(old_paths), old_label, scan=False)
    unknown = [m for m in modules if m not in ctx.old.modules]
    if unknown:
        raise ImpactError(f"module(s) not found in --custom and --old: {', '.join(unknown)}")
    custom_dirs = [os.path.realpath(p) for p in custom_paths]
    ctx.custom_set = set(modules) | {m for m, p in ctx.old.modules.items() if _under(p, custom_dirs)}

    union = set()
    for m in modules:
        members, ctx.unresolved[m] = closure(ctx.old, [m])
        ctx.closure_old[m] = members
        ctx.upstream_old[m] = {x for x in members if x not in ctx.custom_set}
        union.update(members)
    ctx.upstream_union = {x for x in union if x not in ctx.custom_set}
    ctx.old.progress = (lambda n: progress.task(f"Indexing {old_label or 'old'}", n)) if progress else None
    ctx.old.scan(only=union, focus=modules)

    ctx.new = ex.Index(list(new_paths), new_label, scan=False)
    ctx.new.progress = (lambda n: progress.task(f"Indexing {new_label or 'new'}", n)) if progress else None
    ctx.new.scan()
    for m in modules:
        roots = [x for x in ctx.upstream_old[m] if x in ctx.new.modules]
        ctx.closure_new[m] = set(closure(ctx.new, roots)[0])

    upstream = sorted(ctx.upstream_union)
    update = progress.task("Comparing upstream modules", len(upstream)) if progress else None
    for done, m in enumerate(upstream):
        if update:
            update(done, m)
        old_m = ctx.old.extract_module(m)
        new_m = ctx.new.extract_module(m) or {"module": m, "label": new_label}
        ctx.module_diffs[m] = cmp.compare_modules(old_m, new_m)
    if update:
        update(len(upstream))
    cmp.resolve_cross_module_moves(ctx.module_diffs, _new_module_pool(ctx.old, ctx.new))
    return ctx


def dependency_findings(ctx, module):
    out = []
    for dep in sorted(ctx.upstream_old[module]):
        if dep not in ctx.new.modules:
            out.append(finding("breaking", "depends", "depends-removed",
                               f"the dependency `{dep}` no longer exists in the new version "
                               f"(module removed or renamed)", module,
                               os.path.relpath(os.path.join(ctx.old.modules[module], "__manifest__.py"),
                                               os.path.dirname(ctx.old.modules[module])),
                               name=dep, upstream={"name": dep}))
    for u in ctx.unresolved[module]:
        out.append(finding("info", "depends", "depends-unresolved",
                           f"the dependency `{u['module']}` (required by `{u['required_by']}`) cannot be found "
                           f"in --custom, --old and --new: incomplete analysis", module, None,
                           name=u["module"], upstream={"name": u["module"]}))
    return out


# --------------------------------------------------------------------------
# Model-level rules
# --------------------------------------------------------------------------
def _boundary(token):
    return re.compile(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])")


def _xpath_tokens(nodes):
    """Quoted strings of the xpath exprs of a list of node signatures."""
    tokens = []
    for sig in nodes:
        if sig.startswith("xpath["):
            tokens.extend(re.findall(r"""['"]([^'"]+)['"]""", sig))
    return sorted(set(tokens))


def _removed_tokens(tokens, removed, added):
    """Tokens present in a node removed from the parent arch and in no added node."""
    out = []
    for t in tokens:
        rx = _boundary(t)
        if any(rx.search(s) for s in removed) and not any(rx.search(s) for s in added):
            out.append(t)
    return out


def _summary(section, changes):
    parts = []
    if section == "fields":
        if "type" in changes:
            parts.append(f"type {changes['type']['old']} -> {changes['type']['new']}")
        if "attrs" in changes:
            parts.append("arguments changed: " + ", ".join(changes["attrs"]))
    else:
        if "signature" in changes:
            parts.append(f"signature ({changes['signature']['old']}) -> ({changes['signature']['new']})")
        if "decorators" in changes:
            parts.append(f"decorators {changes['decorators']['old']} -> {changes['decorators']['new']}")
    return "; ".join(parts) or "modified"


def _removed_names(md, section, up):
    """Really removed names (removed from an upstream module and no longer defined anywhere in NEW)."""
    still = {n for v in md["new"]["modules"].values() for n in v[section]}
    return {e["name"] for e in md["diff"][section]["removed"] if e["module"] in up and e["name"] not in still}


def _item_findings(ctx, module, model, section, md, up, newc, out):
    cat = "field" if section == "fields" else "method"
    label = "field" if cat == "field" else "method"
    sec = md["diff"][section]
    old_defs = {n for m, v in md["old"]["modules"].items() if m in up for n in v[section]}
    new_defs = {n for v in md["new"]["modules"].values() for n in v[section]}
    for name, data in sorted(ctx.old.data[model][module][section].items()):
        loc = data.get("loc")
        removed = [e for e in sec["removed"] if e["name"] == name and e["module"] in up]
        moved = [m for m in sec["moved"] if m["name"] == name and m["from"] in up]
        modified = [e for e in sec["modified"] if e["name"] == name and e["module"] in up]
        added = [e for e in sec["added"] if e["name"] == name and e["module"] in newc]
        if removed and name not in new_defs:
            out.append(finding("breaking", cat, f"{cat}-removed",
                               f"{label} `{name}` of `{model}` removed upstream (module {removed[0]['module']}): "
                               f"the override no longer has a target", module, loc, name,
                               {"name": name, "model": model, "module": removed[0]["module"]}))
        elif moved:
            to = moved[0]["to"]
            inside = to in newc
            out.append(finding(
                "review" if inside else "breaking", cat, f"{cat}-moved" if inside else f"{cat}-moved-out-of-closure",
                f"{label} `{name}` of `{model}` moved from `{moved[0]['from']}` to `{to}`"
                + ("" if inside else ": add this dependency to the manifest"), module, loc, name,
                {"name": name, "model": model, "from_module": moved[0]["from"], "to_module": to}))
        elif modified:
            out.append(finding("review", cat, f"{cat}-modified",
                               f"{label} `{name}` of `{model}` modified upstream: "
                               f"{_summary(section, modified[0]['changes'])}", module, loc, name,
                               {"name": name, "model": model, "module": modified[0]["module"]}))
        elif added and name not in old_defs:
            out.append(finding("review", cat, f"{cat}-clash",
                               f"{label} `{name}` of `{model}` is now also defined upstream "
                               f"(module {added[0]['module']}): possible clash", module, loc, name,
                               {"name": name, "model": model, "module": added[0]["module"]}))


_FIELD_NODE = re.compile(r"^field\[(?:[^\]]*,)?name=([^,\]]+)")


def _view_findings(ctx, module, model, md, up, out):
    vdiff = md["diff"]["views"]
    removed_fields = _removed_names(md, "fields", up)
    for xid, info in sorted(ctx.old.data[model][module]["views"].items()):
        loc, parent = info.get("loc"), info.get("inherit_id")
        seen = set()
        for sig in info["nodes"]:
            m = _FIELD_NODE.match(sig)
            if m and m.group(1) in removed_fields and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(finding("breaking", "view", "view-field-removed",
                                   f"the view `{xid}` uses the field `{m.group(1)}` of `{model}`, removed upstream",
                                   module, loc, xid, {"name": m.group(1), "model": model}))
        if not parent:
            continue
        gone = [e for e in vdiff["removed"] if e["name"] == parent and e["module"] in up]
        changed = [e for e in vdiff["modified"] if e["name"] == parent and e["module"] in up]
        if gone:
            out.append(finding("breaking", "view", "view-parent-removed",
                               f"the view `{xid}` inherits from `{parent}`, removed upstream", module, loc, xid,
                               {"name": parent, "model": model}))
        elif changed:
            arch = changed[0]["changes"].get("arch", {})
            rem, add = arch.get("nodes_removed", []), arch.get("nodes_added", [])
            out.append(finding("review", "view", "view-parent-modified",
                               f"the view `{xid}` inherits from `{parent}`, modified upstream "
                               f"({len(rem)} node(s) removed, {len(add)} added): check the xpath",
                               module, loc, xid, {"name": parent, "model": model,
                                                  "nodes_removed": rem[:20], "nodes_added": add[:20]}))
            for token in _removed_tokens(_xpath_tokens(info["nodes"]), rem, add):
                out.append(finding("breaking", "view", "view-xpath-target-removed",
                                   f"an xpath of `{xid}` targets `{token}`, a node removed from `{parent}` upstream",
                                   module, loc, xid, {"name": token, "view": parent, "model": model},
                                   confidence="heuristic"))


def _template_status(ctx, module, xid):
    """None | ("removed", {}) | ("modified", changes) for an upstream template referenced by the module."""
    old = ctx.old.qweb.get(xid)
    if old is None or old["module"] not in ctx.upstream_old[module]:
        return None
    new = ctx.new.qweb.get(xid)
    if new is None:
        return "removed", {}
    changes = cmp._cmp_template(old, new, Counter())
    return ("modified", changes) if changes else None


def _template_findings(ctx, module, out):
    for xid, info in sorted(ctx.old.qweb.items()):
        if info["module"] != module:
            continue
        loc = info.get("loc")
        parent = info.get("inherit_id")
        st = _template_status(ctx, module, parent) if parent else None
        if st and st[0] == "removed":
            moved = ctx.local_template_ids().get(parent.split(".", 1)[-1], [])
            hint = f" (exists as: {', '.join(moved[:3])})" if moved else ""
            out.append(finding("breaking", "template", "template-parent-removed",
                               f"the template `{xid}` inherits from `{parent}`, removed upstream{hint}", module, loc,
                               xid, {"name": parent, "moved_to": moved[:3]}))
        elif st:
            arch = st[1].get("arch", {})
            rem, add = arch.get("nodes_removed", []), arch.get("nodes_added", [])
            out.append(finding("review", "template", "template-parent-modified",
                               f"the template `{xid}` inherits from `{parent}`, modified upstream "
                               f"({len(rem)} node(s) removed, {len(add)} added): check the xpath",
                               module, loc, xid, {"name": parent, "nodes_removed": rem[:20],
                                                  "nodes_added": add[:20]}))
            for token in _removed_tokens(_xpath_tokens(info["nodes"]), rem, add):
                out.append(finding("breaking", "template", "template-xpath-target-removed",
                                   f"an xpath of `{xid}` targets `{token}`, a node removed from `{parent}` upstream",
                                   module, loc, xid, {"name": token, "template": parent}, confidence="heuristic"))
        for called in info.get("calls", []):
            cs = _template_status(ctx, module, called)
            if cs and cs[0] == "removed":
                out.append(finding("breaking", "template", "template-call-removed",
                                   f"the template `{xid}` calls `{called}` (t-call), removed upstream",
                                   module, loc, xid, {"name": called}))


def model_findings(ctx, module):
    """Findings on the models touched by the module: fields, methods, views, QWeb templates."""
    out = []
    up, newc = ctx.upstream_old[module], ctx.closure_new[module]
    for model in sorted(ctx.old.module_models.get(module, ())):
        if module not in ctx.old.data.get(model, {}):
            continue
        md = ctx.model_diff(model)
        if not any(m in up for m in md["old"]["modules"]):
            continue   # model specific to the custom module (or provided outside its closure)
        if not md["new"]["modules"]:
            attr = ctx.old.data[model][module]["attributes"].get("_inherit") or {}
            partial = bool(ctx.unresolved[module])   # unresolved dependencies: the model may be defined there
            out.append(finding("breaking", "model", "model-removed",
                               f"the model `{model}` no longer exists in the new version"
                               + (" (incomplete analysis: unresolved dependencies)" if partial else ""),
                               module, attr.get("loc"), model, {"name": model},
                               confidence="heuristic" if partial else "certain"))
            continue
        for section in ("fields", "methods"):
            _item_findings(ctx, module, model, section, md, up, newc, out)
        _view_findings(ctx, module, model, md, up, out)
    _template_findings(ctx, module, out)
    return out


# --------------------------------------------------------------------------
# Module-level rules: OWL, JS, assets
# --------------------------------------------------------------------------
_OPS = {"before", "after", "replace", "remove", "include", "prepend", "append"}
_ASSET_PATH = re.compile(r"^(\w+)/(static/.+)$")
_PLAIN = re.compile(r"^[\w$]+$")
_OWL_CALL = re.compile(r"^t\[(?:[^\]]*,)?t-call=([^,\]]+)")


def _up_diffs(ctx, module):
    return [(m, ctx.module_diffs[m]) for m in sorted(ctx.upstream_old[module]) if m in ctx.module_diffs]


def _owl_status(ctx, module, name):
    for _, d in _up_diffs(ctx, module):
        if any(e["name"] == name for e in d["owl_templates"]["removed"]):
            return "removed", {}
        for e in d["owl_templates"]["modified"]:
            if e["name"] == name:
                return "modified", e["changes"]
    return None


def _owl_findings(ctx, module, out):
    for key, info in sorted(ctx.old.mod[module]["owl_templates"].items()):
        loc, parent = info.get("loc"), info.get("t_inherit")
        st = _owl_status(ctx, module, parent) if parent else None
        if st and st[0] == "removed":
            out.append(finding("breaking", "owl", "owl-inherit-removed",
                               f"the OWL template `{key}` inherits from `{parent}` (t-inherit), removed upstream",
                               module, loc, key, {"name": parent}))
        elif st:
            out.append(finding("review", "owl", "owl-inherit-modified",
                               f"the OWL template `{key}` inherits from `{parent}` (t-inherit), modified upstream: "
                               f"check the xpath", module, loc, key, {"name": parent}))
        for sig in info.get("nodes", []):
            m = _OWL_CALL.match(sig)
            cs = _owl_status(ctx, module, m.group(1)) if m else None
            if cs and cs[0] == "removed":
                out.append(finding("breaking", "owl", "owl-call-removed",
                                   f"the OWL template `{key}` calls `{m.group(1)}` (t-call), removed upstream",
                                   module, loc, key, {"name": m.group(1)}))


def _method_findings(ctx, module, cls_name, methods, changes, loc, rule_prefix, out, what):
    """Methods of a class / patch that intersect the methods removed / modified upstream."""
    meth = changes.get("methods") or {}
    for name in sorted(methods):
        if name in meth.get("removed", []):
            out.append(finding("breaking", "js_class" if what == "class" else "js_patch",
                               f"{rule_prefix}-removed",
                               f"the {what} `{cls_name}` overrides `{name}`, removed from the upstream class", module,
                               loc, cls_name, {"name": name, "class": cls_name}))
        elif name in meth.get("changed", {}):
            ch = meth["changed"][name]
            out.append(finding("review", "js_class" if what == "class" else "js_patch",
                               f"{rule_prefix}-changed",
                               f"the {what} `{cls_name}` overrides `{name}`, whose parameters change upstream: "
                               f"{ch['old']} -> {ch['new']}", module, loc, cls_name,
                               {"name": name, "class": cls_name}))


def _js_findings(ctx, module, out):
    up, newc = ctx.upstream_old[module], ctx.closure_new[module]
    removed, moved, cls_mod = {}, {}, {}
    for m, d in _up_diffs(ctx, module):
        for e in d["js_exports"]["removed"]:
            removed[(e["path"], e["name"])] = m
        for mv in d["js_exports"]["moved"]:
            if mv["from"]["module"] == m:
                moved[(mv["from"]["path"], mv["name"])] = mv
        for e in d["js_classes"]["modified"]:
            cls_mod[(e["path"], e["name"])] = e["changes"]
    imports = ctx.old.js_imports.get(module, [])
    by_file = {}
    for imp in imports:
        if imp["local"]:
            by_file.setdefault(imp["file"], {})[imp["local"]] = (imp["source"], imp["imported"])

    for imp in imports:
        name, src = imp["imported"], imp["source"]
        if not name or name == "*":
            continue
        if (src, name) in moved:
            to = moved[(src, name)]["to"]
            hint = "" if to["module"] in newc else ": add this dependency to the manifest"
            out.append(finding("breaking", "js_import", "js-import-moved",
                               f"`{name}` imported from `{src}` is now exported by `{to['path']}`{hint}",
                               module, imp["loc"], name, {"name": name, "from_path": src, "to_path": to["path"],
                                                           "to_module": to["module"]}))
        elif (src, name) in removed:
            out.append(finding("breaking", "js_import", "js-import-removed",
                               f"`{name}` imported from `{src}` is no longer exported upstream", module, imp["loc"],
                               name, {"name": name, "path": src, "module": removed[(src, name)]}))
        elif (src, name) in cls_mod:
            meth = cls_mod[(src, name)].get("methods") or {}
            out.append(finding("review", "js_import", "js-class-modified",
                               f"the class `{name}` imported from `{src}` changed upstream "
                               f"(methods removed: {', '.join(meth.get('removed', [])) or '-'}; "
                               f"modified: {', '.join(meth.get('changed', {})) or '-'})", module, imp["loc"],
                               name, {"name": name, "path": src}))

    for key, data in sorted(ctx.old.mod[module]["js_classes"].items()):
        ext = data.get("extends")
        imp = by_file.get(data["loc"].rsplit(":", 1)[0], {}).get(ext) if ext and _PLAIN.match(ext) else None
        if imp and imp in cls_mod:
            _method_findings(ctx, module, key.split("::", 1)[1], data.get("methods", {}), cls_mod[imp],
                             data["loc"], "js-method", out, "class")
    for key, data in sorted(ctx.old.mod[module]["js_patches"].items()):
        ident = re.match(r"^([\w$]+)", key.split("::", 1)[1])
        imp = by_file.get(data["loc"].rsplit(":", 1)[0], {}).get(ident.group(1)) if ident else None
        if imp and imp in cls_mod:
            _method_findings(ctx, module, key.split("::", 1)[1], data.get("methods", []), cls_mod[imp],
                             data["loc"], "js-patch-method", out, "patch")

    old_keys = {k for d in up for k in ctx.old.mod[d]["js_registries"]}
    new_keys = {k for m in newc for k in ctx.new.mod[m]["js_registries"]}
    for key, data in sorted(ctx.old.mod[module]["js_registries"].items()):
        if key in old_keys and key not in new_keys:
            out.append(finding("review", "registry", "registry-key-removed",
                               f"the registry entry `{key}` overridden here is no longer registered by the "
                               f"dependencies (removed or moved)", module, data["loc"], key, {"name": key}))
        elif key not in old_keys and key in new_keys:
            out.append(finding("review", "registry", "registry-clash",
                               f"the registry key `{key}` is now also registered upstream: possible "
                               f"duplicate (DuplicatedKeyError without `force`)", module, data["loc"], key,
                               {"name": key}))


def _asset_findings(ctx, module, out):
    up = ctx.upstream_old[module]
    old_bundles = {b for d in up for b in ctx.old.mod[d]["assets"]}
    new_bundles = {b for m in list(ctx.new.mod) for b in ctx.new.mod[m]["assets"]}
    loc = f"{module}/__manifest__.py"
    reported = set()

    def bundle(name):
        if name in old_bundles and name not in new_bundles and name not in reported:
            reported.add(name)
            out.append(finding("breaking", "asset", "asset-bundle-removed",
                               f"the bundle `{name}` is no longer declared by the dependencies: check that it still "
                               f"exists", module, loc, name, {"name": name}, confidence="heuristic"))

    for b, entries in sorted(ctx.old.mod[module]["assets"].items()):
        bundle(b)
        for entry in entries:
            tokens = entry.split()
            op = tokens[0] if tokens and tokens[0] in _OPS else None
            if op == "include":
                bundle(tokens[1] if len(tokens) > 1 else "")
                continue
            if op == "remove":
                continue
            for arg in tokens[1:] if op else tokens:
                m = _ASSET_PATH.match(arg)
                if not m or "*" in arg or m.group(1) not in up:
                    continue
                diff = ctx.module_diffs.get(m.group(1))
                if diff and any(e["name"] == m.group(2) for e in diff["js_files"]["removed"]):
                    out.append(finding("breaking", "asset", "asset-file-removed",
                                       f"the bundle `{b}` references `{arg}`, a file removed upstream", module,
                                       loc, b, {"name": arg}))


def module_findings(ctx, module):
    """Module-level findings: OWL, JS imports / classes / patches / registries, assets."""
    out = []
    _owl_findings(ctx, module, out)
    _js_findings(ctx, module, out)
    _asset_findings(ctx, module, out)
    return out


# --------------------------------------------------------------------------
# XML ids / env.ref and textual mentions
# --------------------------------------------------------------------------
def xmlid_findings(ctx, module):
    """References of the module (XML, env.ref) to upstream XML ids that no longer exist in NEW."""
    out, seen = [], set()
    up = ctx.upstream_old[module]
    local = {}
    for xid in ctx.new.xml_ids:
        local.setdefault(xid.split(".", 1)[1], []).append(xid)
    for r in list(ctx.old.xml_refs.get(module, [])) + list(ctx.old.py_refs.get(module, [])):
        xid = r["xml_id"]
        if ctx.old.xml_ids.get(xid) not in up or xid in ctx.new.xml_ids:
            continue
        file = r["loc"].split(":")[0]
        if (xid, file) in seen:
            continue
        seen.add((xid, file))
        prefix, name = xid.split(".", 1)
        moved = [x for x in local.get(name, []) if x.split(".", 1)[0] != prefix]
        if moved:
            out.append(finding("breaking", "xml_ref", "xmlid-moved",
                               f"reference `{xid}` ({r['kind']}): the identifier is now `{moved[0]}`",
                               module, r["loc"], xid, {"name": xid, "kind": r["kind"], "to": moved[0]}))
        else:
            out.append(finding("breaking", "xml_ref", "xmlid-removed",
                               f"reference `{xid}` ({r['kind']}): the identifier no longer exists upstream", module,
                               r["loc"], xid, {"name": xid, "kind": r["kind"]}))
    return out


_MENTION_SKIP_DIRS = {"i18n", "i18n_extra", "tests", "test", "node_modules", "__pycache__", "migrations",
                      "upgrades", "lib"}


_PY_XML_JS = (".py", ".xml", ".js")


def _removed_upstream_names(ctx, module):
    """{name: (nature, extensions)} of the fields / methods / JS exports removed upstream (>= 4 characters).

    JS export names are searched only in .js files (an identical Python class name is not a mention).
    """
    up, names = ctx.upstream_old[module], {}
    for model in sorted(ctx.old.module_models.get(module, ())):
        if module not in ctx.old.data.get(model, {}):
            continue
        md = ctx.model_diff(model)
        if not any(m in up for m in md["old"]["modules"]) or not md["new"]["modules"]:
            continue
        for section, kind in (("fields", "field"), ("methods", "method")):
            for n in _removed_names(md, section, up):
                names.setdefault(n, (f"{kind} of {model}", _PY_XML_JS))
    for _, d in _up_diffs(ctx, module):
        for e in d["js_exports"]["removed"]:
            names.setdefault(e["name"], ("export JS", (".js",)))
    return {n: k for n, k in names.items() if len(n) >= 4 and n != "default"}


def mention_findings(ctx, module, existing):
    """Heuristic: textual mentions (whole word) of names removed upstream in the module's files."""
    names = _removed_upstream_names(ctx, module)
    if not names:
        return []
    rx = re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                    + r")(?![\w$])")
    covered = {(f["custom"]["loc"].split(":")[0], f["upstream"].get("name")) for f in existing
               if f["custom"]["loc"]}
    mpath = ctx.old.modules[module]
    base = os.path.dirname(mpath)
    hits = {}
    for dirpath, dirnames, filenames in os.walk(mpath):
        dirnames[:] = sorted(d for d in dirnames if d not in _MENTION_SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if not fn.endswith(_PY_XML_JS):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                for m in rx.finditer(line):
                    if (rel, m.group(1)) not in covered and fn.endswith(names[m.group(1)][1]):
                        hits.setdefault((rel, m.group(1)), []).append(i)
    out = []
    for (rel, name), lines in sorted(hits.items()):
        lines = sorted(set(lines))
        out.append(finding("review", "mention", "mention-removed-name",
                           f"mention of `{name}` ({names[name][0]}, removed upstream) at line(s) "
                           f"{', '.join(map(str, lines[:10]))}", module, f"{rel}:{lines[0]}", name,
                           {"name": name, "kind": names[name][0], "lines": lines[:10]}, confidence="heuristic"))
    return out


def all_findings(ctx, module):
    """All the rules for a custom module, without duplicates (already covered xml ids and mentions dropped)."""
    base = dependency_findings(ctx, module) + model_findings(ctx, module) + module_findings(ctx, module)
    covered = {(f["custom"]["loc"].split(":")[0], f["upstream"].get("name")) for f in base if f["custom"]["loc"]}
    xml = [f for f in xmlid_findings(ctx, module)
           if (f["custom"]["loc"].split(":")[0], f["upstream"].get("name")) not in covered]
    base += xml
    return base + mention_findings(ctx, module, base)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
LEVEL_LABELS = {"breaking": "Breaking", "review": "To review", "info": "Info"}
LIMITS = [
    "The use of a removed field, method or export in code (getattr, built names, domains) "
    "is detected only through textual mentions (heuristic).",
    "xpath expressions are not evaluated: the quoted strings are compared to the nodes removed from the parent arch (heuristic).",
    "An item moved to a module that already existed in OLD outside the dependency closure appears "
    "as removed.",
    "Static files other than JS and OWL templates (scss, images) are not tracked.",
    "Asset bundles declared only in templates may be reported by mistake.",
    "Generated identifiers (model_*, field_*) are not tracked.",
]


def _sort_key(f):
    return (LEVELS.index(f["level"]), f["category"], f["custom"]["loc"] or "", f["rule"],
            str(f["upstream"].get("name")))


def analyze(old_paths, new_paths, custom_paths, modules, old_label=None, new_label=None, progress=None):
    """odoo-impact/1 report for the requested custom modules."""
    ctx = build_context(old_paths, new_paths, custom_paths, modules, old_label, new_label, progress)
    report_modules = {}
    update = progress.task("Analyzing custom modules", len(modules)) if progress else None
    for done, m in enumerate(modules):
        if update:
            update(done, m)
        findings = sorted(all_findings(ctx, m), key=_sort_key)
        counts = Counter(f["level"] for f in findings)
        report_modules[m] = {
            "closure": {"old": sorted(ctx.closure_old[m]), "upstream": sorted(ctx.upstream_old[m]),
                        "missing_in_new": sorted(x for x in ctx.upstream_old[m] if x not in ctx.new.modules),
                        "unresolved": ctx.unresolved[m]},
            "counts": {**{lv: counts[lv] for lv in LEVELS}, "total": len(findings)},
            "findings": findings}
    if update:
        update(len(modules))
    stats = Counter()
    for d in ctx.module_diffs.values():
        stats.update(d["stats_total"])
    for md in ctx._model_diffs.values():
        stats.update(md["diff"]["stats_total"])
    limits = list(LIMITS)
    if not js.HAS_TREE_SITTER:
        limits.append("tree-sitter is not installed: the JS rules (imports, classes, patches, registries) "
                      "were not applied.")
    return {"format": "odoo-impact/1", "old_label": old_label, "new_label": new_label,
            "paths": {"old": list(old_paths), "new": list(new_paths), "custom": list(custom_paths)},
            "modules": report_modules,
            "upstream_stats": {k: stats[k] for k in cmp.STAT_KEYS if stats[k]},
            "limits": limits}


LEVEL_STYLE = {"breaking": ("red", "✖"), "review": ("yellow", "▲"), "info": ("blue", "●")}


def render_console(report, style=None):
    """Colored console report: banner, one block per module (counts + bar), findings by level."""
    st = style or term.Style()
    out = ["", st(" OMODI ", "bold", "white") + "  " + st("upgrade impact", "bold") + "  "
           + st(report.get("old_label") or "old", "cyan", "bold") + st(" ➜ ", "gray")
           + st(report.get("new_label") or "new", "green", "bold"), ""]
    for module, mod in report["modules"].items():
        c, cl = mod["counts"], mod["closure"]
        out.append(st.title(module))
        chips = "   ".join(st(f"{LEVEL_STYLE[lv][1]} {c[lv]} {LEVEL_LABELS[lv]}", LEVEL_STYLE[lv][0], "bold")
                           if c[lv] else st(f"{LEVEL_STYLE[lv][1]} 0 {LEVEL_LABELS[lv]}", "gray") for lv in LEVELS)
        out.append(f"  {chips}   " + st.bar([(c[lv], LEVEL_STYLE[lv][0]) for lv in LEVELS], 20))
        out.append("  " + st("deps ", "gray") + (", ".join(cl["upstream"]) or st("none", "gray"))
                   + st(f"  ({len(cl['upstream'])})", "gray"))
        if cl["missing_in_new"]:
            out.append("  " + st("✖ gone in new: ", "red", "bold") + st(", ".join(cl["missing_in_new"]), "red"))
        for level in LEVELS:
            items = [f for f in mod["findings"] if f["level"] == level]
            if not items:
                continue
            col, icon = LEVEL_STYLE[level]
            out += ["", f"  {st(icon + ' ' + LEVEL_LABELS[level].upper(), col, 'bold')} {st(f'({len(items)})', 'gray')}"]
            rows = []
            for f in items:
                up = f["upstream"]
                target = up.get("name", "") + (f" ➜ {up['to_module']}" if up.get("to_module") else "")
                note = st(" ~", "magenta") if f["confidence"] == "heuristic" else ""
                rows.append([st(f["category"], col), st(f["custom"]["loc"] or f["custom"]["module"], "cyan"),
                             st(target, "white") if target else st("-", "gray"), f["message"] + note])
            out += st.table(["category", "custom", "upstream", "finding"], rows, indent=4)
        out.append("")
    out.append(st("legend  ", "gray") + st("~", "magenta") + st(" = heuristic finding", "gray"))
    if report["limits"]:
        out += [""] + [st("  limits", "gray", "bold")] + [st(f"  · {x}", "gray") for x in report["limits"]]
    out.append("")
    return "\n".join(out)


CSV_COLUMNS = ["module", "level", "category", "rule", "confidence", "custom_loc", "custom_name",
               "upstream_name", "upstream_to_module", "message"]


def write_findings_csv(report, path):
    """One row per finding, all modules."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for module, mod in report["modules"].items():
            for f in mod["findings"]:
                up = f["upstream"]
                w.writerow([module, f["level"], f["category"], f["rule"], f["confidence"], f["custom"]["loc"] or "",
                            f["custom"]["name"] or "", up.get("name", ""), up.get("to_module", ""), f["message"]])


def main(argv=None):
    p = argparse.ArgumentParser(prog="omodi impact",
                                description="What to watch out for when upgrading custom modules")
    p.add_argument("--old", nargs="+", required=True, help="addons paths of the old version")
    p.add_argument("--new", nargs="+", required=True, help="addons paths of the new version")
    p.add_argument("--custom", nargs="+", default=[], help="custom addons paths (searched before --old)")
    p.add_argument("--modules", nargs="+", help="custom modules to analyze")
    p.add_argument("--modules-file", help="file listing the modules (one per line or comma-separated)")
    p.add_argument("--old-label", default="old", help="label of the old version (e.g. 20.0)")
    p.add_argument("--new-label", default="new", help="label of the new version (e.g. 21.0)")
    add_output_args(p, "the report (impact.json, impact.csv)")
    a = p.parse_args(argv)
    check_output_args(p, a)
    modules = read_modules(a)
    if not modules:
        p.error("give at least one module (--modules or --modules-file)")
    t0 = time.time()
    try:
        report = analyze(a.old, a.new, a.custom, modules, a.old_label, a.new_label,
                         Progress(enabled=not a.quiet))
    except ImpactError as e:
        p.error(str(e))
    print(render_console(report))   # the colored report always goes to the console
    written = []
    if wants(a, "json"):
        ex.write_json(report, os.path.join(a.output, "impact.json"))
        written.append("impact.json")
    if wants(a, "csv"):
        write_findings_csv(report, os.path.join(a.output, "impact.csv"))
        written.append("impact.csv")
    if written and not a.quiet:
        print(term.paint(f"✔ results ➜ {a.output}/ ", "green", "bold", on=term.color_enabled(sys.stderr))
              + f"({', '.join(written)}) in {time.time() - t0:.1f}s", file=sys.stderr)

if __name__ == "__main__":
    main()
