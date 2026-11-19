import unittest

from omodi.extract import Index, MODULE_FORMAT, MODULE_SECTIONS
from tests.helpers import OLD, quiet, tmp_addons


def extract(path, module="demo"):
    idx, _, _ = quiet(Index, [path])
    return idx.extract_module(module)


class ModuleExtractTest(unittest.TestCase):
    def test_module_extract_has_all_sections(self):
        ext = extract(OLD)
        self.assertEqual(ext["format"], MODULE_FORMAT)
        self.assertEqual(ext["module"], "demo")
        self.assertEqual(MODULE_SECTIONS, ("qweb_templates", "owl_templates", "js_files", "js_exports",
                                           "js_classes", "js_registries", "js_patches", "assets"))
        for s in MODULE_SECTIONS:
            self.assertIn(s, ext)

    def test_unknown_module_returns_none(self):
        self.assertIsNone(extract(OLD, "nope"))

    def test_qweb_not_attached_goes_to_module(self):
        q = extract(OLD)["qweb_templates"]
        self.assertIn("demo.portal_thing", q)
        self.assertEqual(q["demo.portal_thing"]["calls"], ["portal.x"])
        self.assertNotIn("demo.report_demo", q)   # rattaché au modèle

    def test_owl_key_t_name(self):
        o = extract(OLD)["owl_templates"]
        self.assertIn("demo.Widget", o)
        self.assertEqual(o["demo.Widget"]["loc"], "demo/static/src/xml/t.xml")
        self.assertTrue(o["demo.Widget"]["arch_hash"])

    def test_owl_inherit_without_name_key(self):
        o = extract(OLD)["owl_templates"]
        t = o["web.Other|static/src/xml/t.xml"]
        self.assertEqual((t["t_inherit"], t["t_inherit_mode"]), ("web.Other", "extension"))

    def test_static_tests_and_lib_excluded(self):
        self.assertNotIn("demo.TestOnly", extract(OLD)["owl_templates"])
        xml = '<templates><t t-name="m.X"><div/></t></templates>'
        files = {"m/__manifest__.py": "{}", "m/static/src/ok.xml": xml.replace("X", "Ok"),
                 "m/static/src/sub/tests/t.xml": xml.replace("X", "InSrcTests"),
                 "m/static/lib/l.xml": xml.replace("X", "Lib")}
        with tmp_addons(files) as p:
            self.assertEqual(list(extract(p, "m")["owl_templates"]), ["m.Ok"])

    def test_assets_literal_eval(self):
        a = extract(OLD)["assets"]
        self.assertEqual(a["web.assets_frontend"], ["demo/static/src/js/front.js"])
        self.assertEqual(a["web.assets_backend"],
                         ["demo/static/src/**/*", "remove demo/static/src/js/legacy.js"])

    def test_assets_non_literal_warns(self):
        with tmp_addons({"m/__manifest__.py": "{'assets': {'a': [FOO]}}"}) as p:
            idx, _, err = quiet(Index, [p])
            ext = idx.extract_module("m")
        self.assertEqual(ext["assets"], {})
        self.assertIn("assets", err)

    def test_module_without_static_or_assets(self):
        with tmp_addons({"m/__manifest__.py": "{}"}) as p:
            ext = extract(p, "m")
        for s in MODULE_SECTIONS:
            self.assertEqual(ext[s], {})

    def test_invalid_owl_xml_does_not_crash(self):
        with tmp_addons({"m/__manifest__.py": "{}", "m/static/src/bad.xml": "<templates><t"}) as p:
            _, _, err = quiet(Index, [p])
        self.assertIn("XML invalide", err)


if __name__ == "__main__":
    unittest.main()


class CliModuleOptionTest(unittest.TestCase):
    def test_cli_module_option_writes_json(self):
        import json
        import os
        import tempfile
        from omodi import extract, js
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = quiet(extract.main, [OLD, "--module", "demo", "--module", "demo2", "-o", tmp])
            with open(os.path.join(tmp, "modules", "demo.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertTrue(os.path.exists(os.path.join(tmp, "modules", "demo2.json")))
        self.assertEqual(data["format"], "odoo-module-extract/1")
        self.assertIn("demo.Widget", data["owl_templates"])
        if js.HAS_TREE_SITTER:
            self.assertIn("@demo/js/a::Foo", data["js_exports"])
        self.assertIn("demo:", out)

    def test_single_module_to_json_file(self):
        import os
        import tempfile
        from omodi import extract
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "x.json")
            quiet(extract.main, [OLD, "--module", "demo", "-o", target])
            self.assertTrue(os.path.isfile(target))

    def test_model_and_module_together(self):
        import os
        import tempfile
        from omodi import extract
        with tempfile.TemporaryDirectory() as tmp:
            quiet(extract.main, [OLD, "-m", "sale.order", "--module", "demo", "-o", tmp])
            self.assertTrue(os.path.isfile(os.path.join(tmp, "sale.order.json")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "modules", "demo.json")))

    def test_unknown_module_warns(self):
        import tempfile
        from omodi import extract
        with tempfile.TemporaryDirectory() as tmp:
            _, _, err = quiet(extract.main, [OLD, "--module", "nope", "-o", tmp])
        self.assertIn("nope", err)
