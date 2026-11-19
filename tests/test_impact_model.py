import unittest

from omodi import impact
from omodi.extract import Index
from tests.helpers import IMP_OLD, IMP_NEW, IMP_CUSTOM, quiet, tmp_addons


def pick(findings, rule, name=None):
    out = [f for f in findings if f["rule"] == rule and (name is None or f["upstream"].get("name") == name)]
    return out


class ClosureTest(unittest.TestCase):
    def test_closure_transitive_and_cycle_safe(self):
        files = {f"{m}/__manifest__.py": "{'depends': %r}" % d
                 for m, d in {"a": ["b"], "b": ["c", "a"], "c": []}.items()}
        with tmp_addons(files) as p:
            idx, _, _ = quiet(Index, [p], scan=False)
            members, unresolved = impact.closure(idx, ["a"])
        self.assertEqual(sorted(members), ["a", "b", "c"])
        self.assertEqual(unresolved, [])

    def test_unresolved_depends(self):
        idx, _, _ = quiet(Index, [IMP_CUSTOM, IMP_OLD], scan=False)
        members, unresolved = impact.closure(idx, ["cust"])
        self.assertEqual(sorted(members), ["core_mod", "cust", "gone_mod", "up"])
        self.assertEqual(unresolved, [{"module": "unknown_mod", "required_by": "cust"}])

    def test_finding_shape(self):
        f = impact.finding("review", "field", "field-modified", "msg", "cust", "cust/a.py:3", name="x",
                           upstream={"name": "y"})
        self.assertEqual(f, {"level": "review", "category": "field", "rule": "field-modified", "message": "msg",
                             "custom": {"module": "cust", "loc": "cust/a.py:3", "name": "x"},
                             "upstream": {"name": "y"}, "confidence": "certain"})


class ModelRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx, _, _ = quiet(impact.build_context, [IMP_OLD], [IMP_NEW], [IMP_CUSTOM], ["cust"])
        cls.dep = impact.dependency_findings(cls.ctx, "cust")
        cls.model = impact.model_findings(cls.ctx, "cust")

    def test_upstream_scope(self):
        self.assertEqual(self.ctx.upstream_old["cust"], {"up", "gone_mod", "core_mod"})
        self.assertEqual(self.ctx.closure_new["cust"], {"up", "core_mod"})

    def test_missing_dependency_in_new_is_breaking(self):
        f = pick(self.dep, "depends-removed", "gone_mod")
        self.assertEqual([x["level"] for x in f], ["breaking"])
        f = pick(self.dep, "depends-unresolved", "unknown_mod")
        self.assertEqual([x["level"] for x in f], ["info"])

    def test_field_removed_breaking(self):
        f = pick(self.model, "field-removed", "f_removed")
        self.assertEqual([(x["level"], x["category"]) for x in f], [("breaking", "field")])
        self.assertTrue(f[0]["custom"]["loc"].startswith("cust/models/thing.py:"))

    def test_field_moved_outside_new_closure_breaking_with_target_module(self):
        f = pick(self.model, "field-moved-out-of-closure", "f_moved")
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["level"], f[0]["upstream"]["to_module"]), ("breaking", "up_new"))

    def test_field_moved_inside_new_closure_review(self):
        f = pick(self.model, "field-moved", "f_moved_in")
        self.assertEqual([(x["level"], x["upstream"]["to_module"]) for x in f], [("review", "core_mod")])

    def test_field_type_changed_review(self):
        f = pick(self.model, "field-modified", "f_type")
        self.assertEqual([x["level"] for x in f], ["review"])
        self.assertIn("type", f[0]["message"])
        self.assertEqual(len(pick(self.model, "field-modified", "f_attr")), 1)

    def test_method_removed_breaking(self):
        self.assertEqual([x["level"] for x in pick(self.model, "method-removed", "m_removed")], ["breaking"])

    def test_method_signature_changed_review(self):
        f = pick(self.model, "method-modified", "m_sig")
        self.assertEqual([x["level"] for x in f], ["review"])
        self.assertIn("(self, a, b)", f[0]["message"])

    def test_method_moved_outside_closure_breaking(self):
        f = pick(self.model, "method-moved-out-of-closure", "m_moved")
        self.assertEqual([x["level"] for x in f], ["breaking"])

    def test_clash_with_new_upstream_field_review(self):
        f = pick(self.model, "field-clash", "f_clash")
        self.assertEqual([x["level"] for x in f], ["review"])

    def test_custom_only_field_and_model_ignored(self):
        names = {x["custom"]["name"] for x in self.model}
        self.assertNotIn("f_custom_only", names)
        self.assertNotIn("z", names)
        self.assertFalse([x for x in self.model if x["upstream"].get("name") == "cust.own"])

    def test_inherited_model_removed_breaking(self):
        f = pick(self.model, "model-removed", "up.old_model")
        self.assertEqual([(x["level"], x["category"]) for x in f], [("breaking", "model")])
        self.assertFalse(pick(self.model, "field-removed", "x"))   # no noise on the model content

    def test_model_removed_is_heuristic_when_dependencies_unresolved(self):
        # `unknown_mod` cannot be found: the model could be defined where we cannot see
        f = pick(self.model, "model-removed", "up.old_model")
        self.assertEqual(f[0]["confidence"], "heuristic")
        self.assertIn("incomplete", f[0]["message"])

    def test_view_parent_modified_review(self):
        f = pick(self.model, "view-parent-modified", "up.thing_form")
        self.assertEqual([x["level"] for x in f], ["review"])

    def test_view_field_removed_breaking(self):
        f = pick(self.model, "view-field-removed", "f_removed")
        self.assertEqual([x["level"] for x in f], ["breaking"])

    def test_view_xpath_target_removed_breaking_heuristic(self):
        f = pick(self.model, "view-xpath-target-removed", "f_removed")
        self.assertEqual([(x["level"], x["confidence"]) for x in f], [("breaking", "heuristic")])
        self.assertFalse(pick(self.model, "view-xpath-target-removed", "grp"))

    def test_template_parent_removed_breaking(self):
        f = pick(self.model, "template-parent-removed", "up.tpl_gone")
        self.assertEqual([x["level"] for x in f], ["breaking"])

    def test_template_parent_modified_review_and_xpath_token(self):
        self.assertEqual(len(pick(self.model, "template-parent-modified", "up.tpl_base")), 1)
        self.assertEqual(len(pick(self.model, "template-parent-modified", "up.tpl_mod")), 1)
        f = pick(self.model, "template-xpath-target-removed", "removed_cls")
        self.assertEqual([(x["level"], x["confidence"]) for x in f], [("breaking", "heuristic")])
        self.assertFalse(pick(self.model, "template-xpath-target-removed", "a"))

    def test_template_t_call_removed_breaking(self):
        f = pick(self.model, "template-call-removed", "up.tpl_gone")
        self.assertEqual([x["level"] for x in f], ["breaking"])


if __name__ == "__main__":
    unittest.main()
