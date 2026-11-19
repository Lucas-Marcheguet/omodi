from odoo import fields, models


class Thing(models.Model):
    _name = 'up.thing'

    f_type = fields.Integer()
    f_attr = fields.Char(required=False)
    f_clash = fields.Char()

    def m_sig(self, a, b):
        pass
