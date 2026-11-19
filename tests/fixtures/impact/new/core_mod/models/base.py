from odoo import fields, models


class Base(models.Model):
    _name = 'core.base'

    f_base = fields.Char()
    f_moved_in = fields.Char()
