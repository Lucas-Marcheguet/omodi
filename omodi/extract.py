#!/usr/bin/env python3
"""
omodi extract — extrait la définition de modèles et de modules Odoo.

Deux formats de sortie :
  odoo-model-extract/1  (-m MODEL)   : ce que chaque module apporte à un modèle
  odoo-module-extract/1 (--module M) : ce qui ne dépend d'aucun modèle (templates, OWL, JS, assets)

Format modèle — pour un modèle donné (ex. sale.order), tous les modules des chemins d'addons, section par section :
  - attributes  : attributs de classe _xxx (_name, _inherit, _order, _rec_name...)
  - fields      : champs fields.Xxx(...) -> type + arguments
  - methods     : méthodes -> signature + décorateurs
  - constraints : models.Constraint / models.Index / models.UniqueIndex
  - views       : ir.ui.view liées au modèle (y compris vues héritées sans <field name="model">)
  - templates   : templates QWeb des rapports (ir.actions.report / <report>) du modèle et templates
                  qui en héritent (chaîne inherit_id) ; clé = xml_id complet

Format module — un JSON par module, sections :
  - qweb_templates : templates QWeb serveur (hors static/) non rattachés à un rapport de modèle :
                     {inherit_id, primary, mode, priority, arch_hash, nodes, calls (t-call triés), loc}
  - owl_templates  : templates OWL (static/src/**/*.xml), clé = t-name, ou "t-inherit|chemin" sans t-name :
                     {t_inherit, t_inherit_mode, arch_hash, nodes, loc}
  - js_files       : static/src/**/*.js -> {sha1, lines, import_path, loc}   (stdlib)
  - js_exports     : clé "<chemin_import>::<nom>" -> {kind: class|function|const|reexport, loc}
  - js_classes     : clé "<chemin_import>::<Classe>" -> {extends, methods {nom: params}, static
                     {template, props, components}, loc}
  - js_registries  : clé "<a/b>::<clé>" (registry.category("a").category("b").add) -> {value (<= 200 car.), loc}
  - js_patches     : clé "<chemin_import>::<cible>" (patch(cible, {...})) -> {methods, properties, loc}
  - assets         : clé `assets` du __manifest__.py : {bundle: [entrées]}
  Exclus : static/tests, static/lib, static/src/**/tests. Le chemin d'import est @<module>/<chemin sous
  static/src sans .js ni /index> (alias `@odoo-module alias=...` respecté ; `@odoo-module ignore` exclut le fichier).
  js_exports / js_classes / js_registries / js_patches demandent tree-sitter (optionnel, voir js.py) ;
  sans lui, un warning est affiché et ces sections restent vides (js_files reste rempli).

Usage :
  omodi extract ODOO/addons ODOO/odoo/addons ENTERPRISE -m sale.order -o sale.order.json
  omodi extract ODOO/addons -m sale.order -m res.partner -o extracts/   # un JSON par modèle
  omodi extract ODOO/addons --module sale --module web -o extracts/     # extracts/modules/<module>.json
  omodi extract ODOO/addons --list-models sale stock                   # modèles touchés par ces modules
  omodi extract ...                                                            # même chose via le CLI installé

Format de sortie (odoo-model-extract/1) :
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
Format de sortie (odoo-module-extract/1) :
{"format": "odoo-module-extract/1", "module": "web", "label": "20.0", "paths": [...],
 "qweb_templates": {...}, "owl_templates": {...}, "js_files": {...}, "js_exports": {...}, "js_classes": {...},
 "js_registries": {...}, "js_patches": {...}, "assets": {"web.assets_backend": ["web/static/src/**/*", ...]}}

Stdlib uniquement, Python >= 3.9 (tree-sitter et tree-sitter-javascript optionnels pour le JS structurel).
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
# Sous static/src, ces dossiers ne sont pas analysés (static/tests et static/lib ne sont jamais parcourus)
STATIC_SKIP_DIRS = {"tests", "lib", "node_modules"}
SECTIONS = ("attributes", "fields", "methods", "constraints", "views", "templates")


# --------------------------------------------------------------------------
# Découverte
# --------------------------------------------------------------------------
def find_modules(addons_paths):
    """{nom_module: chemin} pour chaque dossier contenant un __manifest__.py."""
    modules = {}
    for root in addons_paths:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            if "__manifest__.py" in filenames:
                name = os.path.basename(dirpath)
                if name in modules:
                    print(f"[warn] module {name} présent deux fois, on garde {modules[name]}",
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
    """Yield (chemin_absolu, chemin_relatif_à_static/src) pour static/src/**, hors tests/lib."""
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
    """('field', type) pour fields.X(...), ('constraint', type) pour models.Constraint(...)."""
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
        print(f"[warn] parse impossible {path}: {e}", file=sys.stderr)
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
    """Racine XML du fichier, ou None (avec un warning) si invalide."""
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, UnicodeDecodeError) as e:
        print(f"[warn] XML invalide {path}: {e}", file=sys.stderr)
        return None


def parse_views(path, module, module_path, root=None):
    """Yield (xml_id, info) pour chaque ir.ui.view (les templates QWeb sans modèle sont ignorés)."""
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
    """Cibles t-call triées (sans doublon) de ces éléments et de leurs descendants."""
    return sorted({e.get("t-call") for el in elements for e in el.iter() if e.get("t-call")})


def parse_qweb(path, module, module_path, root=None):
    """Yield (xml_id, info) pour les templates QWeb serveur : <template> et ir.ui.view de type qweb.

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
    """Yield (report_name complet, modèle) pour les ir.actions.report (records et balises <report>)."""
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
    """Yield (clé, info) pour les templates OWL d'un fichier static/src/**/*.xml.

    Clé : t-name, ou "t-inherit|chemin relatif au module" si le template n'a pas de t-name.
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
    """Clé `depends` du __manifest__.py (liste de noms) ; [] si absente ou illisible (warning)."""
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=manifest_path)
        node = next((v for st in tree.body if isinstance(st, ast.Expr) and isinstance(st.value, ast.Dict)
                     for k, v in zip(st.value.keys, st.value.values)
                     if isinstance(k, ast.Constant) and k.value == "depends"), None)
        return [str(d) for d in ast.literal_eval(node)] if node is not None else []
    except (SyntaxError, ValueError, TypeError, UnicodeDecodeError, OSError) as e:
        print(f"[warn] depends illisibles dans {manifest_path}: {e}", file=sys.stderr)
        return []


