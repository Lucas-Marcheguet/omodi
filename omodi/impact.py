"""
omodi impact — ce à quoi faire attention pour monter de version un ou plusieurs modules custom.

Pour chaque module custom :
  1. fermeture de ses dépendances (`depends` des manifests de OLD) ; les modules custom sont traversés mais
     pas comparés, les autres forment l'« amont » ;
  2. diff de l'amont entre OLD et NEW (extract + compare : modèles, templates QWeb / OWL, JS, assets) ;
  3. croisement avec ce que le module surcharge, hérite, référence ou importe -> constats.
OLD n'est scanné que sur le module custom et sa fermeture ; NEW est scanné en entier (pour voir qu'un élément
a été déplacé vers un module hors de la fermeture : « ajouter la dépendance »).

Usage :
  omodi impact --old ODOO-20/addons ODOO-20/odoo/addons ENTERPRISE-20 \\
                --new ODOO-21/addons ODOO-21/odoo/addons ENTERPRISE-21 \\
                --custom MES_ADDONS --modules mon_module autre_module -o impact/
  (--modules-file modules.txt : un module par ligne ou séparés par des virgules, # = commentaire)
Un module custom est cherché d'abord dans --custom, puis dans --old ; une copie dans --new est ignorée.

Sorties : OUT/impact.json (format odoo-impact/1) et OUT/impact.md (rapport lisible, un titre par module).

Niveaux : breaking (cible supprimée / déplacée / renommée, dépendance absente de NEW),
          review (signature, arguments, arch ou champs statiques modifiés ; conflit de nom avec un nouvel
          élément amont ; mention textuelle d'un nom supprimé), info (le reste, ex. dépendance introuvable).
Règles (champ `rule`) : depends-*, model-removed, field-* / method-* (removed, moved, moved-out-of-closure,
modified, clash), view-* / template-* (parent-removed, parent-modified, field-removed, call-removed,
xpath-target-removed), owl-*, js-import-*, js-class-modified, js-method-*, js-patch-method-*, registry-*,
asset-*, xmlid-removed / xmlid-moved (ref, inherit_id, groups, %(x.y)d, env.ref), mention-removed-name.
Les constats `confidence: heuristic` reposent sur une heuristique (xpath, bundles, mentions textuelles).
Limites : voir LIMITS (reprises dans le rapport). Stdlib uniquement ; tree-sitter optionnel (règles JS).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter

from . import compare as cmp
from . import extract as ex
from . import js
from .migration_stats import read_modules

LEVELS = ("breaking", "review", "info")


class ImpactError(Exception):
    """Erreur d'usage (module introuvable...) : affichée sans traceback par la CLI."""


def finding(level, category, rule, message, module, loc, name=None, upstream=None, confidence="certain"):
    """Un constat : niveau, catégorie, règle, message, côté custom (module, loc, nom) et côté amont."""
    return {"level": level, "category": category, "rule": rule, "message": message,
            "custom": {"module": module, "loc": loc, "name": name},
            "upstream": upstream or {}, "confidence": confidence}


# --------------------------------------------------------------------------
# Dépendances
# --------------------------------------------------------------------------
def closure(index, roots):
    """Fermeture transitive de `depends` depuis `roots`.

    Retourne (membres, non_résolus) : membres = modules trouvés dans l'index (racines comprises) ;
    non_résolus = [{"module", "required_by"}] pour les noms absents de l'index.
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
    """État partagé par toutes les règles : deux index, périmètres amont, diffs par modèle / par module."""

    def __init__(self):
        self.old = self.new = None
        self.modules = []            # modules custom demandés
        self.custom_set = set()      # demandés + modules trouvés dans --custom
        self.closure_old = {}        # module demandé -> membres de la fermeture (OLD)
        self.unresolved = {}         # module demandé -> dépendances introuvables
        self.upstream_old = {}       # module demandé -> modules amont de sa fermeture (OLD)
        self.closure_new = {}        # module demandé -> fermeture des mêmes modules dans NEW
        self.upstream_union = set()
        self.module_diffs = {}       # module amont -> diff odoo-module-diff/1
        self._model_diffs = {}
        self._local_ids = None

    def model_diff(self, model):
        """{"diff", "old", "new"} pour un modèle : OLD restreint à l'amont, NEW complet (déplacements)."""
        if model not in self._model_diffs:
            old_ext = self.old.extract(model)
            old_ext["modules"] = {m: v for m, v in old_ext["modules"].items() if m in self.upstream_union}
            new_ext = self.new.extract(model)
            self._model_diffs[model] = {"diff": cmp.compare_models(old_ext, new_ext),
                                        "old": old_ext, "new": new_ext}
        return self._model_diffs[model]

    def local_template_ids(self):
        """{nom local: [xml ids]} des templates QWeb de NEW (pour suggérer un déplacement)."""
        if self._local_ids is None:
            self._local_ids = {}
            for xid in self.new.qweb:
                self._local_ids.setdefault(xid.split(".", 1)[-1], []).append(xid)
        return self._local_ids


