from odoo import fields, models


class CoreExt(models.Model):
    _inherit = 'core.base'

    f_moved_in = fields.Char()