ID_TAGS = {"record", "template", "menuitem", "report", "act_window"}
_REF_ATTRS = {"inherit_id": "inherit_id", "parent": "parent", "action": "action",
              "t-call": "t-call", "t-inherit": "t-inherit"}
_PCT_REF = re.compile(r"%\(([\w.\-]+)\)d")
_XMLID = re.compile(r"^[\w.\-]+$")


def xml_refs_of(root, module, path, module_path):
    """Références à des XML ids d'autres modules dans un fichier XML (ref, inherit_id, groups, %(x.y)d...).

    Les ids sans préfixe ou préfixés par le module lui-même sont ignorés. Pas de numéro de ligne (ElementTree).
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
    """`env.ref("x.y")` à littéral chaîne dans un fichier Python -> [{xml_id, kind: env.ref, loc}]."""
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
    """Clé `assets` du __manifest__.py : {bundle: [entrées]} (tuples aplatis en "op arg arg")."""
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
        print(f"[warn] assets illisibles dans {manifest_path}: {e}", file=sys.stderr)
        return {}


# --------------------------------------------------------------------------
# Index : un seul parcours, puis extraction par modèle
# --------------------------------------------------------------------------
class Index:
    def __init__(self, addons_paths, label=None, only=None, focus=None, scan=True):
        """only : modules à scanner (None = tous). focus : modules dont on collecte aussi les références
        (XML ids, env.ref, imports JS) pour l'analyse d'impact. scan=False : découverte + depends seulement,
        puis appeler .scan(only, focus)."""
        self.paths = list(addons_paths)
        self.label = label
        self.modules = find_modules(addons_paths)
        self.depends = {m: read_depends(os.path.join(p, "__manifest__.py")) for m, p in self.modules.items()}
        self.xml_ids = {}                        # id complet -> module (modules scannés)
        self.xml_refs = defaultdict(list)        # focus : références XML vers d'autres modules
        self.py_refs = defaultdict(list)         # focus : env.ref("x.y")
        self.js_imports = defaultdict(list)      # focus : imports JS (tree-sitter)
        self.only, self.focus = None, set()
        # data[model][module][section][name] = {...}
        self.data = defaultdict(lambda: defaultdict(lambda: {s: {} for s in SECTIONS}))
        self.module_models = defaultdict(set)
        self.mod = defaultdict(lambda: {s: {} for s in MODULE_SECTIONS})   # extraction par module
        self.qweb = {}              # xml_id -> info (+ "module"), tous les templates QWeb serveur
        self.js_paths = {}          # (module, "static/src/...js") -> chemin absolu (lignes via difflib)
        self.qweb_attached = set()  # ceux rattachés à un modèle via un rapport
        if scan:
            self.scan(only, focus)

    def scan(self, only=None, focus=None):
        self.only = None if only is None else set(only)
        self.focus = set(focus or ())
        self._scan()

    def _scan(self):
        all_views = {}
        reports = []   # (report_name, model)
        for module, mpath in sorted(self.modules.items()):
            if self.only is not None and module not in self.only:
                continue
            for py in _iter_files(mpath, ".py"):
                for model, section, name, data in parse_python(py, mpath):
                    bucket = self.data[model][module][section]
                    if section == "attributes" and name == "_inherit" and name in bucket:
                        # plusieurs classes du même module : union des _inherit
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
        # Résolution du modèle des vues héritées qui ne le précisent pas
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
        """static/src : templates OWL (.xml) et fichiers JS (lus une seule fois)."""
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
            print(f"[warn] lecture impossible {full}: {e}", file=sys.stderr)
            return
        mod, loc = self.mod[module], _loc(full, mpath)
        ip = js.import_path(module, rel, data[:1000].decode("utf-8", "replace"))
        key = f"static/src/{rel}"
        mod["js_files"][key] = {**js.file_info(data), "import_path": ip, "loc": loc}
        self.js_paths[(module, key)] = full
        if ip is None:   # `@odoo-module ignore` : pas un module, rien de structurel
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
        """Rattache aux modèles les templates de rapports et ceux qui en héritent (chaîne inherit_id)."""
        roots = defaultdict(set)   # xml_id du template principal -> modèles
        for rname, model in reports:
            if rname in self.qweb:
                roots[rname].add(model)
            else:
                print(f"[warn] rapport {rname} ({model}) : template introuvable", file=sys.stderr)
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
        """Extraction odoo-module-extract/1 d'un module, ou None si le module est inconnu."""
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
    """Chemin relatif (dans un dossier de sortie) de l'extraction d'un module."""
    return os.path.join("modules", f"{module}.json")


