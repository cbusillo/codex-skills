from odoo import api


@api.deprecated("Private execution witness")
def obsolete_witness():
    return False


WITNESS = obsolete_witness()
