"""Intentionally broken and corrected controls; never installed in Odoo."""
from odoo import api, fields, models


class NativeProbe(models.Model):
    _name = "jb.native.probe"
    _description = "Native inspection control"

    name = fields.Char()

    @api.depends("jb_native_missing_field")
    def broken_dependency(self):
        return self.name

    @api.depends("name")
    def valid_dependency(self):
        return self.name


def wrong_model(value: "odoo.model.res_partner") -> "odoo.model.res_company":
    return value


def correct_model(value: "odoo.model.res_partner") -> "odoo.model.res_partner":
    return value


@api.deprecated("Native inspection deprecated-call control")
def deprecated_api():
    return 1


def broken_deprecated_call():
    return deprecated_api()