def main(argv=None):
    p = argparse.ArgumentParser(description="Extrait la définition de modèles Odoo")
    p.add_argument("paths", nargs="+", help="chemins d'addons (community, enterprise, custom...)")
    p.add_argument("-m", "--model", action="append", default=[], help="modèle à extraire (répétable)")
    p.add_argument("--module", action="append", default=[],
                   help="module dont on extrait templates QWeb/OWL, JS et assets (répétable)")
    p.add_argument("-o", "--output", help="fichier .json (une seule cible) ou dossier "
                                          "(modules dans OUTPUT/modules/)")
    p.add_argument("--label", help="étiquette de version stockée dans la sortie (ex. 20.0)")
    p.add_argument("--list-models", nargs="+", metavar="MODULE",
                   help="affiche les modèles touchés par ces modules et quitte")
    a = p.parse_args(argv)

    idx = Index(a.paths, a.label)
    if a.list_models:
        for m in sorted(idx.models_of(a.list_models)):
            print(m)
        return
    if not (a.model or a.module) or not a.output:
        p.error("-m/--model ou --module, et -o/--output sont requis (sauf avec --list-models)")

    single = len(a.model) + len(a.module) == 1 and a.output.endswith(".json")
    for model in a.model:
        ext = idx.extract(model)
        if not ext["modules"]:
            print(f"[warn] modèle {model} introuvable", file=sys.stderr)
        out = a.output if single else os.path.join(a.output, model_filename(model))
        write_json(ext, out)
        n = {s: sum(len(v[s]) for v in ext["modules"].values()) for s in SECTIONS}
        print(f"{model}: {len(ext['modules'])} modules, {n['fields']} champs, "
              f"{n['methods']} méthodes, {n['views']} vues -> {out}")
    for module in a.module:
        ext = idx.extract_module(module)
        if ext is None:
            print(f"[warn] module {module} introuvable", file=sys.stderr)
            continue
        out = a.output if single else os.path.join(a.output, module_filename(module))
        write_json(ext, out)
        print(f"{module}: {len(ext['qweb_templates'])} templates QWeb, {len(ext['owl_templates'])} templates OWL, "
              f"{len(ext['js_files'])} fichiers JS, {len(ext['js_exports'])} exports, "
              f"{len(ext['js_classes'])} classes -> {out}")


if __name__ == "__main__":
    main()
