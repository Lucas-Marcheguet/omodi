"""Les sorties des sections existantes doivent rester identiques (golden files)."""
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest

from omodi import compare as odoo_compare
from omodi import extract as odoo_extract
from omodi import migration_stats as odoo_migration_stats

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.join(HERE, "fixtures", "legacy")
GOLDEN = os.path.join(HERE, "fixtures", "golden")

# Clés ajoutées par les nouvelles fonctionnalités : ignorées pour la comparaison
IGNORED = {"paths", "templates"}


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in IGNORED}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


class RegressionTest(unittest.TestCase):
    def test_existing_outputs_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = os.path.join(tmp, "old.json"), os.path.join(tmp, "new.json")
            diff = os.path.join(tmp, "diff.json")
            _quiet(odoo_extract.main, [os.path.join(LEGACY, "old"), "-m", "sale.order", "-o", old,
                                       "--label", "old"])
            _quiet(odoo_extract.main, [os.path.join(LEGACY, "new"), "-m", "sale.order", "-o", new,
                                       "--label", "new"])
            _quiet(odoo_compare.main, [old, new, "-o", diff])
            for name, path in (("old", old), ("new", new), ("diff", diff)):
                self.assertEqual(_strip(_load(path)), _strip(_load(os.path.join(GOLDEN, name + ".json"))), name)

    def test_orchestrator_stats_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            _quiet(odoo_migration_stats.main, [
                "--old", os.path.join(LEGACY, "old"), "--new", os.path.join(LEGACY, "new"),
                "--modules", "sale_demo", "-o", tmp])
            self.assertEqual(_load(os.path.join(tmp, "stats.json")), _load(os.path.join(GOLDEN, "stats.json")))
            # Les nouvelles colonnes s'ajoutent à droite : les colonnes existantes restent un préfixe identique
            with open(os.path.join(tmp, "stats_by_module.csv"), encoding="utf-8", newline="") as a, \
                    open(os.path.join(GOLDEN, "stats_by_module.csv"), encoding="utf-8", newline="") as b:
                fresh, golden = list(csv.reader(a)), list(csv.reader(b))
            width = len(golden[0])
            self.assertGreaterEqual(len(fresh[0]), width)
            self.assertEqual([r[:width] for r in fresh], golden)


if __name__ == "__main__":
    unittest.main()
