import copy
import json
import os
import tempfile
import unittest

from omodi import compare, extract, js
from tests.helpers import OLD, NEW, quiet

needs_ts = unittest.skipUnless(js.HAS_TREE_SITTER, "tree-sitter not installed")


def ext(path, module):
    idx, _, _ = quiet(extract.Index, [path], )
    return idx.extract_module(module)


def model_ext(path):
    idx, _, _ = quiet(extract.Index, [path])
    return idx.extract("sale.order")


def total(diff):
    return diff["stats_total"]


class StatKeysTest(unittest.TestCase):
    def test_new_keys_appended_in_order(self):
        keys = compare.STAT_KEYS
        self.assertEqual(keys[:23], [
            "fields_removed", "fields_added", "fields_modified", "fields_moved_out", "fields_moved_in",
            "field_attrs_added", "field_attrs_removed", "field_attrs_changed", "field_type_changed",
            "methods_removed", "methods_added", "methods_modified", "methods_moved_out", "methods_moved_in",
            "constraints_removed", "constraints_added", "constraints_modified",
            "attributes_modified",
            "views_removed", "views_added", "views_modified", "view_nodes_removed", "view_nodes_added"])
        self.assertEqual(keys[23:], [
            "templates_removed", "templates_added", "templates_modified",
            "template_nodes_removed", "template_nodes_added",
            "owl_templates_removed", "owl_templates_added", "owl_templates_modified",
            "js_files_removed", "js_files_added", "js_files_modified", "js_lines_removed", "js_lines_added",
            "js_exports_removed", "js_exports_added", "js_exports_moved_out", "js_exports_moved_in",
            "js_classes_removed", "js_classes_added", "js_classes_modified",
            "js_methods_removed", "js_methods_added",
            "js_registries_removed", "js_registries_added", "js_registries_modified",
            "js_patches_removed", "js_patches_added", "js_patches_modified",
            "assets_entries_removed", "assets_entries_added"])


class ModelTemplatesDiffTest(unittest.TestCase):
    def test_report_template_modified(self):
        d = compare.compare_models(model_ext(OLD), model_ext(NEW))
        self.assertEqual([m["name"] for m in d["templates"]["modified"]], ["demo.report_demo_ext"])
        self.assertEqual(d["templates"]["removed"], [])
        self.assertEqual(d["templates"]["added"], [])
        self.assertEqual(d["stats"]["demo"]["templates_modified"], 1)
        self.assertEqual(d["stats"]["demo"]["template_nodes_added"], 1)
        self.assertNotIn("template_nodes_removed", d["stats"]["demo"])


class ModuleDiffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old, cls.new = ext(OLD, "demo"), ext(NEW, "demo")

    def diff(self, **kw):
        return compare.compare_modules(self.old, self.new, **kw)

    def test_format_and_dispatch(self):
        d = compare.compare(self.old, self.new)
        self.assertEqual(d["format"], "odoo-module-diff/1")
        self.assertEqual(d["module"], "demo")

    def test_qweb_templates(self):
        t = total(self.diff())
        self.assertEqual((t["templates_removed"], t["templates_added"], t["templates_modified"]), (1, 1, 1))
        self.assertEqual((t["template_nodes_removed"], t["template_nodes_added"]), (1, 1))

    def test_owl_templates(self):
        d = self.diff()
        t = total(d)
        self.assertEqual((t["owl_templates_removed"], t["owl_templates_added"], t["owl_templates_modified"]),
                         (1, 1, 1))
        self.assertEqual([x["name"] for x in d["owl_templates"]["removed"]], ["demo.Gone"])
        self.assertEqual([x["name"] for x in d["owl_templates"]["modified"]], ["demo.Widget"])

    def test_assets_entries(self):
        d = self.diff()
        self.assertEqual(total(d)["assets_entries_removed"], 1)
        self.assertEqual(total(d)["assets_entries_added"], 1)
        mod = {m["bundle"]: m for m in d["assets"]["modified"]}
        self.assertEqual(mod["web.assets_backend"]["removed"], ["remove demo/static/src/js/legacy.js"])
        self.assertEqual(mod["web.assets_frontend"]["added"], ["demo/static/src/js/front2.js"])

    def test_js_files_level1(self):
        d = self.diff(line_counter=lambda module, key, o, n: (5, 3))
        t = total(d)
        self.assertEqual((t["js_files_removed"], t["js_files_added"], t["js_files_modified"]), (1, 2, 4))
        self.assertEqual(t["js_lines_added"], 3 + 4 * 5)     # front2 (1) + helper (2) + 4 modified
        self.assertEqual(t["js_lines_removed"], 2 + 4 * 3)   # legacy (2) + 4 modified
        self.assertEqual([x["name"] for x in d["js_files"]["removed"]], ["static/src/js/legacy.js"])

    def test_js_lines_default_is_line_count_delta(self):
        o = {"module": "m", "js_files": {"a.js": {"sha1": "1", "lines": 10}}}
        n = {"module": "m", "js_files": {"a.js": {"sha1": "2", "lines": 13}, "b.js": {"sha1": "3", "lines": 4}}}
        t = total(compare.compare_modules(o, n))
        self.assertEqual((t["js_lines_added"], t.get("js_lines_removed", 0)), (3 + 4, 0))

    @needs_ts
    def test_js_exports_intra_module_move(self):
        d = self.diff()
        t = total(d)
        self.assertEqual((t["js_exports_removed"], t["js_exports_added"]), (3, 1))   # K, LEGACY, movable / NEW
        self.assertEqual((t["js_exports_moved_out"], t["js_exports_moved_in"]), (1, 1))
        self.assertEqual(d["js_exports"]["moved"], [{
            "name": "helper", "from": {"module": "demo", "path": "@demo/js/a"},
            "to": {"module": "demo", "path": "@demo/js/helper"}}])
        self.assertEqual(sorted(x["name"] for x in d["js_exports"]["removed"]), ["K", "LEGACY", "movable"])
        self.assertEqual([x["name"] for x in d["js_exports"]["added"]], ["NEW"])

    @needs_ts
    def test_no_moves_option(self):
        t = total(self.diff(detect_moves=False))
        self.assertEqual((t["js_exports_removed"], t["js_exports_added"]), (4, 2))
        self.assertNotIn("js_exports_moved_out", t)

    @needs_ts
    def test_js_classes_and_methods(self):
        d = self.diff()
        t = total(d)
        self.assertEqual((t["js_classes_modified"], t.get("js_classes_removed", 0), t.get("js_classes_added", 0)),
                         (1, 0, 0))
        self.assertEqual((t["js_methods_added"], t.get("js_methods_removed", 0)), (1, 0))
        ch = d["js_classes"]["modified"][0]["changes"]
        self.assertEqual(ch["static"]["template"], {"old": '"demo.Widget"', "new": '"demo.Widget2"'})
        self.assertEqual(ch["methods"]["added"], ["baz"])
        self.assertEqual(ch["methods"]["changed"]["bar"], {"old": "(a, b)", "new": "(a)"})

    @needs_ts
    def test_js_registries(self):
        d = self.diff()
        t = total(d)
        self.assertEqual((t["js_registries_removed"], t["js_registries_added"], t["js_registries_modified"]),
                         (1, 1, 1))
        self.assertEqual([x["name"] for x in d["js_registries"]["removed"]], ["services::demo_service"])
        self.assertEqual(d["js_registries"]["modified"][0]["changes"]["value"], {"old": "Foo", "new": "Bar"})

    @needs_ts
    def test_js_patches(self):
        d = self.diff()
        self.assertEqual(total(d)["js_patches_modified"], 1)
        self.assertEqual(d["js_patches"]["modified"][0]["changes"]["methods"],
                         {"old": ["setup"], "new": ["more", "setup"]})

    def test_patch_extension_change_is_a_modification(self):
        o = {"module": "m", "js_patches": {"@m/x::A": {"methods": [], "properties": [], "extension": "a()"}}}
        n = {"module": "m", "js_patches": {"@m/x::A": {"methods": [], "properties": [], "extension": "b()"}}}
        self.assertEqual(total(compare.compare_modules(o, n))["js_patches_modified"], 1)

    def test_loc_change_is_not_a_modification(self):
        moved_loc = copy.deepcopy(self.old)

        def bump(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "loc" and isinstance(v, str):
                        o[k] = v + "9"
                    else:
                        bump(v)
        bump(moved_loc)
        self.assertEqual(compare.compare_modules(self.old, moved_loc)["stats_total"], {})

    def test_missing_side_is_empty(self):
        d = compare.compare(compare.EMPTY, self.new)
        self.assertEqual(d["module"], "demo")
        self.assertGreater(total(d)["js_files_added"], 0)


@needs_ts
class CrossModuleMoveTest(unittest.TestCase):
    def test_export_moved_to_other_module(self):
        diffs = {m: compare.compare_modules(ext(OLD, m), ext(NEW, m)) for m in ("demo", "demo2")}
        self.assertEqual(total(diffs["demo2"])["js_exports_added"], 1)
        compare.resolve_cross_module_moves(diffs)
        a, b = total(diffs["demo"]), total(diffs["demo2"])
        self.assertEqual((a["js_exports_removed"], a["js_exports_moved_out"], a["js_exports_moved_in"]), (2, 2, 1))
        self.assertNotIn("js_exports_added", b)
        self.assertEqual(b["js_exports_moved_in"], 1)
        self.assertIn({"name": "movable", "from": {"module": "demo", "path": "@demo/js/movable"},
                       "to": {"module": "demo2", "path": "@demo2/movable2"}}, diffs["demo"]["js_exports"]["moved"])
        self.assertIn("movable", [m["name"] for m in diffs["demo2"]["js_exports"]["moved"]])

    def test_extra_added_pool_for_unlisted_module(self):
        diffs = {"demo": compare.compare_modules(ext(OLD, "demo"), ext(NEW, "demo"))}
        compare.resolve_cross_module_moves(diffs, extra_added={"js_exports": [
            {"module": "demo2", "path": "@demo2/movable2", "name": "movable", "kind": "function"}]})
        a = total(diffs["demo"])
        self.assertEqual((a["js_exports_removed"], a["js_exports_moved_out"]), (2, 2))

    def test_default_and_star_never_moved(self):
        o = {"module": "a", "js_exports": {"@a/x::default": {"kind": "const"}, "@a/x::*": {"kind": "reexport"}}}
        n = {"module": "b", "js_exports": {"@b/y::default": {"kind": "const"}, "@b/y::*": {"kind": "reexport"}}}
        diffs = {"a": compare.compare_modules(o, {"module": "a"}), "b": compare.compare_modules({"module": "b"}, n)}
        compare.resolve_cross_module_moves(diffs)
        self.assertEqual(total(diffs["a"])["js_exports_removed"], 2)
        self.assertNotIn("js_exports_moved_out", total(diffs["a"]))


class CompareCliTest(unittest.TestCase):
    def test_incompatible_formats_exit_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
            quiet(extract.main, [OLD, "-m", "sale.order", "-o", a])
            quiet(extract.main, [OLD, "--module", "demo", "-o", b])
            with self.assertRaises(SystemExit) as cm:
                quiet(compare.main, [a, b, "-o", os.path.join(tmp, "d.json")])
        self.assertEqual(cm.exception.code, 2)

    def test_directories_with_modules_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            for label, path in (("old", OLD), ("new", NEW)):
                quiet(extract.main, [path, "--module", "demo", "-m", "sale.order", "-o", os.path.join(tmp, label)])
            _, out, _ = quiet(compare.main, [os.path.join(tmp, "old"), os.path.join(tmp, "new"),
                                             "-o", os.path.join(tmp, "diff")])
            with open(os.path.join(tmp, "diff", "modules", "demo.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["format"], "odoo-module-diff/1")
            with open(os.path.join(tmp, "diff", "sale.order.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["format"], "odoo-model-diff/1")
        self.assertIn("demo:", out)
        self.assertIn("sale.order:", out)


if __name__ == "__main__":
    unittest.main()
