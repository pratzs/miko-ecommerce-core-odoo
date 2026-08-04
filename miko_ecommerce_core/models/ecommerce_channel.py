# -*- coding: utf-8 -*-
"""A connected store.

Platform agnostic on purpose: this model knows a store exists, who it belongs to
and how imported orders should behave. It knows nothing about Shopify, and it
must stay that way, or the second platform becomes a rewrite rather than an
addition.
"""
from odoo import _, api, fields, models


class MikoEcommerceChannel(models.Model):
    _name = 'miko.ecommerce.channel'
    _description = 'Connected store'
    _order = 'name'

    name = fields.Char(required=True)
    platform = fields.Selection(
        selection=[('none', 'Not set')], default='none', required=True,
        help="Each connector adds its own platform here. If Shopify is not in this "
             "list, the Shopify connector is not installed yet: find it by "
             "searching the Odoo Apps store for Miko.")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda s: s.env.company)

    # --- how imported orders should behave -------------------------------
    # Defaults chosen so a first sync cannot do anything irreversible. A
    # connector that confirms and invoices on day one is a connector that
    # produces a mess nobody can unpick.
    auto_confirm_orders = fields.Boolean(
        string='Confirm orders automatically', default=False,
        help="Off by default. Import first, look at what arrived, then decide.")
    auto_create_invoice = fields.Boolean(
        string='Create invoices automatically', default=False)
    connection_state = fields.Selection(
        [('untested', 'Not tested yet'), ('ok', 'Connected'), ('error', 'Failed')],
        default='untested', readonly=True,
        help="Whether the last connection test succeeded. Every connector sets "
             "this, so the store list reads the same whatever platform it is.")
    connection_message = fields.Text(
        readonly=True,
        help="What the last connection test said, in full, including what to do "
             "about a failure.")
    create_missing_products = fields.Boolean(
        string='Create products found on orders', default=True,
        help="When an order arrives for something not in Odoo, create the product "
             "rather than failing the order. Switch this off if the Odoo catalogue "
             "is the authority and an unknown product means something is wrong.")
    unmapped_tax_policy = fields.Selection(
        [('block', 'Stop and ask'), ('ignore', 'Import without the tax')],
        default='block', required=True, string='Unmapped taxes',
        help="What to do when the store sends a tax with no Odoo equivalent.\n\n"
             "Stop and ask is the default because the alternative is invoices "
             "quietly short by the tax amount, which nobody notices until a "
             "return is filed.")
    import_from_date = fields.Datetime(
        string='Import orders placed after',
        help="A hard floor. Without it, connecting a five year old store pulls "
             "five years of history on the first run.")

    team_id = fields.Many2one('crm.team', string='Sales team')
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse')
    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist')
    journal_id = fields.Many2one('account.journal', string='Invoice journal',
                                 domain=[('type', '=', 'sale')])
    default_customer_id = fields.Many2one(
        'res.partner', string='Fallback customer',
        help="Used when an order arrives with no usable customer details, which "
             "happens more often than any platform's documentation admits.")

    mapping_count = fields.Integer(compute='_compute_counts')
    job_count = fields.Integer(compute='_compute_counts')
    failed_job_count = fields.Integer(compute='_compute_counts')
    last_sync = fields.Datetime(readonly=True)

    def _compute_counts(self):
        Mapping = self.env['miko.ecommerce.mapping']
        Job = self.env['miko.ecommerce.job']
        for ch in self:
            ch.mapping_count = Mapping.search_count([('channel_id', '=', ch.id)])
            ch.job_count = Job.search_count([('channel_id', '=', ch.id)])
            ch.failed_job_count = Job.search_count(
                [('channel_id', '=', ch.id), ('state', '=', 'failed')])

    def action_view_jobs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync jobs'),
            'res_model': 'miko.ecommerce.job',
            'view_mode': 'list,form',
            'domain': [('channel_id', '=', self.id)],
            'context': {'default_channel_id': self.id},
        }

    def _touch_sync(self):
        self.sudo().write({'last_sync': fields.Datetime.now()})
