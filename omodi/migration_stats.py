#!/usr/bin/env python3
"""
omodi stats — enchaîne extraction + comparaison pour une liste de modules.

Entrée  : deux versions d'Odoo (chemins d'addons) + la liste des modules à vérifier.
Étapes  : 1. indexe les deux versions une seule fois (extract.Index : Python, XML, QWeb, OWL, JS, assets)
          2. trouve tous les modèles touchés par ces modules (dans l'une ou l'autre version)
          3. extrait chaque modèle dans les deux versions   -> OUT/old/<modèle>.json, OUT/new/<modèle>.json
             et chaque module listé (templates non rattachés, OWL, JS, assets)
                                                              -> OUT/old/modules/<module>.json, OUT/new/modules/...
          4. compare                                        -> OUT/diff/<modèle>.json, OUT/diff/modules/<module>.json
             (lignes JS ajoutées / retirées exactes via difflib ; exports déplacés entre modules détectés,
             y compris vers des modules non listés)
          5. agrège les stats                               -> OUT/stats.json + CSV
Les extractions de modèles contiennent tous les modules qui touchent le modèle (utile pour détecter
les déplacements) ; les stats sont restreintes aux modules demandés, sauf --all-modules (modèles uniquement).

Usage :
  omodi stats \\
      --old odoo-20/addons odoo-20/odoo/addons enterprise-20 \\
      --new odoo-21/addons odoo-21/odoo/addons enterprise-21 \\
      --modules sale stock account          # ou --modules-file modules.txt (un par ligne, # = commentaire)
      --old-label 20.0 --new-label 21.0 -o migration_20_21/
  omodi stats --old ... --new ... --modules ... -o ...     # même chose via le CLI installé

Sorties dans OUT/ :
  stats.json             totaux, par module, par modèle, par (module, modèle)
  stats_by_module.csv    une ligne par module (les colonnes frontend sont ajoutées à droite)
  stats_by_model.csv     une ligne par modèle
  stats_detail.csv       une ligne par (module, modèle)
  stats_frontend.csv     une ligne par module : templates QWeb / OWL, JS, assets (FRONTEND_KEYS)
  old/ new/ diff/        les JSON intermédiaires (modèles à la racine, modules dans modules/)
Résumé console : lignes « Templates » et « JS » en plus des lignes existantes.
Stdlib uniquement, Python >= 3.9 ; tree-sitter + tree-sitter-javascript optionnels (pip install omodi[js])
pour les stats JS structurelles (exports, classes, registres, patchs). Paquet omodi.
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
    """line_counter pour compare_modules : lignes ajoutées / retirées exactes (difflib), fichiers relus
    seulement quand leur sha1 diffère."""
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
    """Exports / classes ajoutés dans les modules non listés : destinations possibles des déplacements."""
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


def print_table(title, rows, top):
    cols = [("ch-", "fields_removed"), ("ch+", "fields_added"), ("ch~", "fields_modified"),
            ("dét+", "field_attrs_added"), ("me-", "methods_removed"), ("me+", "methods_added"),
            ("me~", "methods_modified"), ("vu-", "views_removed"), ("vu+", "views_added"),
            ("vu~", "views_modified")]
    rows = [r for r in rows if r[0] != "TOTAL"]
    main = [k for _, k in cols if k != "field_attrs_added"]
    rows = sorted(rows, key=lambda kv: -sum(kv[1].get(k, 0) for k in main))[:top]
    if not rows:
        return
    print(f"\n{title}")
    print(f"{'':<32}" + "".join(f"{h:>6}" for h, _ in cols))
    for name, c in rows:
        print(f"{str(name)[:31]:<32}" + "".join(f"{c.get(k, 0):>6}" for _, k in cols))


def main(argv=None):
    p = argparse.ArgumentParser(description="Stats de migration Odoo pour une liste de modules")
    p.add_argument("--old", nargs="+", required=True, help="chemins d'addons de l'ancienne version")
    p.add_argument("--new", nargs="+", required=True, help="chemins d'addons de la nouvelle version")
    p.add_argument("--modules", nargs="+", help="modules à vérifier")
    p.add_argument("--modules-file", help="fichier listant les modules (un par ligne ou séparés par des virgules)")
    p.add_argument("--old-label", default="old")
    p.add_argument("--new-label", default="new")
    p.add_argument("-o", "--output", required=True, help="dossier de sortie")
    p.add_argument("--all-modules", action="store_true",
                   help="compter aussi les modules non listés qui touchent les mêmes modèles")
    p.add_argument("--no-moves", action="store_true")
    p.add_argument("--top", type=int, default=20, help="lignes affichées dans le résumé")
    a = p.parse_args(argv)

    modules = read_modules(a)
    if not modules:
        p.error("donne au moins un module (--modules ou --modules-file)")

    t0 = time.time()
    print(f"Indexation {a.old_label}...", file=sys.stderr)
    old_idx = ex.Index(a.old, a.old_label)
    print(f"Indexation {a.new_label}...", file=sys.stderr)
    new_idx = ex.Index(a.new, a.new_label)

    gone = [m for m in modules if m in old_idx.modules and m not in new_idx.modules]
    new_only = [m for m in modules if m not in old_idx.modules and m in new_idx.modules]
    unknown = [m for m in modules if m not in old_idx.modules and m not in new_idx.modules]
    for m in unknown:
        print(f"[warn] module {m} introuvable dans les deux versions", file=sys.stderr)

    models = sorted(old_idx.models_of(modules) | new_idx.models_of(modules))
    print(f"{len(models)} modèles touchés par {len(modules)} modules", file=sys.stderr)

    wanted = set(modules)
    by_module = defaultdict(Counter)
    by_model = defaultdict(Counter)
    detail = {}
    total = Counter()

    # niveau module : templates non rattachés, OWL, JS, assets (extraction + diff par module listé)
    present = [m for m in modules if m in old_idx.modules or m in new_idx.modules]
    module_diffs = {}
    counter = js_line_counter(old_idx, new_idx)
    for module in present:
        old_m = old_idx.extract_module(module) or {"module": module, "label": a.old_label}
        new_m = new_idx.extract_module(module) or {"module": module, "label": a.new_label}
        if module in old_idx.modules:
            ex.write_json(old_m, os.path.join(a.output, "old", ex.module_filename(module)))
        if module in new_idx.modules:
            ex.write_json(new_m, os.path.join(a.output, "new", ex.module_filename(module)))
        module_diffs[module] = cmp.compare_modules(old_m, new_m, detect_moves=not a.no_moves,
                                                   line_counter=counter)
    if not a.no_moves:
        cmp.resolve_cross_module_moves(module_diffs, unlisted_added(old_idx, new_idx, set(present)))
    for module, mdiff in module_diffs.items():
        cmp.write_json(mdiff, os.path.join(a.output, "diff", ex.module_filename(module)))
        c = Counter(mdiff["stats"].get(module, {}))
        if c:   # comme pour les modèles : pas de ligne vide dans by_module
            by_module[module].update(c)
            total.update(c)

    for model in models:
        fname = ex.model_filename(model)
        old_ext, new_ext = old_idx.extract(model), new_idx.extract(model)
        ex.write_json(old_ext, os.path.join(a.output, "old", fname))
        ex.write_json(new_ext, os.path.join(a.output, "new", fname))
        diff = cmp.compare_models(old_ext, new_ext, detect_moves=not a.no_moves)
        cmp.write_json(diff, os.path.join(a.output, "diff", fname))
        for module, c in diff["stats"].items():
            if not a.all_modules and module not in wanted:
                continue
            c = Counter(c)
            by_module[module].update(c)
            by_model[model].update(c)
            detail[(module, model)] = c
            total.update(c)

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
    ex.write_json(stats, os.path.join(a.output, "stats.json"))
    write_csv(os.path.join(a.output, "stats_by_module.csv"), ["module"],
              sorted(by_module.items()) + [("TOTAL", total)])
    write_csv(os.path.join(a.output, "stats_by_model.csv"), ["model"],
              sorted(by_model.items()) + [("TOTAL", total)])
    write_csv(os.path.join(a.output, "stats_detail.csv"), ["module", "model"], sorted(detail.items()))
    frontend_total = Counter()
    with open(os.path.join(a.output, "stats_frontend.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["module"] + cmp.FRONTEND_KEYS)
        for module in present:
            c = by_module.get(module, Counter())
            frontend_total.update({k: c[k] for k in cmp.FRONTEND_KEYS})
            w.writerow([module] + [c.get(k, 0) for k in cmp.FRONTEND_KEYS])
        w.writerow(["TOTAL"] + [frontend_total.get(k, 0) for k in cmp.FRONTEND_KEYS])

    # résumé console
    g = lambda k: total.get(k, 0)
    print(f"\n{a.old_label} -> {a.new_label} | {len(modules)} modules, {len(models)} modèles "
          f"({time.time() - t0:.1f}s)")
    if gone:
        print(f"Modules disparus : {', '.join(gone)}")
    print(f"Champs     : -{g('fields_removed')} +{g('fields_added')} ~{g('fields_modified')} "
          f"(détails +{g('field_attrs_added')} -{g('field_attrs_removed')} ~{g('field_attrs_changed')}, "
          f"type changé {g('field_type_changed')}) déplacés {g('fields_moved_out')}")
    print(f"Méthodes   : -{g('methods_removed')} +{g('methods_added')} ~{g('methods_modified')} "
          f"déplacées {g('methods_moved_out')}")
    print(f"Vues       : -{g('views_removed')} +{g('views_added')} ~{g('views_modified')} "
          f"(nœuds -{g('view_nodes_removed')} +{g('view_nodes_added')})")
    print(f"Contraintes: -{g('constraints_removed')} +{g('constraints_added')} ~{g('constraints_modified')}"
          f" | attributs de classe ~{g('attributes_modified')}")
    print(f"Templates  : QWeb -{g('templates_removed')} +{g('templates_added')} ~{g('templates_modified')} "
          f"(nœuds -{g('template_nodes_removed')} +{g('template_nodes_added')}) "
          f"| OWL -{g('owl_templates_removed')} +{g('owl_templates_added')} ~{g('owl_templates_modified')}")
    print(f"JS         : fichiers -{g('js_files_removed')} +{g('js_files_added')} ~{g('js_files_modified')} "
          f"(lignes -{g('js_lines_removed')} +{g('js_lines_added')}) "
          f"| exports -{g('js_exports_removed')} +{g('js_exports_added')} "
          f"déplacés {g('js_exports_moved_out')} "
          f"| classes -{g('js_classes_removed')} +{g('js_classes_added')} ~{g('js_classes_modified')} "
          f"(méthodes -{g('js_methods_removed')} +{g('js_methods_added')}) "
          f"| registres -{g('js_registries_removed')} +{g('js_registries_added')} ~{g('js_registries_modified')} "
          f"| patchs -{g('js_patches_removed')} +{g('js_patches_added')} ~{g('js_patches_modified')} "
          f"| assets -{g('assets_entries_removed')} +{g('assets_entries_added')}")
    print_table("Par module :", list(by_module.items()), a.top)
    print_table("Modèles les plus touchés :", list(by_model.items()), a.top)
    print(f"\nRésultats -> {a.output}/ (stats.json, stats_by_module.csv, stats_by_model.csv, "
          f"stats_detail.csv, stats_frontend.csv)")


if __name__ == "__main__":
    main()