def _new_module_pool(old, new):
    """Exports / classes des modules absents de OLD : destinations possibles d'un déplacement."""
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


def build_context(old_paths, new_paths, custom_paths, modules, old_label=None, new_label=None):
    """Indexe OLD (custom + fermeture amont seulement) et NEW (arbre entier), calcule les diffs amont."""
    ctx = Context()
    ctx.modules = list(modules)
    ctx.old = ex.Index(list(custom_paths) + list(old_paths), old_label, scan=False)
    unknown = [m for m in modules if m not in ctx.old.modules]
    if unknown:
        raise ImpactError(f"module(s) introuvable(s) dans --custom et --old : {', '.join(unknown)}")
    custom_dirs = [os.path.realpath(p) for p in custom_paths]
    ctx.custom_set = set(modules) | {m for m, p in ctx.old.modules.items() if _under(p, custom_dirs)}

    union = set()
    for m in modules:
        members, ctx.unresolved[m] = closure(ctx.old, [m])
        ctx.closure_old[m] = members
        ctx.upstream_old[m] = {x for x in members if x not in ctx.custom_set}
        union.update(members)
    ctx.upstream_union = {x for x in union if x not in ctx.custom_set}
    ctx.old.scan(only=union, focus=modules)

    ctx.new = ex.Index(list(new_paths), new_label, scan=False)
    ctx.new.scan()
    for m in modules:
        roots = [x for x in ctx.upstream_old[m] if x in ctx.new.modules]
        ctx.closure_new[m] = set(closure(ctx.new, roots)[0])

    for m in sorted(ctx.upstream_union):
        old_m = ctx.old.extract_module(m)
        new_m = ctx.new.extract_module(m) or {"module": m, "label": new_label}
        ctx.module_diffs[m] = cmp.compare_modules(old_m, new_m)
    cmp.resolve_cross_module_moves(ctx.module_diffs, _new_module_pool(ctx.old, ctx.new))
    return ctx


def dependency_findings(ctx, module):
    out = []
    for dep in sorted(ctx.upstream_old[module]):
        if dep not in ctx.new.modules:
            out.append(finding("breaking", "depends", "depends-removed",
                               f"la dépendance `{dep}` n'existe plus dans la nouvelle version "
                               f"(module supprimé ou renommé)", module,
                               os.path.relpath(os.path.join(ctx.old.modules[module], "__manifest__.py"),
                                               os.path.dirname(ctx.old.modules[module])),
                               name=dep, upstream={"name": dep}))
    for u in ctx.unresolved[module]:
        out.append(finding("info", "depends", "depends-unresolved",
                           f"la dépendance `{u['module']}` (requise par `{u['required_by']}`) est introuvable "
                           f"dans --custom, --old et --new : analyse incomplète", module, None,
                           name=u["module"], upstream={"name": u["module"]}))
    return out


# --------------------------------------------------------------------------
# Règles niveau modèle
# --------------------------------------------------------------------------
def _boundary(token):
    return re.compile(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])")


def _xpath_tokens(nodes):
    """Chaînes entre guillemets des expr xpath d'une liste de signatures de nœuds."""
    tokens = []
    for sig in nodes:
        if sig.startswith("xpath["):
            tokens.extend(re.findall(r"""['"]([^'"]+)['"]""", sig))
    return sorted(set(tokens))


