#!/usr/bin/env python3
"""
omodi extract — extracts the definition of Odoo models and modules.

Two output formats:
  odoo-model-extract/1  (-m MODEL)   : what each module contributes to a model
  odoo-module-extract/1 (--module M) : what depends on no model (templates, OWL, JS, assets)

Model format — for a given model (e.g. sale.order), all the modules of the addons paths, section by section:
  - attributes  : class attributes _xxx (_name, _inherit, _order, _rec_name...)
  - fields      : fields fields.Xxx(...) -> type + arguments
  - methods     : methods -> signature + decorators
  - constraints : models.Constraint / models.Index / models.UniqueIndex
  - views       : ir.ui.view related to the model (including inherited views without <field name="model">)
  - templates   : QWeb templates of the model's reports (ir.actions.report / <report>) and templates
                  inheriting from them (inherit_id chain); key = full xml_id

Module format — one JSON per module, sections:
  - qweb_templates : server QWeb templates (outside static/) not attached to a model report:
                     {inherit_id, primary, mode, priority, arch_hash, nodes, calls (sorted t-call), loc}
  - owl_templates  : templates OWL (static/src/**/*.xml), key = t-name, or "t-inherit|path" without t-name:
                     {t_inherit, t_inherit_mode, arch_hash, nodes, loc}
  - js_files       : static/src/**/*.js -> {sha1, lines, import_path, loc}   (stdlib)
  - js_exports     : key "<import_path>::<name>" -> {kind: class|function|const|reexport, loc}
  - js_classes     : key "<import_path>::<Class>" -> {extends, methods {name: params}, static
                     {template, props, components}, loc}
  - js_registries  : key "<a/b>::<key>" (registry.category("a").category("b").add) -> {value (<= 200 chars), loc}
  - js_patches     : key "<import_path>::<target>" (patch(target, {...})) -> {methods, properties, loc}
  - assets         : `assets` key of __manifest__.py: {bundle: [entries]}
  Excluded: static/tests, static/lib, static/src/**/tests. The import path is @<module>/<path under
  static/src without .js or /index> (`@odoo-module alias=...` alias honored; `@odoo-module ignore` excludes the file).
  js_exports / js_classes / js_registries / js_patches require tree-sitter (optional, see js.py);
  without it, a warning is shown and these sections stay empty (js_files is still filled).

Usage:
  omodi extract ODOO/addons ODOO/odoo/addons ENTERPRISE -m sale.order -o sale.order.json
  omodi extract ODOO/addons -m sale.order -m res.partner -o extracts/   # one JSON per model
  omodi extract ODOO/addons --module sale --module web -o extracts/     # extracts/modules/<module>.json
  omodi extract ODOO/addons --list-models sale stock                   # models touched by these modules
  omodi extract ...                                                            # same thing through the installed CLI

Output format (odoo-model-extract/1) :
{
  "format": "odoo-model-extract/1",
  "model": "sale.order",
  "label": "20.0",
  "modules": {
    "sale": {
      "attributes":  {"_order": "'date_order desc, id desc'", ...},
      "fields":      {"partner_id": {"type": "Many2one", "attrs": {"_arg0": "'res.partner'", "required": "True"}, "loc": "sale/models/sale_order.py:120"}},
      "methods":     {"action_confirm": {"signature": "self", "decorators": [], "loc": "..."}},
      "constraints": {"_date_order_id_idx": {"type": "Index", "args": ["'(date_order desc, id desc)'"], "loc": "..."}},
      "views":       {"sale.view_order_form": {"inherit_id": null, "type": "form", "mode": null, "priority": null,
                                               "arch_hash": "...", "nodes": ["form[string=Sales Order]", ...], "loc": "..."}},
      "templates":   {"sale.report_saleorder": {"inherit_id": null, "primary": null, "mode": null, "priority": null,
                                                 "arch_hash": "...", "nodes": [...], "calls": ["web.html_container"], "loc": "..."}}
    }
  }
}
Output format (odoo-module-extract/1) :
{"format": "odoo-module-extract/1", "module": "web", "label": "20.0", "paths": [...],
 "qweb_templates": {...}, "owl_templates": {...}, "js_files": {...}, "js_exports": {...}, "js_classes": {...},
 "js_registries": {...}, "js_patches": {...}, "assets": {"web.assets_backend": ["web/static/src/**/*", ...]}}

Stdlib only, Python >= 3.9 (tree-sitter and tree-sitter-javascript optional for structural JS).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

from . import js

FORMAT = "odoo-model-extract/1"
SKIP_DIRS = {"tests", "test", "static", "migrations", "upgrades", "i18n", "i18n_extra",
             "node_modules", "__pycache__"}
CONSTRAINT_TYPES = {"Constraint", "Index", "UniqueIndex"}
MODULE_FORMAT = "odoo-module-extract/1"
MODULE_SECTIONS = ("qweb_templates", "owl_templates", "js_files", "js_exports", "js_classes",
                   "js_registries", "js_patches", "assets")
# Under static/src these folders are not analyzed (static/tests and static/lib are never walked)
STATIC_SKIP_DIRS = {"tests", "lib", "node_modules"}
SECTIONS = ("attributes", "fields", "methods", "constraints", "views", "templates")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def find_modules(addons_paths):
    """{module_name: path} for each folder containing a __manifest__.py."""
    modules = {}
    for root in addons_paths:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            if "__manifest__.py" in filenames:
                name = os.path.basename(dirpath)
                if name in modules:
                    print(f"[warn] module {name} found twice, keeping {modules[name]}",
                          file=sys.stderr)
                else:
                    modules[name] = dirpath
                dirnames[:] = []
    return modules


def _iter_files(module_path, ext):
    for dirpath, dirnames, filenames in os.walk(module_path):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(filenames):
            if f.endswith(ext):
                yield os.path.join(dirpath, f)


def static_files(module_path):
    """Yield (absolute_path, path_relative_to_static/src) for static/src/**, excluding tests/lib."""
    base = os.path.join(module_path, "static", "src")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in STATIC_SKIP_DIRS and not d.startswith("."))
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            yield full, os.path.relpath(full, base).replace(os.sep, "/")


def _loc(path, module_path, line=None):
    rel = os.path.relpath(path, os.path.dirname(module_path))
    return f"{rel}:{line}" if line else rel


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------
def _norm(node):
    return " ".join(ast.unparse(node).split())


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _class_model(cls):
    name = inherit = None
    for st in cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            if st.targets[0].id == "_name":
                name = _literal(st.value)
            elif st.targets[0].id == "_inherit":
                inherit = _literal(st.value)
    if isinstance(name, str):
        return name
    if isinstance(inherit, str):
        return inherit
    if isinstance(inherit, (list, tuple)) and inherit and isinstance(inherit[0], str):
        return inherit[0]
    return None


def _call_kind(value):
    """('field', type) for fields.X(...), ('constraint', type) for models.Constraint(...)."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
            and isinstance(value.func.value, ast.Name):
        mod, attr = value.func.value.id, value.func.attr
        if mod == "fields":
            return "field", attr
        if mod == "models" and attr in CONSTRAINT_TYPES:
            return "constraint", attr
    return None, None


def parse_python(path, module_path):
    """Yield (model, section, name, data)."""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"[warn] cannot parse {path}: {e}", file=sys.stderr)
        return
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        model = _class_model(cls)
        if not model:
            continue
        for st in cls.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield model, "methods", st.name, {
                    "signature": _norm(st.args),
                    "decorators": [_norm(d) for d in st.decorator_list],
                    "loc": _loc(path, module_path, st.lineno),
                }
                continue
            if isinstance(st, ast.Assign):
                targets, value = st.targets, st.value
            elif isinstance(st, ast.AnnAssign) and st.value is not None:
                targets, value = [st.target], st.value
            else:
                continue
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            kind, typ = _call_kind(value)
            loc = _loc(path, module_path, st.lineno)
            if kind == "field":
                attrs = {f"_arg{i}": _norm(a) for i, a in enumerate(value.args)}
                for kw in value.keywords:
                    attrs[kw.arg or "**"] = _norm(kw.value)
                for n in names:
                    yield model, "fields", n, {"type": typ, "attrs": attrs, "loc": loc}
            elif kind == "constraint":
                data = {"type": typ, "args": [_norm(a) for a in value.args]
                        + [f"{k.arg}={_norm(k.value)}" for k in value.keywords], "loc": loc}
                for n in names:
                    yield model, "constraints", n, data
            else:
                for n in names:
                    if n.startswith("_") and not n.startswith("__"):
                        yield model, "attributes", n, {"value": _norm(value), "loc": loc}


# --------------------------------------------------------------------------
# XML
# --------------------------------------------------------------------------
_REF_EVAL = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _full_id(module, xid):
    if not xid:
        return None
    return xid if "." in xid else f"{module}.{xid}"


def _arch(elements, text):
    wrap = ET.Element("arch")
    wrap.text = (text or "").strip() or None
    for e in elements:
        wrap.append(e)
    raw = ET.tostring(wrap, encoding="unicode")
    try:
        canon = ET.canonicalize(raw, strip_text=True, with_comments=False)
    except Exception:
        canon = raw
    nodes = []
    for el in wrap.iter():
        if el is wrap or not isinstance(el.tag, str):
            continue
        nodes.append(el.tag + "[" + ",".join(f"{k}={v}" for k, v in sorted(el.attrib.items())) + "]")
    return hashlib.sha1(canon.encode()).hexdigest(), nodes


def parse_xml(path):
    """XML root of the file, or None (with a warning) if invalid."""
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, UnicodeDecodeError) as e:
        print(f"[warn] invalid XML {path}: {e}", file=sys.stderr)
        return None


def parse_views(path, module, module_path, root=None):
    """Yield (xml_id, info) for each ir.ui.view (QWeb templates without a model are ignored)."""
    root = root if root is not None else parse_xml(path)
    if root is None:
        return
    for rec in root.iter("record"):
        if rec.get("model") != "ir.ui.view" or not rec.get("id"):
            continue
        info = {"model": None, "inherit_id": None, "type": None, "mode": None, "priority": None}
        arch = None
        for f in rec.findall("field"):
            n = f.get("name")
            if n == "model":
                info["model"] = (f.text or "").strip() or None
            elif n == "inherit_id":
                ref = f.get("ref")
                if not ref and f.get("eval"):
                    m = _REF_EVAL.search(f.get("eval"))
                    ref = m.group(1) if m else None
                info["inherit_id"] = _full_id(module, ref)
            elif n in ("type", "mode", "priority"):
                info[n] = (f.text or f.get("eval") or "").strip() or None
            elif n == "arch":
                arch = f
        info["arch_hash"], info["nodes"] = _arch(list(arch), arch.text) if arch is not None else ("", [])
        info["loc"] = _loc(path, module_path)
        yield _full_id(module, rec.get("id")), info


def _ref_of(field, module):
    ref = field.get("ref")
    if not ref and field.get("eval"):
        m = _REF_EVAL.search(field.get("eval"))
        ref = m.group(1) if m else None
    return _full_id(module, ref)


def _calls(elements):
    """Sorted t-call targets (no duplicates) of these elements and their descendants."""
    return sorted({e.get("t-call") for el in elements for e in el.iter() if e.get("t-call")})


def parse_qweb(path, module, module_path, root=None):
    """Yield (xml_id, info) for server QWeb templates: <template> and ir.ui.view of type qweb.

    info = {inherit_id, primary, mode, priority, arch_hash, nodes, calls, loc}
    """
    root = root if root is not None else parse_xml(path)
    if root is None:
        return
    loc = _loc(path, module_path)
    for tpl in root.iter("template"):
        if not tpl.get("id"):
            continue
        h, nodes = _arch(list(tpl), tpl.text)
        yield _full_id(module, tpl.get("id")), {
            "inherit_id": _full_id(module, tpl.get("inherit_id")),
            "primary": tpl.get("primary"), "mode": tpl.get("mode"), "priority": tpl.get("priority"),
            "arch_hash": h, "nodes": nodes, "calls": _calls(list(tpl)), "loc": loc}
    for rec in root.iter("record"):
        if rec.get("model") != "ir.ui.view" or not rec.get("id"):
            continue
        fields = {f.get("name"): f for f in rec.findall("field")}
        if (fields.get("type") is None or (fields["type"].text or "").strip() != "qweb"):
            continue
        arch = fields.get("arch")
        h, nodes = _arch(list(arch), arch.text) if arch is not None else ("", [])
        txt = lambda n: ((fields[n].text or fields[n].get("eval") or "").strip() or None) if n in fields else None
        yield _full_id(module, rec.get("id")), {
            "inherit_id": _ref_of(fields["inherit_id"], module) if "inherit_id" in fields else None,
            "primary": None, "mode": txt("mode"), "priority": txt("priority"),
            "arch_hash": h, "nodes": nodes, "calls": _calls(list(arch)) if arch is not None else [],
            "loc": loc}


def parse_reports(path, module, root=None):
    """Yield (full report_name, model) for ir.actions.report (records and <report> tags)."""
    root = root if root is not None else parse_xml(path)
    if root is None:
        return
    for rec in root.iter("record"):
        if rec.get("model") != "ir.actions.report":
            continue
        vals = {f.get("name"): (f.text or "").strip() for f in rec.findall("field")}
        if vals.get("model") and vals.get("report_name"):
            yield _full_id(module, vals["report_name"]), vals["model"]
    for rep in root.iter("report"):
        if rep.get("model") and rep.get("name"):
            yield _full_id(module, rep.get("name")), rep.get("model")


def parse_owl(path, module, module_path, root=None):
    """Yield (key, info) for the OWL templates of a static/src/**/*.xml.

    Key: t-name, or "t-inherit|path relative to the module" if the template has no t-name.
    info = {t_inherit, t_inherit_mode, arch_hash, nodes, loc}
    """
    root = root if root is not None else parse_xml(path)
    if root is None:
        return
    rel = os.path.relpath(path, module_path).replace(os.sep, "/")
    cands = [root] if (root.get("t-name") or root.get("t-inherit")) else list(root)
    for el in cands:
        if not isinstance(el.tag, str):
            continue
        name, inherit = el.get("t-name"), el.get("t-inherit")
        if not name and not inherit:
            continue
        h, nodes = _arch([el], None)
        yield (name or f"{inherit}|{rel}"), {
            "t_inherit": inherit, "t_inherit_mode": el.get("t-inherit-mode"),
            "arch_hash": h, "nodes": nodes, "loc": _loc(path, module_path)}


def read_depends(manifest_path):
    """`depends` key of __manifest__.py (list of names); [] if missing or unreadable (warning)."""
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=manifest_path)
        node = next((v for st in tree.body if isinstance(st, ast.Expr) and isinstance(st.value, ast.Dict)
                     for k, v in zip(st.value.keys, st.value.values)
                     if isinstance(k, ast.Constant) and k.value == "depends"), None)
        return [str(d) for d in ast.literal_eval(node)] if node is not None else []
    except (SyntaxError, ValueError, TypeError, UnicodeDecodeError, OSError) as e:
        print(f"[warn] unreadable depends in {manifest_path}: {e}", file=sys.stderr)
        return []


ID_TAGS = {"record", "template", "menuitem", "report", "act_window"}
_REF_ATTRS = {"inherit_id": "inherit_id", "parent": "parent", "action": "action",
              "t-call": "t-call", "t-inherit": "t-inherit"}
_PCT_REF = re.compile(r"%\(([\w.\-]+)\)d")
_XMLID = re.compile(r"^[\w.\-]+$")


def xml_refs_of(root, module, path, module_path):
    """References to other modules' XML ids in an XML file (ref, inherit_id, groups, %(x.y)d...).

    Ids without prefix or prefixed by the module itself are ignored. No line number (ElementTree).
    """
    loc, out = _loc(path, module_path), []

    def add(xid, kind):
        xid = (xid or "").strip()
        if _XMLID.match(xid) and "." in xid and not xid.startswith(module + "."):
            out.append({"xml_id": xid, "kind": kind, "loc": loc})

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr, kind in _REF_ATTRS.items():
            add(el.get(attr), kind)
        if el.tag == "field" and el.get("ref"):
            add(el.get("ref"), el.get("name") or "ref")
        for g in (el.get("groups") or "").split(","):
            add(g.strip().lstrip("!-"), "groups")
        for m in _REF_EVAL.finditer(el.get("eval") or ""):
            add(m.group(1), "eval-ref")
        for text in list(el.attrib.values()) + [el.text or ""]:
            for m in _PCT_REF.finditer(text):
                add(m.group(1), "action")
    return out


def py_refs_of(path, module, module_path):
    """`env.ref("x.y")` with a string literal in a Python file -> [{xml_id, kind: env.ref, loc}]."""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "ref" and node.args):
            continue
        base = node.func.value
        is_env = (isinstance(base, ast.Name) and base.id == "env") or \
                 (isinstance(base, ast.Attribute) and base.attr == "env")
        arg = node.args[0]
        if is_env and isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                and "." in arg.value and not arg.value.startswith(module + "."):
            out.append({"xml_id": arg.value, "kind": "env.ref", "loc": _loc(path, module_path, node.lineno)})
    return out


def parse_assets(manifest_path):
    """`assets` key of __manifest__.py: {bundle: [entries]} (tuples flattened to "op arg arg")."""
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=manifest_path)
        node = next((v for st in tree.body if isinstance(st, ast.Expr) and isinstance(st.value, ast.Dict)
                     for k, v in zip(st.value.keys, st.value.values)
                     if isinstance(k, ast.Constant) and k.value == "assets"), None)
        if node is None:
            return {}
        raw = ast.literal_eval(node)
        return {str(bundle): [e if isinstance(e, str) else " ".join(str(x) for x in e) for e in entries]
                for bundle, entries in raw.items()}
    except (SyntaxError, ValueError, TypeError, AttributeError, UnicodeDecodeError, OSError) as e:
        print(f"[warn] unreadable assets in {manifest_path}: {e}", file=sys.stderr)
        return {}


# --------------------------------------------------------------------------
# Index: a single walk, then extraction per model
# --------------------------------------------------------------------------
class Index:
    def __init__(self, addons_paths, label=None, only=None, focus=None, scan=True, progress=None):
        """only: modules to scan (None = all). focus: modules whose references are also collected
        (XML ids, env.ref, JS imports) for the impact analysis. scan=False: discovery + depends only,
        then call .scan(only, focus)."""
        self.paths = list(addons_paths)
        self.label = label
        self.progress = progress   # optional callable(factory): factory(total) -> update(done, name)
        self.modules = find_modules(addons_paths)
        self.depends = {m: read_depends(os.path.join(p, "__manifest__.py")) for m, p in self.modules.items()}
        self.xml_ids = {}                        # full id -> module (scanned modules)
        self.xml_refs = defaultdict(list)        # focus: XML references to other modules
        self.py_refs = defaultdict(list)         # focus: env.ref("x.y")
        self.js_imports = defaultdict(list)      # focus: JS imports (tree-sitter)
        self.only, self.focus = None, set()
        # data[model][module][section][name] = {...}
        self.data = defaultdict(lambda: defaultdict(lambda: {s: {} for s in SECTIONS}))
        self.module_models = defaultdict(set)
        self.mod = defaultdict(lambda: {s: {} for s in MODULE_SECTIONS})   # per-module extraction
        self.qweb = {}              # xml_id -> info (+ "module"), all server QWeb templates
        self.js_paths = {}          # (module, "static/src/...js") -> absolute path (lines via difflib)
        self.qweb_attached = set()  # those attached to a model through a report
        if scan:
            self.scan(only, focus)

    def scan(self, only=None, focus=None):
        self.only = None if only is None else set(only)
        self.focus = set(focus or ())
        self._scan()

    def _scan(self):
        all_views = {}
        reports = []   # (report_name, model)
        todo = [(m, p) for m, p in sorted(self.modules.items()) if self.only is None or m in self.only]
        update = self.progress(len(todo)) if self.progress else None
        for done, (module, mpath) in enumerate(todo):
            if update:
                update(done, module)
            for py in _iter_files(mpath, ".py"):
                for model, section, name, data in parse_python(py, mpath):
                    bucket = self.data[model][module][section]
                    if section == "attributes" and name == "_inherit" and name in bucket:
                        # several classes in the same module: union of the _inherit
                        bucket[name]["value"] = bucket[name]["value"] + " | " + data["value"]
                    elif section == "fields" and name in bucket:
                        bucket[name]["attrs"].update(data["attrs"])
                        bucket[name]["type"] = data["type"]
                    else:
                        bucket[name] = data
                    self.module_models[module].add(model)
                if module in self.focus:
                    self.py_refs[module].extend(py_refs_of(py, module, mpath))
            for xml in _iter_files(mpath, ".xml"):
                root = parse_xml(xml)
                if root is None:
                    continue
                for xid, info in parse_views(xml, module, mpath, root):
                    info["module"] = module
                    all_views[xid] = info
                for xid, info in parse_qweb(xml, module, mpath, root):
                    info["module"] = module
                    self.qweb[xid] = info
                reports.extend(parse_reports(xml, module, root))
                for el in root.iter():
                    if el.tag in ID_TAGS and el.get("id"):
                        self.xml_ids[_full_id(module, el.get("id"))] = module
                if module in self.focus:
                    self.xml_refs[module].extend(xml_refs_of(root, module, xml, mpath))
            self._scan_static(module, mpath)
            self.mod[module]["assets"] = parse_assets(os.path.join(mpath, "__manifest__.py"))
        if update:
            update(len(todo))
        # Resolve the model of inherited views that do not specify it
        for xid, info in all_views.items():
            model, seen, cur = info["model"], set(), info
            while not model and cur and cur.get("inherit_id") and cur["inherit_id"] not in seen:
                seen.add(cur["inherit_id"])
                cur = all_views.get(cur["inherit_id"])
                model = cur and cur.get("model")
            if not model:
                continue
            module = info.pop("module")
            info.pop("model", None)
            self.data[model][module]["views"][xid] = info
            self.module_models[module].add(model)
        self._attach_report_templates(reports)

    def _scan_static(self, module, mpath):
        """static/src : OWL templates (.xml) and JS files (read once)."""
        mod = self.mod[module]
        for full, rel in static_files(mpath):
            if rel.endswith(".xml"):
                root = parse_xml(full)
                if root is not None:
                    for key, info in parse_owl(full, module, mpath, root):
                        mod["owl_templates"][key] = info
            elif rel.endswith(".js"):
                self._scan_js(module, mpath, full, rel)

    def _scan_js(self, module, mpath, full, rel):
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as e:
            print(f"[warn] cannot read {full}: {e}", file=sys.stderr)
            return
        mod, loc = self.mod[module], _loc(full, mpath)
        ip = js.import_path(module, rel, data[:1000].decode("utf-8", "replace"))
        key = f"static/src/{rel}"
        mod["js_files"][key] = {**js.file_info(data), "import_path": ip, "loc": loc}
        self.js_paths[(module, key)] = full
        if ip is None:   # `@odoo-module ignore`: not a module, nothing structural
            return
        focus = module in self.focus
        res = js.analyze(data, ip, loc, with_imports=focus)
        if focus:
            for imp in res.get("imports", []):
                imp["source"] = js.resolve_source(imp["source"], module, rel)
                imp["file"] = loc
                self.js_imports[module].append(imp)
        for section, k in (("js_exports", "exports"), ("js_classes", "classes"),
                           ("js_registries", "registries"), ("js_patches", "patches")):
            mod[section].update(res[k])

    def _attach_report_templates(self, reports):
        """Attaches report templates and those inheriting from them (inherit_id chain) to models."""
        roots = defaultdict(set)   # xml_id of the main template -> models
        for rname, model in reports:
            if rname in self.qweb:
                roots[rname].add(model)
            else:
                print(f"[warn] report {rname} ({model}): template not found", file=sys.stderr)
        for xid, info in self.qweb.items():
            models, cur, seen = set(), xid, set()
            while cur and cur not in seen:
                seen.add(cur)
                models |= roots.get(cur, set())
                cur = (self.qweb.get(cur) or {}).get("inherit_id")
            for model in models:
                data = {k: v for k, v in info.items() if k != "module"}
                self.data[model][info["module"]]["templates"][xid] = data
                self.module_models[info["module"]].add(model)
            if models:
                self.qweb_attached.add(xid)
            else:
                self.mod[info["module"]]["qweb_templates"][xid] = {k: v for k, v in info.items()
                                                                   if k != "module"}

    def models_of(self, modules):
        out = set()
        for m in modules:
            out |= self.module_models.get(m, set())
        return out

    def extract_module(self, module):
        """odoo-module-extract/1 extraction of a module, or None if the module is unknown."""
        if module not in self.modules:
            return None
        sections = self.mod[module]
        return {"format": MODULE_FORMAT, "module": module, "label": self.label, "paths": self.paths,
                **{s: (dict(sorted(sections[s].items())) if s != "assets" else sections[s])
                   for s in MODULE_SECTIONS}}

    def extract(self, model):
        mods = self.data.get(model, {})
        return {
            "format": FORMAT,
            "model": model,
            "label": self.label,
            "paths": self.paths,
            "modules": {m: {s: dict(sorted(v[s].items())) for s in SECTIONS}
                        for m, v in sorted(mods.items())},
        }


def write_json(obj, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def model_filename(model):
    return f"{model}.json"


def module_filename(module):
    """Relative path (inside an output folder) of a module's extraction."""
    return os.path.join("modules", f"{module}.json")


