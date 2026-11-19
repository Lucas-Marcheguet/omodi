from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    demo_x = fields.Char()