def _removed_tokens(tokens, removed, added):
    """Tokens présents dans un nœud retiré de l'arch parente et dans aucun nœud ajouté."""
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
            parts.append("arguments modifiés : " + ", ".join(changes["attrs"]))
    else:
        if "signature" in changes:
            parts.append(f"signature ({changes['signature']['old']}) -> ({changes['signature']['new']})")
        if "decorators" in changes:
            parts.append(f"décorateurs {changes['decorators']['old']} -> {changes['decorators']['new']}")
    return "; ".join(parts) or "modifié"


def _removed_names(md, section, up):
    """Noms réellement supprimés (retirés d'un module amont et encore définis nulle part dans NEW)."""
    still = {n for v in md["new"]["modules"].values() for n in v[section]}
    return {e["name"] for e in md["diff"][section]["removed"] if e["module"] in up and e["name"] not in still}


def _item_findings(ctx, module, model, section, md, up, newc, out):
    cat = "field" if section == "fields" else "method"
    label, e = ("champ", "") if cat == "field" else ("méthode", "e")   # accord des participes
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
                               f"{label} `{name}` de `{model}` supprimé{e} en amont (module {removed[0]['module']}) : "
                               f"la surcharge n'a plus de cible", module, loc, name,
                               {"name": name, "model": model, "module": removed[0]["module"]}))
        elif moved:
            to = moved[0]["to"]
            inside = to in newc
            out.append(finding(
                "review" if inside else "breaking", cat, f"{cat}-moved" if inside else f"{cat}-moved-out-of-closure",
                f"{label} `{name}` de `{model}` déplacé{e} de `{moved[0]['from']}` vers `{to}`"
                + ("" if inside else " : ajouter cette dépendance au manifest"), module, loc, name,
                {"name": name, "model": model, "from_module": moved[0]["from"], "to_module": to}))
        elif modified:
            out.append(finding("review", cat, f"{cat}-modified",
                               f"{label} `{name}` de `{model}` modifié{e} en amont : "
                               f"{_summary(section, modified[0]['changes'])}", module, loc, name,
                               {"name": name, "model": model, "module": modified[0]["module"]}))
        elif added and name not in old_defs:
            out.append(finding("review", cat, f"{cat}-clash",
                               f"{label} `{name}` de `{model}` est maintenant aussi défini{e} en amont "
                               f"(module {added[0]['module']}) : conflit possible", module, loc, name,
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
                                   f"la vue `{xid}` utilise le champ `{m.group(1)}` de `{model}`, supprimé en amont",
                                   module, loc, xid, {"name": m.group(1), "model": model}))
        if not parent:
            continue
        gone = [e for e in vdiff["removed"] if e["name"] == parent and e["module"] in up]
        changed = [e for e in vdiff["modified"] if e["name"] == parent and e["module"] in up]
        if gone:
            out.append(finding("breaking", "view", "view-parent-removed",
                               f"la vue `{xid}` hérite de `{parent}`, supprimée en amont", module, loc, xid,
                               {"name": parent, "model": model}))
        elif changed:
            arch = changed[0]["changes"].get("arch", {})
            rem, add = arch.get("nodes_removed", []), arch.get("nodes_added", [])
            out.append(finding("review", "view", "view-parent-modified",
                               f"la vue `{xid}` hérite de `{parent}`, modifiée en amont "
                               f"({len(rem)} nœud(s) retiré(s), {len(add)} ajouté(s)) : vérifier les xpath",
                               module, loc, xid, {"name": parent, "model": model,
                                                  "nodes_removed": rem[:20], "nodes_added": add[:20]}))
            for token in _removed_tokens(_xpath_tokens(info["nodes"]), rem, add):
                out.append(finding("breaking", "view", "view-xpath-target-removed",
                                   f"un xpath de `{xid}` cible `{token}`, nœud retiré de `{parent}` en amont",
                                   module, loc, xid, {"name": token, "view": parent, "model": model},
                                   confidence="heuristic"))


