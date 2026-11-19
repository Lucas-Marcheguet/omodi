from odoo import fields, models


class Thing(models.Model):
    _inherit = 'up.thing'

    f_removed = fields.Char(string='x')
    f_moved = fields.Char()
    f_type = fields.Char(string='t')
    f_attr = fields.Char()
    f_clash = fields.Char()
    f_custom_only = fields.Char()

    def m_removed(self): return super().m_removed()

    def m_sig(self, a): return super().m_sig(a)

    def m_moved(self): return super().m_moved()

    def helper_group(self):
        return self.env.ref('up.group_gone')


class Gone(models.Model):
    _inherit = 'up.old_model'

    y = fields.Char()


class Core(models.Model):
    _inherit = 'core.base'

    f_moved_in = fields.Char()


class OwnModel(models.Model):
    _name = 'cust.own'

    z = fields.Char()
