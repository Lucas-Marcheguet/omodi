import unittest

from omodi.extract import Index
from tests.helpers import OLD, NEW, quiet, tmp_addons


def templates(path, model="sale.order", module="demo"):
    idx, _, _ = quiet(Index, [path])
    return idx.extract(model)["modules"][module]["templates"]


class QwebReportTest(unittest.TestCase):
    def test_report_template_attached_to_model(self):
        t = templates(OLD)
        self.assertIn("demo.report_demo", t)
        self.assertIn("demo.report_demo_ext", t)  # inherited via inherit_id

    def test_tag_report_supported(self):
        self.assertIn("demo.report_tag", templates(OLD))

    def test_non_report_template_not_in_model_extract(self):
        t = templates(OLD)
        self.assertNotIn("demo.portal_thing", t)
        self.assertNotIn("demo.portal_old", t)

    def test_template_data(self):
        t = templates(OLD)
        self.assertEqual(t["demo.report_demo"]["calls"], ["web.html_container"])
        ext = t["demo.report_demo_ext"]
        self.assertEqual(ext["inherit_id"], "demo.report_demo")
        self.assertEqual(ext["priority"], "20")
        self.assertIn("span[]", ext["nodes"])
        self.assertTrue(ext["arch_hash"])
        self.assertTrue(ext["loc"].startswith("demo/report/report.xml"))

    def test_inherit_cycle_terminates(self):
        xml = """<odoo>
  <record id="r" model="ir.actions.report"><field name="model">res.partner</field>
    <field name="report_name">m.a</field></record>
  <template id="a" inherit_id="b"><x/></template>
  <template id="b" inherit_id="a"><y/></template>
</odoo>"""
        with tmp_addons({"m/__manifest__.py": "{}", "m/r.xml": xml}) as p:
            t = templates(p, "res.partner", "m")
        self.assertIn("m.a", t)

    def test_report_with_missing_template_warns(self):
        xml = """<odoo><record id="r" model="ir.actions.report">
  <field name="model">res.partner</field><field name="report_name">m.nothing</field></record></odoo>"""
        with tmp_addons({"m/__manifest__.py": "{}", "m/r.xml": xml}) as p:
            idx, _, err = quiet(Index, [p])
        self.assertIn("m.nothing", err)
        self.assertEqual(idx.extract("res.partner")["modules"], {})


if __name__ == "__main__":
    unittest.main()