def _template_status(ctx, module, xid):
    """None | ("removed", {}) | ("modified", changes) pour un template amont référencé par le module."""
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
            hint = f" (existe sous : {', '.join(moved[:3])})" if moved else ""
            out.append(finding("breaking", "template", "template-parent-removed",
                               f"le template `{xid}` hérite de `{parent}`, supprimé en amont{hint}", module, loc,
                               xid, {"name": parent, "moved_to": moved[:3]}))
        elif st:
            arch = st[1].get("arch", {})
            rem, add = arch.get("nodes_removed", []), arch.get("nodes_added", [])
            out.append(finding("review", "template", "template-parent-modified",
                               f"le template `{xid}` hérite de `{parent}`, modifié en amont "
                               f"({len(rem)} nœud(s) retiré(s), {len(add)} ajouté(s)) : vérifier les xpath",
                               module, loc, xid, {"name": parent, "nodes_removed": rem[:20],
                                                  "nodes_added": add[:20]}))
            for token in _removed_tokens(_xpath_tokens(info["nodes"]), rem, add):
                out.append(finding("breaking", "template", "template-xpath-target-removed",
                                   f"un xpath de `{xid}` cible `{token}`, nœud retiré de `{parent}` en amont",
                                   module, loc, xid, {"name": token, "template": parent}, confidence="heuristic"))
        for called in info.get("calls", []):
            cs = _template_status(ctx, module, called)
            if cs and cs[0] == "removed":
                out.append(finding("breaking", "template", "template-call-removed",
                                   f"le template `{xid}` appelle `{called}` (t-call), supprimé en amont",
                                   module, loc, xid, {"name": called}))


def model_findings(ctx, module):
    """Constats sur les modèles touchés par le module : champs, méthodes, vues, templates QWeb."""
    out = []
    up, newc = ctx.upstream_old[module], ctx.closure_new[module]
    for model in sorted(ctx.old.module_models.get(module, ())):
        if module not in ctx.old.data.get(model, {}):
            continue
        md = ctx.model_diff(model)
        if not any(m in up for m in md["old"]["modules"]):
            continue   # modèle propre au module custom (ou fourni hors de sa fermeture)
        if not md["new"]["modules"]:
            attr = ctx.old.data[model][module]["attributes"].get("_inherit") or {}
            partial = bool(ctx.unresolved[module])   # dépendances introuvables : le modèle y est peut-être défini
            out.append(finding("breaking", "model", "model-removed",
                               f"le modèle `{model}` n'existe plus dans la nouvelle version"
                               + (" (analyse incomplète : dépendances introuvables)" if partial else ""),
                               module, attr.get("loc"), model, {"name": model},
                               confidence="heuristic" if partial else "certain"))
            continue
        for section in ("fields", "methods"):
            _item_findings(ctx, module, model, section, md, up, newc, out)
        _view_findings(ctx, module, model, md, up, out)
    _template_findings(ctx, module, out)
    return out


# --------------------------------------------------------------------------
# Règles niveau module : OWL, JS, assets
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
                               f"le template OWL `{key}` hérite de `{parent}` (t-inherit), supprimé en amont",
                               module, loc, key, {"name": parent}))
        elif st:
            out.append(finding("review", "owl", "owl-inherit-modified",
                               f"le template OWL `{key}` hérite de `{parent}` (t-inherit), modifié en amont : "
                               f"vérifier les xpath", module, loc, key, {"name": parent}))
        for sig in info.get("nodes", []):
            m = _OWL_CALL.match(sig)
            cs = _owl_status(ctx, module, m.group(1)) if m else None
            if cs and cs[0] == "removed":
                out.append(finding("breaking", "owl", "owl-call-removed",
                                   f"le template OWL `{key}` appelle `{m.group(1)}` (t-call), supprimé en amont",
                                   module, loc, key, {"name": m.group(1)}))


