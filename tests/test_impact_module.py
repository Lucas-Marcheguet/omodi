import unittest
from unittest import mock

from omodi import impact, js
from tests.helpers import IMP_OLD, IMP_NEW, IMP_CUSTOM, quiet

needs_ts = unittest.skipUnless(js.HAS_TREE_SITTER, "tree-sitter not installed")


def pick(findings, rule, name=None):
    return [f for f in findings if f["rule"] == rule and (name is None or f["upstream"].get("name") == name)]


def levels(findings):
    return [f["level"] for f in findings]


class ModuleRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx, _, _ = quiet(impact.build_context, [IMP_OLD], [IMP_NEW], [IMP_CUSTOM], ["cust"])
        cls.mod = impact.module_findings(cls.ctx, "cust")
        cls.xml = impact.xmlid_findings(cls.ctx, "cust")
        cls.every = impact.all_findings(cls.ctx, "cust")

    # --- OWL
    def test_owl_inherit_removed_breaking(self):
        self.assertEqual(levels(pick(self.mod, "owl-inherit-removed", "up.TplGone")), ["breaking"])

    def test_owl_inherit_modified_review(self):
        self.assertEqual(levels(pick(self.mod, "owl-inherit-modified", "up.Tpl")), ["review"])

    def test_owl_call_removed_breaking(self):
        self.assertEqual(levels(pick(self.mod, "owl-call-removed", "up.TplGone")), ["breaking"])

    # --- JS
    @needs_ts
    def test_js_import_removed_breaking(self):
        f = pick(self.mod, "js-import-removed", "Gone")
        self.assertEqual(levels(f), ["breaking"])
        self.assertEqual(f[0]["custom"]["loc"], "cust/static/src/js/main.js:2")

    @needs_ts
    def test_js_import_moved_breaking_with_new_path(self):
        f = pick(self.mod, "js-import-moved", "Moved")
        self.assertEqual(levels(f), ["breaking"])
        self.assertEqual((f[0]["upstream"]["to_path"], f[0]["upstream"]["to_module"]),
                         ("@up_new/js/moved", "up_new"))

    @needs_ts
    def test_js_import_kept_no_finding(self):
        self.assertFalse([f for f in self.mod if f["upstream"].get("name") == "Kept"])

    @needs_ts
    def test_class_extends_modified_class_review(self):
        self.assertEqual(levels(pick(self.mod, "js-class-modified", "Base")), ["review"])

    @needs_ts
    def test_class_override_of_removed_method_breaking(self):
        self.assertEqual(levels(pick(self.mod, "js-method-removed", "removedMethod")), ["breaking"])
        self.assertEqual(levels(pick(self.mod, "js-method-changed", "changedMethod")), ["review"])
        self.assertFalse(pick(self.mod, "js-method-removed", "setup"))

    @needs_ts
    def test_patch_removed_method_breaking(self):
        self.assertEqual(levels(pick(self.mod, "js-patch-method-removed", "removedMethod")), ["breaking"])
        self.assertEqual(levels(pick(self.mod, "js-patch-method-changed", "changedMethod")), ["review"])

    @needs_ts
    def test_registry_key_removed_review(self):
        self.assertEqual(levels(pick(self.mod, "registry-key-removed", "main::entry_gone")), ["review"])

    @needs_ts
    def test_registry_clash_review(self):
        self.assertEqual(levels(pick(self.mod, "registry-clash", "main::entry_new")), ["review"])

    # --- assets
    def test_asset_removed_file_breaking(self):
        f = pick(self.mod, "asset-file-removed", "up/static/src/js/gone_file.js")
        self.assertEqual(levels(f), ["breaking"])
        self.assertFalse(pick(self.mod, "asset-file-removed", "up/static/src/js/lib.js"))

    def test_asset_bundle_removed_breaking(self):
        f = pick(self.mod, "asset-bundle-removed", "up.bundle_gone")
        self.assertEqual([(x["level"], x["confidence"]) for x in f], [("breaking", "heuristic")])

    # --- XML ids / env.ref
    def test_xml_ref_removed_breaking(self):
        f = pick(self.xml, "xmlid-removed", "up.group_gone")
        files = sorted(x["custom"]["loc"].split(":")[0] for x in f)
        self.assertEqual(files, ["cust/models/thing.py", "cust/views/v.xml"])   # xml (3 refs, 1 constat) + env.ref
        self.assertEqual(set(levels(f)), {"breaking"})

    def test_xml_ref_moved_breaking_with_target(self):
        f = pick(self.xml, "xmlid-moved", "up.act_moved")
        self.assertEqual(levels(f), ["breaking"])
        self.assertEqual(f[0]["upstream"]["to"], "up_new.act_moved")

    def test_xml_ref_unchanged_and_unknown_ignored(self):
        names = {f["upstream"].get("name") for f in self.xml}
        self.assertNotIn("up.group_ok", names)
        self.assertNotIn("up.thing_form", names)
        self.assertNotIn("other.unknown", names)

    def test_env_ref_removed_breaking_has_line(self):
        f = [x for x in pick(self.xml, "xmlid-removed", "up.group_gone") if x["custom"]["loc"].endswith(".py")
             or ".py:" in x["custom"]["loc"]]
        self.assertEqual(len(f), 1)
        self.assertRegex(f[0]["custom"]["loc"], r"^cust/models/thing\.py:\d+$")

    def test_no_duplicate_between_xmlid_and_template_findings(self):
        self.assertFalse([f for f in self.every if f["rule"].startswith("xmlid")
                          and f["upstream"].get("name") == "up.tpl_gone"])
        self.assertTrue(pick(self.every, "template-parent-removed", "up.tpl_gone"))

    # --- mentions
    def test_mention_heuristic_review(self):
        f = [x for x in pick(self.every, "mention-removed-name", "f_removed")
             if x["custom"]["loc"].startswith("cust/utils.py")]
        self.assertEqual([(x["level"], x["confidence"]) for x in f], [("review", "heuristic")])
        self.assertEqual(f[0]["custom"]["loc"], "cust/utils.py:2")

    def test_mention_not_duplicated_on_covered_line(self):
        for name in ("f_removed", "m_removed"):
            files = [x["custom"]["loc"].split(":")[0] for x in pick(self.every, "mention-removed-name", name)]
            self.assertNotIn("cust/models/thing.py", files)
            self.assertNotIn("cust/views/v.xml", files)

    @needs_ts
    def test_js_export_names_only_searched_in_js_files(self):
        # `Gone` est aussi le nom d'une classe Python du module : not a mention of the removed JS export
        files = [x["custom"]["loc"] for x in pick(self.every, "mention-removed-name", "Gone")]
        self.assertFalse([f for f in files if not f.split(":")[0].endswith(".js")])

    def test_patch_wording(self):
        self.assertTrue(all(f["message"].startswith("the patch") for f in self.every
                            if f["rule"].startswith("js-patch-method")))

    def test_short_names_never_mentioned(self):
        self.assertFalse([f for f in self.every if f["rule"] == "mention-removed-name"
                          and len(f["upstream"]["name"]) < 4])


class NoTreeSitterTest(unittest.TestCase):
    def test_no_tree_sitter_skips_js_rules_with_warning(self):
        with mock.patch.object(js, "HAS_TREE_SITTER", False), mock.patch.object(js, "_WARNED", False):
            ctx, _, err = quiet(impact.build_context, [IMP_OLD], [IMP_NEW], [IMP_CUSTOM], ["cust"])
            every = impact.all_findings(ctx, "cust")
        self.assertEqual(len([l for l in err.splitlines() if "tree-sitter" in l]), 1)
        self.assertFalse([f for f in every if f["category"].startswith("js") or f["rule"].startswith("registry")])
        self.assertTrue(pick(every, "field-removed", "f_removed"))     # the other rules still run
        self.assertTrue(pick(every, "asset-file-removed"))


if __name__ == "__main__":
    unittest.main()
