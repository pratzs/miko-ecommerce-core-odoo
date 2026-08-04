# -*- coding: utf-8 -*-
"""The sync queue, and the thing that actually runs it.

A connector that talks to someone else's server over the internet will fail. The
question is only what happens next, and the answer decides whether anyone can
trust the integration.

**A failure is kept, never lost.** Every unit of work is a row holding the exact
payload that caused it, so it can be inspected, fixed and run again without going
back to the store for the data.

**A queue that nothing drains is just a log.** Jobs are re-executed here, by
dispatching back to a `_job_<operation>` method on the channel. An earlier version
had a Retry button that only set the state back to Waiting, which looked like it
worked and did nothing at all.

**Two kinds of failure, because they need different things from a human.**

* `failed` is transient. The network dropped, the store returned a 502, a rate
  limit was hit. Retrying is the right answer and the scheduler does it, backing
  off further each time.
* `blocked` needs somebody to change something first: a tax with no mapping, a
  product that is not in Odoo, an expired token. Retrying on a timer would just
  burn the attempt budget and bury the real message, so these wait, and each one
  carries the sentence explaining what to fix.
"""
import json
import logging
import traceback

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

STATES = [
    ('pending', 'Waiting'),
    ('done', 'Done'),
    ('failed', 'Failed'),
    ('blocked', 'Needs attention'),
    ('skipped', 'Skipped'),
]


class MikoEcommerceJob(models.Model):
    _name = 'miko.ecommerce.job'
    _description = 'Sync job'
    _order = 'create_date desc'
    _rec_name = 'operation'

    channel_id = fields.Many2one(
        'miko.ecommerce.channel', required=True, ondelete='cascade', index=True)
    operation = fields.Char(required=True, index=True,
                            help="What was being done, such as import_order.")
    direction = fields.Selection(
        [('in', 'Store to Odoo'), ('out', 'Odoo to store')],
        default='in', index=True)
    external_id = fields.Char(index=True)
    external_ref = fields.Char(string='Store reference')
    payload = fields.Text(help="Exactly what was being processed, kept so a "
                               "failure can be replayed without fetching it again.")
    state = fields.Selection(STATES, default='pending', required=True, index=True)
    error = fields.Text()
    guidance = fields.Text(
        string='What to do',
        help="For a job that cannot simply be retried: the one thing a person "
             "has to change before it can succeed.")
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=3)
    next_retry = fields.Datetime(
        index=True, help="Retries back off, so a store that is down is not "
                         "hammered every fifteen minutes.")
    odoo_ref = fields.Char(string='Created record')

    # ------------------------------------------------------------------
    @api.model
    def enqueue(self, channel, operation, external_id=None, payload=None,
                external_ref=None, direction='in'):
        return self.create({
            'channel_id': channel.id,
            'operation': operation,
            'direction': direction,
            'external_id': str(external_id) if external_id else False,
            'external_ref': external_ref or False,
            'payload': json.dumps(payload, default=str) if payload is not None else False,
        })

    def mark_done(self, record=None):
        self.write({
            'state': 'done',
            'error': False,
            'guidance': False,
            'next_retry': False,
            'odoo_ref': ('%s,%s' % (record._name, record.id)) if record else False,
        })

    def mark_skipped(self, reason):
        """Already imported, or deliberately out of scope. Not a failure."""
        self.write({'state': 'skipped', 'error': reason})

    def mark_failed(self, err, blocked=False, guidance=None):
        """Record a failure, and say which kind it is.

        `blocked` means a person has to change something; those are never retried
        automatically, because a timer cannot fix a missing tax mapping.
        """
        text = err if isinstance(err, str) else traceback.format_exc()
        for job in self:
            attempts = (job.attempts or 0) + 1
            values = {
                'attempts': attempts,
                'error': (text or '')[:8000],
                'guidance': guidance or job.guidance or False,
            }
            if blocked:
                values.update({'state': 'blocked', 'next_retry': False})
            else:
                values['state'] = 'failed'
                # Exponential backoff: 2, 4, 8... minutes. A store that is down
                # stays down for a while, and retrying hard makes it worse.
                delay = min(2 ** attempts, 240)
                values['next_retry'] = fields.Datetime.add(
                    fields.Datetime.now(), minutes=delay)
            job.write(values)

    def mark_blocked(self, message, guidance=None):
        return self.mark_failed(message, blocked=True, guidance=guidance)

    # ------------------------------------------------------------------
    def run(self):
        """Execute these jobs by dispatching back to their channel.

        The channel implements `_job_<operation>(payload, job)`. Returning a
        recordset links it to the job; returning None is a success with nothing
        to link.
        """
        for job in self:
            channel = job.channel_id
            handler = '_job_%s' % (job.operation or '')
            if not hasattr(channel, handler):
                # A payload with no handler cannot ever succeed, so it must not
                # sit in the queue pretending it might.
                job.mark_blocked(
                    _("There is no handler for the operation '%s'.") % job.operation,
                    _("This job was written by a different version of the module. "
                      "It is safe to delete."))
                continue
            try:
                record = getattr(channel, handler)(job.get_payload(), job)
            except Exception as err:            # noqa: BLE001 - kept, not lost
                _logger.exception("miko: job %s (%s) failed", job.id, job.operation)
                self.env.cr.rollback()
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(record if record is not None and getattr(
                    record, '_name', None) else None)
        return True

    def _is_blocking(self, err):
        """Whether a person has to act before this could ever succeed.

        Overridden per platform. The default is deliberately conservative: treat
        anything raised as a plain UserError as something we chose to refuse, and
        therefore something only a human can clear.
        """
        from odoo.exceptions import UserError, ValidationError
        return isinstance(err, (UserError, ValidationError))

    def _guidance_for(self, err):
        """The sentence a person needs. Platform modules give better ones."""
        from odoo.exceptions import UserError, ValidationError
        if isinstance(err, (UserError, ValidationError)):
            return str(err)
        return None

    def action_retry(self):
        """Run these jobs again now.

        Works on blocked jobs too: the whole point of blocked is that a person
        fixed something and wants to try again immediately.
        """
        runnable = self.filtered(lambda j: j.state in ('failed', 'blocked', 'pending'))
        runnable.write({'state': 'pending', 'error': False, 'next_retry': False})
        runnable.run()
        return True

    def action_reset_attempts(self):
        """Give up on giving up. For a job that exhausted its attempts."""
        self.write({'attempts': 0, 'next_retry': False})
        return True

    # ------------------------------------------------------------------
    @api.model
    def _cron_retry_failed(self, limit=200):
        """Retry transient failures that are due. Never touches blocked ones."""
        due = self.search([
            ('state', '=', 'failed'),
            ('attempts', '<', 3),
            '|', ('next_retry', '=', False),
                 ('next_retry', '<=', fields.Datetime.now()),
        ], limit=limit)
        # attempts is compared against the column default rather than
        # max_attempts because a domain cannot compare two fields; jobs wanting a
        # different budget are filtered in Python.
        due = due.filtered(lambda j: j.attempts < (j.max_attempts or 3))
        if due:
            _logger.info("miko: retrying %s failed job(s)", len(due))
            due.run()
        return True

    def get_payload(self):
        self.ensure_one()
        if not self.payload:
            return {}
        try:
            return json.loads(self.payload)
        except Exception:
            return {}
