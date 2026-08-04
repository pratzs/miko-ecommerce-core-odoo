# -*- coding: utf-8 -*-
"""Identity mapping: which Odoo record is which store record.

This one table is the difference between a connector that can be trusted and one
that cannot. Everything else is API plumbing.

The failure it prevents is the expensive one. A sync runs, the connection drops
halfway, somebody runs it again, and now every order that came through the first
time exists twice: two sales orders, two deliveries, two invoices, and a customer
charged twice. Recovering from that by hand takes days, and it destroys confidence
in the integration permanently.

So every record that crosses the boundary gets a row here, keyed on
(channel, model, external id), with a unique index behind it. Import becomes an
upsert rather than a create, and a re-run is a no-op instead of a disaster.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class MikoEcommerceMapping(models.Model):
    _name = 'miko.ecommerce.mapping'
    _description = 'Link between an Odoo record and a store record'
    _rec_name = 'external_id'
    _order = 'channel_id, model_name, external_id'

    channel_id = fields.Many2one(
        'miko.ecommerce.channel', string='Store', required=True,
        ondelete='cascade', index=True)
    model_name = fields.Char(
        string='Odoo model', required=True, index=True,
        help="The Odoo model this row points at, such as sale.order.")
    odoo_id = fields.Integer(
        string='Odoo record', required=True, index=True,
        help="Stored as a plain integer rather than a reference so a deleted "
             "record leaves a visible orphan instead of silently vanishing.")
    external_id = fields.Char(
        string='Store id', required=True, index=True,
        help="The identifier the store uses. Always text: some platforms use "
             "numbers, some use GraphQL global ids, and one of them changed from "
             "the first to the second between API versions.")
    external_ref = fields.Char(
        string='Store reference',
        help="The human readable one, such as an order number. For finding things, "
             "never for matching on.")
    last_synced = fields.Datetime(readonly=True)
    sync_hash = fields.Char(
        help="Fingerprint of the payload last written. Lets a sync skip records "
             "that have not actually changed, rather than rewriting everything "
             "every run.")

    def init(self):
        """Create the unique index directly, on every Odoo series.

        Not _sql_constraints: Odoo 19 replaced that with models.Constraint, so a
        declaration written once is silently absent on one series or the other,
        and the series without it has NO protection against two concurrent
        webhooks both passing the Python check and creating duplicates. A race is
        exactly how a connector duplicates orders in production, so this belongs
        in the database rather than in Python alone.
        """
        super().init()
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS miko_ecommerce_mapping_external_uniq
                ON miko_ecommerce_mapping (channel_id, model_name, external_id)
        """)

    # The Python constraint stays as well. The index is the guarantee; this is
    # what turns a raw database error into a sentence somebody can act on.
    @api.constrains('channel_id', 'model_name', 'external_id')
    def _check_unique_external(self):
        for rec in self:
            clash = self.search([
                ('channel_id', '=', rec.channel_id.id),
                ('model_name', '=', rec.model_name),
                ('external_id', '=', rec.external_id),
                ('id', '!=', rec.id),
            ], limit=1)
            if clash:
                raise ValidationError(_(
                    "Store record %(ext)s is already linked to Odoo %(model)s "
                    "%(odoo)s on this store. Linking it twice is what creates "
                    "duplicate orders.") % {
                        'ext': rec.external_id, 'model': rec.model_name,
                        'odoo': clash.odoo_id})

    # ------------------------------------------------------------------
    @api.model
    def find_odoo_record(self, channel, model_name, external_id):
        """The Odoo record for a store id, or an empty recordset.

        Returns a real recordset, and quietly repairs the mapping if the Odoo
        record has since been deleted, so a stale row can never make the caller
        think something exists when it does not.
        """
        if not external_id:
            return self.env[model_name].browse()
        row = self.search([
            ('channel_id', '=', channel.id),
            ('model_name', '=', model_name),
            ('external_id', '=', str(external_id)),
        ], limit=1)
        if not row:
            return self.env[model_name].browse()
        record = self.env[model_name].browse(row.odoo_id).exists()
        if not record:
            row.unlink()      # the Odoo side is gone; the link is meaningless
        return record

    @api.model
    def link(self, channel, record, external_id, external_ref=None, sync_hash=None):
        """Record that an Odoo record and a store record are the same thing."""
        row = self.search([
            ('channel_id', '=', channel.id),
            ('model_name', '=', record._name),
            ('external_id', '=', str(external_id)),
        ], limit=1)
        values = {
            'odoo_id': record.id,
            'external_ref': external_ref or False,
            'last_synced': fields.Datetime.now(),
            'sync_hash': sync_hash or False,
        }
        if row:
            row.write(values)
            return row
        values.update({
            'channel_id': channel.id,
            'model_name': record._name,
            'external_id': str(external_id),
        })
        return self.create(values)

    @api.model
    def _channel_for(self, record):
        """The channel a given Odoo record came from, or an empty recordset.

        The inverse of find_odoo_record, and the question every export path has
        to answer before it writes anything: this record is only a store's
        business if the store is where it came from.
        """
        row = self.search([
            ('model_name', '=', record._name),
            ('odoo_id', '=', record.id),
        ], limit=1)
        return row.channel_id

    @api.model
    def already_imported(self, channel, model_name, external_id):
        """True when this store record has already been brought in.

        The guard every import must call before creating anything.
        """
        return bool(self.find_odoo_record(channel, model_name, external_id))
