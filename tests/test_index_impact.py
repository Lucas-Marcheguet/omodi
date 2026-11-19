import unittest

from omodi import extract, js
from omodi.extract import Index
from tests.helpers import IMP_OLD, IMP_CUSTOM, quiet, tmp_addons

needs_ts = unittest.skipUnless(js.HAS_TREE_SITTER, "tree-sitter not installed")


def full(**kw):
    idx, _, _ = quiet(Index, [IMP_CUSTOM, IMP_OLD], **kw)
    return idx


class DependsTest(unittest.TestCase):
    def test_depends_read_for_all_modules(self):
        idx = full(scan=False)
        self.assertEqual(idx.depends["cust"], ["up", "gone_mod", "unknown_mod"])
        self.assertEqual(idx.depends["up"], ["core_mod"])
        self.assertEqual(idx.depends["core_mod"], [])

    def test_non_literal_depends_warns_and_is_empty(self):
        with tmp_addons({"m/__manifest__.py": "{'depends': [FOO]}"}) as p:
            res, _, err = quiet(extract.read_depends, p + "/m/__manifest__.py")
        self.assertEqual(res, [])
        self.assertIn("depends", err)

    def test_missing_depends_is_empty(self):
        with tmp_addons({"m/__manifest__.py": "{'name': 'x'}"}) as p:
            self.assertEqual(extract.read_depends(p + "/m/__manifest__.py"), [])


class TwoPhaseScanTest(unittest.TestCase):
    def test_two_phase_scan_only_scans_selected(self):
        idx = full(scan=False)
        self.assertEqual(dict(idx.xml_ids), {})
        self.assertEqual(len(idx.data), 0)
        quiet(idx.scan, {"up", "core_mod"})
        self.assertIn("up", idx.data["up.thing"])
        self.assertNotIn("cust", idx.data["up.thing"])
        self.assertNotIn("cust.g1", idx.xml_ids)

    def test_default_scan_is_unchanged(self):
        idx = full()
        self.assertIn("cust", idx.data["up.thing"])

    def test_xml_ids_collected(self):
        ids = full().xml_ids
        self.assertEqual(ids["up.thing_form"], "up")
        self.assertEqual(ids["up.group_gone"], "up")
        self.assertEqual(ids["cust.g1"], "cust")
        self.assertEqual(ids["cust.menu_cust"], "cust")


class FocusRefsTest(unittest.TestCase):
    def test_xml_refs_collected_for_focus_only(self):
        self.assertEqual(dict(full().xml_refs), {})
        refs = {(r["xml_id"], r["kind"]) for r in full(focus={"cust"}).xml_refs["cust"]}
        self.assertEqual(refs, {
            ("up.thing_form", "inherit_id"), ("up.tpl_gone", "inherit_id"), ("up.tpl_base", "inherit_id"),
            ("up.tpl_mod", "inherit_id"), ("up.tpl_gone", "t-call"), ("up.group_gone", "eval-ref"),
            ("up.group_ok", "eval-ref"), ("up.act_moved", "action"), ("up.group_gone", "groups"),
            ("up.group_ok", "groups"), ("other.unknown", "eval-ref")})

    def test_unqualified_and_own_ids_ignored_and_percent_refs(self):
        xml = """<odoo><record id="a" model="x"><field name="p" ref="local"/><field name="q" ref="m.own"/>
<field name="r" ref="o.theirs"/></record><button type="action" name="%(o.act)d"/></odoo>"""
        with tmp_addons({"m/__manifest__.py": "{}", "m/v.xml": xml}) as p:
            idx, _, _ = quiet(Index, [p], focus={"m"})
        self.assertEqual({(r["xml_id"], r["kind"]) for r in idx.xml_refs["m"]},
                         {("o.theirs", "r"), ("o.act", "action")})

    def test_py_env_ref_collected(self):
        refs = full(focus={"cust"}).py_refs["cust"]
        self.assertEqual([(r["xml_id"], r["kind"]) for r in refs], [("up.group_gone", "env.ref")])
        self.assertTrue(refs[0]["loc"].startswith("cust/models/thing.py:"))

    @needs_ts
    def test_js_imports_for_focus_module(self):
        imps = full(focus={"cust"}).js_imports["cust"]
        got = {(i["imported"], i["source"]) for i in imps}
        self.assertEqual(got, {("Base", "@up/js/lib"), ("Gone", "@up/js/lib"), ("Moved", "@up/js/lib"),
                               ("Kept", "@up/js/lib"), ("registry", "@web/core/registry"),
                               ("patch", "@web/core/utils/patch")})
        self.assertTrue(all(i["file"] == "cust/static/src/js/main.js" for i in imps))
        self.assertEqual(dict(full().js_imports), {})


@needs_ts
class JsImportsTest(unittest.TestCase):
    def test_js_imports_named_default_namespace_side_effect(self):
        src = b'import A, { b as c, d } from "x";\nimport * as ns from "y";\nimport "z";\n'
        r = js.analyze(src, "@m/f", "m/static/src/f.js", with_imports=True)["imports"]
        got = [(i["imported"], i["local"], i["source"]) for i in r]
        self.assertEqual(got, [("default", "A", "x"), ("b", "c", "x"), ("d", "d", "x"),
                               ("*", "ns", "y"), (None, None, "z")])
        self.assertEqual(r[0]["loc"], "m/static/src/f.js:1")

    def test_analyze_without_imports_unchanged(self):
        r = js.analyze(b'import A from "x";', "@m/f", "m/f.js")
        self.assertEqual(set(r), {"exports", "classes", "registries", "patches"})


class ResolveSourceTest(unittest.TestCase):
    def test_relative_import_resolved(self):
        self.assertEqual(js.resolve_source("./a/b", "m", "js/main.js"), "@m/js/a/b")
        self.assertEqual(js.resolve_source("../x", "m", "js/sub/f.js"), "@m/js/x")
        self.assertEqual(js.resolve_source("./foo/index", "m", "js/main.js"), "@m/js/foo")
        self.assertEqual(js.resolve_source("@up/js/lib", "m", "js/main.js"), "@up/js/lib")


if __name__ == "__main__":
    unittest.main()
