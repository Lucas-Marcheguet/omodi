import unittest
from unittest import mock

from omodi import js
from omodi.extract import Index
from tests.helpers import OLD, quiet, tmp_addons

needs_ts = unittest.skipUnless(js.HAS_TREE_SITTER, "tree-sitter non installé")
P = "@m/x"


def analyze(src, path=P):
    return js.analyze(src.encode(), path, "m/static/src/x.js")


class ImportPathTest(unittest.TestCase):
    def test_import_path_default(self):
        self.assertEqual(js.import_path("web", "core/utils/foo.js", "/** @odoo-module */"),
                         "@web/core/utils/foo")
        self.assertEqual(js.import_path("web", "core/utils/foo.js", ""), "@web/core/utils/foo")

    def test_import_path_index_stripped(self):
        self.assertEqual(js.import_path("web", "core/foo/index.js", ""), "@web/core/foo")

    def test_import_path_alias(self):
        hdr = "/** @odoo-module alias=web.Widget default=false */\nimport x"
        self.assertEqual(js.import_path("web", "a.js", hdr), "web.Widget")

    def test_import_path_ignore(self):
        self.assertIsNone(js.import_path("web", "a.js", "// @odoo-module ignore\n"))

    def test_file_info(self):
        info = js.file_info(b"a\nb\n")
        self.assertEqual(info["lines"], 2)
        self.assertEqual(len(info["sha1"]), 40)
        self.assertEqual(js.file_info(b"a\nb")["lines"], 2)
        self.assertEqual(js.file_info(b"")["lines"], 0)


@needs_ts
class AnalyzeTest(unittest.TestCase):
    def test_exports_all_forms(self):
        r = analyze("""
export class A {}
export function f() {}
export const c = 1, d = 2;
export let l = 3;
export default 5;
export { a as b, z };
export { r as q } from "./other";
export * from "./all";
const a = 1; class z {}
""")["exports"]
        kinds = {k.split("::", 1)[1]: v["kind"] for k, v in r.items()}
        self.assertEqual(kinds, {"A": "class", "f": "function", "c": "const", "d": "const", "l": "const",
                                 "default": "const", "b": "const", "z": "class", "q": "reexport",
                                 "*": "reexport"})
        self.assertTrue(all(k.startswith(P + "::") for k in r))
        self.assertEqual(r[P + "::A"]["loc"], "m/static/src/x.js:2")

    def test_export_default_class_and_function(self):
        self.assertEqual(analyze("export default class Foo {}")["exports"][P + "::default"]["kind"], "class")
        self.assertEqual(analyze("export default function () {}")["exports"][P + "::default"]["kind"],
                         "function")

    def test_class_extends_methods_static_fields(self):
        c = analyze("""
export class Foo extends Component {
    static template = "m.Foo";
    static props = { a: String,   b: Number };
    static components = { Bar };
    static other = 1;
    setup() {}
    bar(a, b = 2) {}
    get x() { return 1; }
    static make(z) {}
}
class Hidden {}
""")["classes"]
        foo = c[P + "::Foo"]
        self.assertEqual(foo["extends"], "Component")
        self.assertEqual(foo["static"], {"template": '"m.Foo"', "props": "{ a: String, b: Number }",
                                         "components": "{ Bar }"})
        self.assertEqual(foo["methods"], {"setup": "()", "bar": "(a, b = 2)", "get x": "()",
                                          "static make": "(z)"})
        self.assertIsNone(c[P + "::Hidden"]["extends"])

    def test_registry_chain_category_add(self):
        r = analyze("""
registry.category("actions").add("demo_action", Foo);
registry.category("web").category("sub").add("k", 1, { sequence: 5 });
registry.category("x").add(someVar, 1);
other.category("nope").add("k", 1);
""")["registries"]
        self.assertEqual({k: v["value"] for k, v in r.items()},
                         {"actions::demo_action": "Foo", "web/sub::k": "1", "x::someVar": "1"})
        self.assertEqual(r["actions::demo_action"]["loc"], "m/static/src/x.js:2")

    def test_registry_value_truncated_200(self):
        r = analyze('registry.category("a").add("k", "%s");' % ("A" * 300))["registries"]
        self.assertEqual(len(r["a::k"]["value"]), 200)

    def test_patch_members(self):
        p = analyze("patch(Foo.prototype, { setup() {}, extra: 1, async more() {} });")["patches"]
        self.assertEqual(p, {P + "::Foo.prototype": {"methods": ["more", "setup"], "properties": ["extra"],
                                                     "loc": "m/static/src/x.js:1"}})

    def test_patch_with_non_literal_extension(self):
        p = analyze("patch(Foo.prototype, saleMixin());")["patches"]
        self.assertEqual(p, {P + "::Foo.prototype": {"methods": [], "properties": [], "extension": "saleMixin()",
                                                     "loc": "m/static/src/x.js:1"}})

    def test_syntax_error_does_not_crash(self):
        r = analyze("export const ok = 1;\nexport class {{{ ( \n")
        self.assertEqual(set(r), {"exports", "classes", "registries", "patches"})
        self.assertIn(P + "::ok", r["exports"])

    def test_empty_file(self):
        self.assertEqual(analyze(""), {"exports": {}, "classes": {}, "registries": {}, "patches": {}})