def _method_findings(ctx, module, cls_name, methods, changes, loc, rule_prefix, out, what):
    article, e = ("la", "e") if what == "classe" else ("le", "")
    """Méthodes d'une classe / d'un patch qui recoupent les méthodes retirées / modifiées en amont."""
    meth = changes.get("methods") or {}
    for name in sorted(methods):
        if name in meth.get("removed", []):
            out.append(finding("breaking", "js_class" if what == "classe" else "js_patch",
                               f"{rule_prefix}-removed",
                               f"{article} {what} `{cls_name}` surcharge `{name}`, supprimé{e} de la classe amont", module,
                               loc, cls_name, {"name": name, "class": cls_name}))
        elif name in meth.get("changed", {}):
            ch = meth["changed"][name]
            out.append(finding("review", "js_class" if what == "classe" else "js_patch",
                               f"{rule_prefix}-changed",
                               f"{article} {what} `{cls_name}` surcharge `{name}`, dont les paramètres changent en amont : "
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
            hint = "" if to["module"] in newc else " : ajouter cette dépendance au manifest"
            out.append(finding("breaking", "js_import", "js-import-moved",
                               f"`{name}` importé depuis `{src}` est maintenant exporté par `{to['path']}`{hint}",
                               module, imp["loc"], name, {"name": name, "from_path": src, "to_path": to["path"],
                                                           "to_module": to["module"]}))
        elif (src, name) in removed:
            out.append(finding("breaking", "js_import", "js-import-removed",
                               f"`{name}` importé depuis `{src}` n'est plus exporté en amont", module, imp["loc"],
                               name, {"name": name, "path": src, "module": removed[(src, name)]}))
        elif (src, name) in cls_mod:
            meth = cls_mod[(src, name)].get("methods") or {}
            out.append(finding("review", "js_import", "js-class-modified",
                               f"la classe `{name}` importée depuis `{src}` a changé en amont "
                               f"(méthodes retirées : {', '.join(meth.get('removed', [])) or '-'}; "
                               f"modifiées : {', '.join(meth.get('changed', {})) or '-'})", module, imp["loc"],
                               name, {"name": name, "path": src}))

    for key, data in sorted(ctx.old.mod[module]["js_classes"].items()):
        ext = data.get("extends")
        imp = by_file.get(data["loc"].rsplit(":", 1)[0], {}).get(ext) if ext and _PLAIN.match(ext) else None
        if imp and imp in cls_mod:
            _method_findings(ctx, module, key.split("::", 1)[1], data.get("methods", {}), cls_mod[imp],
                             data["loc"], "js-method", out, "classe")
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
                               f"l'entrée de registre `{key}` surchargée ici n'est plus enregistrée par les "
                               f"dépendances (supprimée ou déplacée)", module, data["loc"], key, {"name": key}))
        elif key not in old_keys and key in new_keys:
            out.append(finding("review", "registry", "registry-clash",
                               f"la clé de registre `{key}` est maintenant aussi enregistrée en amont : doublon "
                               f"possible (DuplicatedKeyError sans `force`)", module, data["loc"], key,
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
                               f"le bundle `{name}` n'est plus déclaré par les dépendances : vérifier qu'il existe "
                               f"toujours", module, loc, name, {"name": name}, confidence="heuristic"))

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
                                       f"le bundle `{b}` référence `{arg}`, fichier supprimé en amont", module,
                                       loc, b, {"name": arg}))


def module_findings(ctx, module):
    """Constats niveau module : OWL, imports / classes / patchs / registres JS, assets."""
    out = []
    _owl_findings(ctx, module, out)
    _js_findings(ctx, module, out)
    _asset_findings(ctx, module, out)
    return out


# --------------------------------------------------------------------------
# XML ids / env.ref et mentions textuelles
# --------------------------------------------------------------------------
def xmlid_findings(ctx, module):
    """Références du module (XML, env.ref) à des XML ids amont qui n'existent plus dans NEW."""
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
                               f"référence `{xid}` ({r['kind']}) : l'identifiant est maintenant `{moved[0]}`",
                               module, r["loc"], xid, {"name": xid, "kind": r["kind"], "to": moved[0]}))
        else:
            out.append(finding("breaking", "xml_ref", "xmlid-removed",
                               f"référence `{xid}` ({r['kind']}) : l'identifiant n'existe plus en amont", module,
                               r["loc"], xid, {"name": xid, "kind": r["kind"]}))
    return out


_MENTION_SKIP_DIRS = {"i18n", "i18n_extra", "tests", "test", "node_modules", "__pycache__", "migrations",
                      "upgrades", "lib"}


_PY_XML_JS = (".py", ".xml", ".js")


