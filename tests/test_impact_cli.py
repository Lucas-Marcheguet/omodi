import csv
import json
import os
import re
import shutil
import tempfile
import unittest

from omodi import cli, impact, js
from tests.helpers import IMP_OLD, IMP_NEW, IMP_CUSTOM, quiet

needs_ts = unittest.skipUnless(js.HAS_TREE_SITTER, "tree-sitter not installed")
ARGS = ["--old", IMP_OLD, "--new", IMP_NEW, "--custom", IMP_CUSTOM, "--old-label", "A", "--new-label", "B"]


def run(tmp, *extra):
    return quiet(impact.main, [*ARGS, "--modules", "cust", "-o", tmp, *extra])


class ReportTest(unittest.TestCase):
    def test_end_to_end_report_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, md, _ = run(tmp)
            with open(os.path.join(tmp, "impact.json"), encoding="utf-8") as fh:
                rep = json.load(fh)
            self.assertFalse(os.path.exists(os.path.join(tmp, "impact.md")))
        self.assertEqual(rep["format"], "odoo-impact/1")
        self.assertEqual((rep["old_label"], rep["new_label"]), ("A", "B"))
        mod = rep["modules"]["cust"]
        self.assertEqual(mod["closure"]["upstream"], ["core_mod", "gone_mod", "up"])
        self.assertEqual(mod["closure"]["missing_in_new"], ["gone_mod"])
        self.assertEqual(mod["closure"]["unresolved"], [{"module": "unknown_mod", "required_by": "cust"}])
        c = mod["counts"]
        self.assertEqual(c["total"], c["breaking"] + c["review"] + c["info"])
        order = [impact.LEVELS.index(f["level"]) for f in mod["findings"]]
        self.assertEqual(order, sorted(order))
        self.assertIn("## Module `cust`", md)
        self.assertEqual(int(re.search(r"### Breaking \((\d+)\)", md).group(1)), c["breaking"])
        for title in ("### To review", "### Info", "## Limits"):
            self.assertIn(title, md)
        self.assertTrue(rep["limits"])
        self.assertIn("fields_removed", rep["upstream_stats"])

    @needs_ts
    def test_exact_counts_with_tree_sitter(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(tmp)
            with open(os.path.join(tmp, "impact.json"), encoding="utf-8") as fh:
                c = json.load(fh)["modules"]["cust"]["counts"]
        self.assertEqual(c, {"breaking": 22, "review": 15, "info": 1, "total": 38})

    def test_unknown_module_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                _, _, err = quiet(impact.main, [*ARGS, "--modules", "nope", "-o", tmp])
        self.assertEqual(cm.exception.code, 2)

    def test_unknown_module_message(self):
        import contextlib
        import io
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                impact.main([*ARGS, "--modules", "nope", "-o", tmp])
        self.assertIn("nope", err.getvalue())

    def test_modules_file_and_several_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            mf = os.path.join(tmp, "mods.txt")
            with open(mf, "w", encoding="utf-8") as fh:
                fh.write("# modules custom\nup, core_mod\n")
            quiet(impact.main, [*ARGS, "--modules", "cust", "--modules-file", mf, "-o", os.path.join(tmp, "o")])
            with open(os.path.join(tmp, "o", "impact.json"), encoding="utf-8") as fh:
                rep = json.load(fh)
        self.assertEqual(sorted(rep["modules"]), ["core_mod", "cust", "up"])

    def test_custom_in_old_tree_equals_custom_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "old")
            shutil.copytree(IMP_OLD, old)
            shutil.copytree(os.path.join(IMP_CUSTOM, "cust"), os.path.join(old, "cust"))
            quiet(impact.main, ["--old", old, "--new", IMP_NEW, "--modules", "cust", "-o", os.path.join(tmp, "a")])
            quiet(impact.main, ["--old", IMP_OLD, "--new", IMP_NEW, "--custom", IMP_CUSTOM, "--modules", "cust",
                                "-o", os.path.join(tmp, "b")])
            with open(os.path.join(tmp, "a", "impact.json"), encoding="utf-8") as fa, \
                    open(os.path.join(tmp, "b", "impact.json"), encoding="utf-8") as fb:
                a, b = json.load(fa), json.load(fb)
        self.assertEqual(a["modules"]["cust"]["findings"], b["modules"]["cust"]["findings"])
        self.assertEqual(a["modules"]["cust"]["counts"], b["modules"]["cust"]["counts"])

    def test_empty_custom_module_gives_valid_empty_report(self):
        from tests.helpers import tmp_addons
        with tmp_addons({"m/__manifest__.py": "{'depends': ['up']}"}) as custom, tempfile.TemporaryDirectory() as tmp:
            _, md, _ = quiet(impact.main, ["--old", IMP_OLD, "--new", IMP_NEW, "--custom", custom,
                                           "--modules", "m", "-o", tmp])
            with open(os.path.join(tmp, "impact.json"), encoding="utf-8") as fh:
                mod = json.load(fh)["modules"]["m"]
        self.assertEqual(mod["findings"], [])
        self.assertEqual(mod["counts"], {"breaking": 0, "review": 0, "info": 0, "total": 0})
        self.assertIn("Nothing to report.", md)

    def test_omodi_impact_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            quiet(cli.main, ["impact", *ARGS, "--modules", "cust", "-o", tmp])
            self.assertTrue(os.path.isfile(os.path.join(tmp, "impact.json")))

    def test_help_lists_impact(self):
        _, out, _ = quiet(cli.main, [])
        self.assertIn("impact", out)
        self.assertIn("-o [DIR]", out)

    def test_no_output_prints_markdown_only(self):
        _, out, _ = quiet(impact.main, [*ARGS, "--modules", "cust"])
        self.assertIn("## Module `cust`", out)
        self.assertFalse(os.path.exists("omodi-output"))

    def test_output_without_value_uses_default_dir_and_format_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                quiet(impact.main, [*ARGS, "--modules", "cust", "-f", "csv", "-o"])
                self.assertTrue(os.path.isfile(os.path.join(tmp, "omodi-output", "impact.csv")))
                self.assertFalse(os.path.exists(os.path.join(tmp, "omodi-output", "impact.json")))
                with open(os.path.join(tmp, "omodi-output", "impact.csv"), encoding="utf-8", newline="") as fh:
                    rows = list(csv.reader(fh))
            finally:
                os.chdir(cwd)
        self.assertEqual(rows[0], impact.CSV_COLUMNS)
        self.assertGreater(len(rows), 1)

    def test_format_without_output_is_an_error(self):
        with self.assertRaises(SystemExit) as cm:
            quiet(impact.main, [*ARGS, "--modules", "cust", "-f", "json"])
        self.assertEqual(cm.exception.code, 2)

    def test_progress_on_stderr_not_stdout(self):
        _, out, err = quiet(impact.main, [*ARGS, "--modules", "cust"])
        self.assertIn("Indexing", err)
        self.assertNotIn("Indexing", out)
        _, _, err = quiet(impact.main, [*ARGS, "--modules", "cust", "-q"])
        self.assertEqual(err, "")

    def test_markdown_escapes_pipes(self):
        rep = {"format": "odoo-impact/1", "old_label": "A", "new_label": "B", "limits": [],
               "upstream_stats": {}, "modules": {"m": {
                   "closure": {"old": [], "upstream": [], "missing_in_new": [], "unresolved": []},
                   "counts": {"breaking": 1, "review": 0, "info": 0, "total": 1},
                   "findings": [impact.finding("breaking", "field", "r", "a | b", "m", "m/x.py:1", "n", {"name": "u|v"})]}}}
        md = impact.render_markdown(rep)
        self.assertIn("a \\| b", md)
        self.assertIn("u\\|v", md)


if __name__ == "__main__":
    unittest.main()