class RobustnessTest(unittest.TestCase):
    def test_non_utf8_and_empty_js_files_are_indexed(self):
        import os
        with tmp_addons({"m/__manifest__.py": "{}", "m/static/src/ok.js": "export const ok = 1;\n"}) as p:
            base = os.path.join(p, "m", "static", "src")
            with open(os.path.join(base, "latin.js"), "wb") as fh:
                fh.write(b"// caf\xe9 \xff\nexport const latin = 1;\n")
            open(os.path.join(base, "empty.js"), "w").close()
            idx, _, _ = quiet(Index, [p])
            files = idx.extract_module("m")["js_files"]
        self.assertEqual(set(files), {"static/src/ok.js", "static/src/latin.js", "static/src/empty.js"})
        self.assertEqual(files["static/src/empty.js"]["lines"], 0)


class FallbackTest(unittest.TestCase):
    def test_no_tree_sitter_fallback(self):
        with mock.patch.object(js, "HAS_TREE_SITTER", False), mock.patch.object(js, "_WARNED", False):
            idx, _, err = quiet(Index, [OLD])
            ext = idx.extract_module("demo")
        self.assertIn("static/src/js/a.js", ext["js_files"])
        self.assertEqual(ext["js_files"]["static/src/js/a.js"]["lines"], 12)
        for s in ("js_exports", "js_classes", "js_registries", "js_patches"):
            self.assertEqual(ext[s], {})
        self.assertEqual(len([l for l in err.splitlines() if "tree-sitter" in l]), 1)


@needs_ts
class IndexJsTest(unittest.TestCase):
    def test_module_extract_js_sections(self):
        idx, _, _ = quiet(Index, [OLD])
        ext = idx.extract_module("demo")
        self.assertEqual(set(ext["js_files"]), {
            "static/src/js/a.js", "static/src/js/broken.js", "static/src/js/front.js",
            "static/src/js/legacy.js", "static/src/js/movable.js", "static/src/js/patch.js",
            "static/src/js/reg.js"})
        self.assertIn("@demo/js/a::Foo", ext["js_exports"])
        self.assertFalse([k for k in ext["js_exports"] if "ignored" in k.lower()])
        self.assertIn("@demo/js/a::Foo", ext["js_classes"])
        self.assertIn("actions::demo_action", ext["js_registries"])
        self.assertIn("@demo/js/patch::Foo.prototype", ext["js_patches"])


if __name__ == "__main__":
    unittest.main()
