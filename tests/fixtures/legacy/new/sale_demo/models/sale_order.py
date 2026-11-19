from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'
    _order = 'date_order desc'

    note2 = fields.Char(string='Remark', required=False)
    new_field = fields.Integer()

    def action_old(self, a, b=None):
        return a

    def action_new(self):
        pass
