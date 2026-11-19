import csv
import json
import os
import tempfile
import unittest

from omodi import compare, js, migration_stats
from tests.helpers import OLD, NEW, quiet

needs_ts = unittest.skipUnless(js.HAS_TREE_SITTER, "tree-sitter not installed")


def run(tmp, *extra):
    return quiet(migration_stats.main, ["--old", OLD, "--new", NEW, "--modules", "demo", "demo2",
                                        "--old-label", "A", "--new-label", "B", "-o", tmp, *extra])


def jload(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


class OrchestratorTest(unittest.TestCase):
    def test_outputs_and_level1_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = run(tmp)
            for v in ("old", "new", "diff"):
                for m in ("demo", "demo2"):
                    self.assertTrue(os.path.isfile(os.path.join(tmp, v, "modules", f"{m}.json")), (v, m))
                self.assertTrue(os.path.isfile(os.path.join(tmp, v, "sale.order.json")))
            stats = jload(tmp, "stats.json")
            demo = stats["by_module"]["demo"]
            # attached QWeb (sale.order) + unattached (module)
            self.assertEqual((demo["templates_removed"], demo["templates_added"], demo["templates_modified"]),
                             (1, 1, 2))
            self.assertEqual((demo["template_nodes_removed"], demo["template_nodes_added"]), (1, 2))
            self.assertEqual((demo["owl_templates_removed"], demo["owl_templates_added"],
                              demo["owl_templates_modified"]), (1, 1, 1))
            self.assertEqual((demo["js_files_removed"], demo["js_files_added"], demo["js_files_modified"]),
                             (1, 2, 4))
            self.assertEqual((demo["assets_entries_removed"], demo["assets_entries_added"]), (1, 1))
            # lignes exactes (difflib) : voir diff(1) des fixtures
            self.assertEqual((demo["js_lines_added"], demo["js_lines_removed"]), (3 + 7, 2 + 8))
            self.assertIn("QWeb", out)
            self.assertIn("JS", out)

    @needs_ts
    def test_structural_stats_and_cross_module_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp)
            stats = jload(tmp, "stats.json")
            d, d2 = stats["by_module"]["demo"], stats["by_module"]["demo2"]
            self.assertEqual((d["js_exports_removed"], d["js_exports_added"], d["js_exports_moved_out"],
                              d["js_exports_moved_in"]), (2, 1, 2, 1))
            self.assertEqual(d2["js_exports_moved_in"], 1)
            self.assertNotIn("js_exports_added", d2)
            self.assertEqual((d["js_classes_modified"], d["js_methods_added"]), (1, 1))
            self.assertEqual((d["js_registries_removed"], d["js_registries_added"], d["js_registries_modified"]),
                             (1, 1, 1))
            self.assertEqual(d["js_patches_modified"], 1)
            diff = jload(tmp, "diff", "modules", "demo.json")
            self.assertIn("movable", [m["name"] for m in diff["js_exports"]["moved"]])

    @needs_ts
    def test_move_to_unlisted_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            quiet(migration_stats.main, ["--old", OLD, "--new", NEW, "--modules", "demo", "-o", tmp])
            d = jload(tmp, "stats.json")["by_module"]["demo"]
            self.assertEqual(d["js_exports_moved_out"], 2)    # helper (intra) + movable (to unlisted demo2)
            self.assertEqual(d["js_exports_removed"], 2)

    def test_stats_frontend_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp)
            with open(os.path.join(tmp, "stats_frontend.csv"), encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["module"] + compare.FRONTEND_KEYS)
        self.assertEqual([r[0] for r in rows[1:]], ["demo", "demo2", "TOTAL"])
        demo = dict(zip(rows[0], rows[1]))
        self.assertEqual(demo["js_files_added"], "2")

    def test_existing_csv_columns_are_a_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp)
            with open(os.path.join(tmp, "stats_by_module.csv"), encoding="utf-8", newline="") as fh:
                header = next(csv.reader(fh))
        self.assertEqual(header[:24], ["module"] + compare.STAT_KEYS[:23])
        self.assertEqual(header[1:], compare.STAT_KEYS)

    def test_no_output_prints_summary_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                _, out, err = quiet(migration_stats.main, ["--old", OLD, "--new", NEW, "--modules", "demo"])
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(cwd)
        self.assertIn("QWeb", out)
        self.assertIn("Indexing", err)

    def test_format_csv_only_and_json_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, "-f", "csv")
            self.assertEqual(sorted(os.listdir(tmp)), ["stats_by_model.csv", "stats_by_module.csv",
                                                       "stats_detail.csv", "stats_frontend.csv"])
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp, "-f", "json")
            self.assertEqual(sorted(os.listdir(tmp)), ["diff", "new", "old", "stats.json"])

    def test_default_output_dir_when_no_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                quiet(migration_stats.main, ["--old", OLD, "--new", NEW, "--modules", "demo", "-o"])
                self.assertTrue(os.path.isfile(os.path.join(tmp, "omodi-output", "stats.json")))
            finally:
                os.chdir(cwd)

    def test_module_missing_in_new_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            quiet(migration_stats.main, ["--old", OLD, "--new", NEW, "--modules", "demo", "ghost", "-o", tmp])
            stats = jload(tmp, "stats.json")
        self.assertEqual(stats["modules_unknown"], ["ghost"])


if __name__ == "__main__":
    unittest.main()
