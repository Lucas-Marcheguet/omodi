from odoo import fields, models


class Thing(models.Model):
    _name = 'up.thing'

    f_removed = fields.Char()
    f_moved = fields.Char()
    f_type = fields.Char()
    f_attr = fields.Char(required=True)

    def m_removed(self):
        pass

    def m_sig(self, a):
        pass

    def m_moved(self):
        pass


class OldModel(models.Model):
    _name = 'up.old_model'

    x = fields.Char()