def _removed_upstream_names(ctx, module):
    """{nom: (nature, extensions)} des champs / méthodes / exports JS supprimés en amont (>= 4 caractères).

    Les noms d'exports JS ne sont cherchés que dans les .js (un nom de classe Python identique n'est pas une mention).
    """
    up, names = ctx.upstream_old[module], {}
    for model in sorted(ctx.old.module_models.get(module, ())):
        if module not in ctx.old.data.get(model, {}):
            continue
        md = ctx.model_diff(model)
        if not any(m in up for m in md["old"]["modules"]) or not md["new"]["modules"]:
            continue
        for section, kind in (("fields", "champ"), ("methods", "méthode")):
            for n in _removed_names(md, section, up):
                names.setdefault(n, (f"{kind} de {model}", _PY_XML_JS))
    for _, d in _up_diffs(ctx, module):
        for e in d["js_exports"]["removed"]:
            names.setdefault(e["name"], ("export JS", (".js",)))
    return {n: k for n, k in names.items() if len(n) >= 4 and n != "default"}


def mention_findings(ctx, module, existing):
    """Heuristique : mentions textuelles (mot entier) de noms supprimés en amont dans les fichiers du module."""
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
                           f"mention de `{name}` ({names[name][0]}, supprimé en amont) aux lignes "
                           f"{', '.join(map(str, lines[:10]))}", module, f"{rel}:{lines[0]}", name,
                           {"name": name, "kind": names[name][0], "lines": lines[:10]}, confidence="heuristic"))
    return out


def all_findings(ctx, module):
    """Toutes les règles pour un module custom, sans doublon (xml ids et mentions déjà couverts écartés)."""
    base = dependency_findings(ctx, module) + model_findings(ctx, module) + module_findings(ctx, module)
    covered = {(f["custom"]["loc"].split(":")[0], f["upstream"].get("name")) for f in base if f["custom"]["loc"]}
    xml = [f for f in xmlid_findings(ctx, module)
           if (f["custom"]["loc"].split(":")[0], f["upstream"].get("name")) not in covered]
    base += xml
    return base + mention_findings(ctx, module, base)


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------
LEVEL_LABELS = {"breaking": "Cassant", "review": "À vérifier", "info": "Info"}
LIMITS = [
    "L'usage d'un champ, d'une méthode ou d'un export supprimé dans du code (getattr, noms construits, domaines) "
    "n'est détecté que par mention textuelle (heuristique).",
    "Les xpath ne sont pas évalués : on compare les chaînes citées aux nœuds retirés de l'arch parente (heuristique).",
    "Un élément déplacé vers un module qui existait déjà dans OLD hors de la fermeture de dépendances apparaît "
    "comme supprimé.",
    "Les fichiers statiques autres que JS et templates OWL (scss, images) ne sont pas suivis.",
    "Les bundles d'assets déclarés seulement dans des templates peuvent être signalés à tort.",
    "Les identifiants générés (model_*, field_*) ne sont pas suivis.",
]


def _sort_key(f):
    return (LEVELS.index(f["level"]), f["category"], f["custom"]["loc"] or "", f["rule"],
            str(f["upstream"].get("name")))


def analyze(old_paths, new_paths, custom_paths, modules, old_label=None, new_label=None):
    """Rapport odoo-impact/1 pour les modules custom demandés."""
    ctx = build_context(old_paths, new_paths, custom_paths, modules, old_label, new_label)
    report_modules = {}
    for m in modules:
        findings = sorted(all_findings(ctx, m), key=_sort_key)
        counts = Counter(f["level"] for f in findings)
        report_modules[m] = {
            "closure": {"old": sorted(ctx.closure_old[m]), "upstream": sorted(ctx.upstream_old[m]),
                        "missing_in_new": sorted(x for x in ctx.upstream_old[m] if x not in ctx.new.modules),
                        "unresolved": ctx.unresolved[m]},
            "counts": {**{lv: counts[lv] for lv in LEVELS}, "total": len(findings)},
            "findings": findings}
    stats = Counter()
    for d in ctx.module_diffs.values():
        stats.update(d["stats_total"])
    for md in ctx._model_diffs.values():
        stats.update(md["diff"]["stats_total"])
    limits = list(LIMITS)
    if not js.HAS_TREE_SITTER:
        limits.append("tree-sitter n'est pas installé : les règles JS (imports, classes, patchs, registres) "
                      "n'ont pas été appliquées.")
    return {"format": "odoo-impact/1", "old_label": old_label, "new_label": new_label,
            "paths": {"old": list(old_paths), "new": list(new_paths), "custom": list(custom_paths)},
            "modules": report_modules,
            "upstream_stats": {k: stats[k] for k in cmp.STAT_KEYS if stats[k]},
            "limits": limits}