def main(argv=None):
    p = argparse.ArgumentParser(prog="omodi extract", description="Extract the definition of Odoo models")
    p.add_argument("paths", nargs="+", help="addons paths (community, enterprise, custom...)")
    p.add_argument("-m", "--model", action="append", default=[], help="model to extract (repeatable)")
    p.add_argument("--module", action="append", default=[],
                   help="module whose QWeb/OWL templates, JS and assets are extracted (repeatable)")
    p.add_argument("-o", "--output", help="output .json file (single target) or folder "
                                          "(modules in OUTPUT/modules/)")
    p.add_argument("--label", help="version label stored in the output (e.g. 20.0)")
    p.add_argument("--list-models", nargs="+", metavar="MODULE",
                   help="print the models touched by these modules and exit")
    a = p.parse_args(argv)

    idx = Index(a.paths, a.label)
    if a.list_models:
        for m in sorted(idx.models_of(a.list_models)):
            print(m)
        return
    if not (a.model or a.module) or not a.output:
        p.error("-m/--model or --module, and -o/--output are required (except with --list-models)")

    single = len(a.model) + len(a.module) == 1 and a.output.endswith(".json")
    for model in a.model:
        ext = idx.extract(model)
        if not ext["modules"]:
            print(f"[warn] model {model} not found", file=sys.stderr)
        out = a.output if single else os.path.join(a.output, model_filename(model))
        write_json(ext, out)
        n = {s: sum(len(v[s]) for v in ext["modules"].values()) for s in SECTIONS}
        print(f"{model}: {len(ext['modules'])} modules, {n['fields']} fields, "
              f"{n['methods']} methods, {n['views']} views -> {out}")
    for module in a.module:
        ext = idx.extract_module(module)
        if ext is None:
            print(f"[warn] module {module} not found", file=sys.stderr)
            continue
        out = a.output if single else os.path.join(a.output, module_filename(module))
        write_json(ext, out)
        print(f"{module}: {len(ext['qweb_templates'])} templates QWeb, {len(ext['owl_templates'])} templates OWL, "
              f"{len(ext['js_files'])} JS files, {len(ext['js_exports'])} exports, "
              f"{len(ext['js_classes'])} classes -> {out}")


if __name__ == "__main__":
    main()
