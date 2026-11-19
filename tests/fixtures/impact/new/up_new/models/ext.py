from odoo import fields, models


class ThingExt(models.Model):
    _inherit = 'up.thing'

    f_moved = fields.Char()

    def m_moved(self):
        pass