def _cell(text):
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report):
    """Rapport lisible : un titre par module, trois niveaux, tableau Catégorie / Custom / Amont / Constat."""
    lines = [f"# Impact de la montée de version {report.get('old_label') or 'old'} -> "
             f"{report.get('new_label') or 'new'}", ""]
    for module, mod in report["modules"].items():
        c, cl = mod["counts"], mod["closure"]
        lines += [f"## Module `{module}`", "",
                  f"Cassant : **{c['breaking']}** | À vérifier : **{c['review']}** | Info : **{c['info']}**", "",
                  f"Dépendances amont analysées ({len(cl['upstream'])}) : "
                  + (", ".join(f"`{m}`" for m in cl["upstream"]) or "aucune")]
        if cl["missing_in_new"]:
            lines.append("Absentes de la nouvelle version : " + ", ".join(f"`{m}`" for m in cl["missing_in_new"]))
        lines.append("")
        for level in LEVELS:
            items = [f for f in mod["findings"] if f["level"] == level]
            lines += [f"### {LEVEL_LABELS[level]} ({len(items)})", ""]
            if not items:
                lines += ["Rien à signaler.", ""]
                continue
            lines += ["| Catégorie | Custom | Amont | Constat |", "|---|---|---|---|"]
            for f in items:
                up = f["upstream"]
                target = up.get("name", "") + (f" -> {up['to_module']}" if up.get("to_module") else "")
                note = " (heuristique)" if f["confidence"] == "heuristic" else ""
                lines.append(f"| {_cell(f['category'])} | `{_cell(f['custom']['loc'] or f['custom']['module'])}` | "
                             f"{_cell(target)} | {_cell(f['message'])}{note} |")
            lines.append("")
    lines += ["## Limites", ""] + [f"- {x}" for x in report["limits"]] + [""]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="omodi impact",
                                description="Ce à quoi faire attention pour monter de version des modules custom")
    p.add_argument("--old", nargs="+", required=True, help="chemins d'addons de l'ancienne version")
    p.add_argument("--new", nargs="+", required=True, help="chemins d'addons de la nouvelle version")
    p.add_argument("--custom", nargs="+", default=[], help="chemins d'addons custom (cherchés avant --old)")
    p.add_argument("--modules", nargs="+", help="modules custom à analyser")
    p.add_argument("--modules-file", help="fichier listant les modules (un par ligne ou séparés par des virgules)")
    p.add_argument("--old-label", default="old")
    p.add_argument("--new-label", default="new")
    p.add_argument("-o", "--output", required=True, help="dossier de sortie (impact.json, impact.md)")
    a = p.parse_args(argv)
    modules = read_modules(a)
    if not modules:
        p.error("donne au moins un module (--modules ou --modules-file)")
    t0 = time.time()
    try:
        report = analyze(a.old, a.new, a.custom, modules, a.old_label, a.new_label)
    except ImpactError as e:
        p.error(str(e))
    ex.write_json(report, os.path.join(a.output, "impact.json"))
    with open(os.path.join(a.output, "impact.md"), "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    print(f"{a.old_label} -> {a.new_label} | {len(modules)} module(s) custom ({time.time() - t0:.1f}s)")
    for m, mod in report["modules"].items():
        c = mod["counts"]
        print(f"{m}: cassant {c['breaking']} | à vérifier {c['review']} | info {c['info']} "
              f"({len(mod['closure']['upstream'])} dépendances amont)")
    print(f"Résultats -> {a.output}/ (impact.json, impact.md)")


if __name__ == "__main__":
    main()
