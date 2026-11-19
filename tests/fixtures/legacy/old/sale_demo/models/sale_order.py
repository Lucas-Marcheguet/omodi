from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'
    _order = 'id desc'

    note2 = fields.Char(string='Note', required=True)
    gone_field = fields.Integer()

    def action_old(self, a):
        return a

    @api.depends('note2')
    def _compute_x(self):
        pass
