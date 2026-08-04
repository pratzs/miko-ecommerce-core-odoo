# -*- coding: utf-8 -*-
"""Engine tests.

The one that matters is duplicate prevention. Everything else in a connector is
recoverable; importing every order twice is not.
"""
from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMapping(TransactionCase):

    def setUp(self):
        super().setUp()
        self.channel = self.env['miko.ecommerce.channel'].create({'name': 'Test store'})
        self.other = self.env['miko.ecommerce.channel'].create({'name': 'Second store'})
        self.Map = self.env['miko.ecommerce.mapping']
        self.partner = self.env['res.partner'].create({'name': 'Web customer'})

    def test_a_store_record_cannot_be_linked_twice(self):
        """The whole point. Without this, a re-run duplicates every order.

        Asserts that it is REFUSED, not which layer refuses it. The database
        index and the Python constraint both guard this, and which one fires
        first is an implementation detail that varies by Odoo series. What must
        never vary is that the second link fails.
        """
        from psycopg2 import IntegrityError
        from odoo.tools import mute_logger
        self.Map.link(self.channel, self.partner, '1001')
        second = self.env['res.partner'].create({'name': 'Another'})
        # An explicit try/except rather than assertRaises: Odoo's override of
        # assertRaises calls issubclass() on its argument, so it cannot take a
        # tuple of acceptable exceptions.
        refused = False
        with mute_logger('odoo.sql_db'):
            try:
                with self.env.cr.savepoint():
                    self.Map.create({
                        'channel_id': self.channel.id,
                        'model_name': 'res.partner',
                        'external_id': '1001',
                        'odoo_id': second.id,
                    })
            except (ValidationError, IntegrityError):
                refused = True
        self.assertTrue(refused, 'a second link to the same store record must fail')

    def test_the_unique_index_exists_in_the_database(self):
        """The Python constraint alone loses a race between two webhooks."""
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes "
            " WHERE tablename = 'miko_ecommerce_mapping'"
            "   AND indexname = 'miko_ecommerce_mapping_external_uniq'")
        row = self.env.cr.fetchone()
        self.assertTrue(row, 'the unique index must exist on every series')
        self.assertIn('UNIQUE', row[0].upper())

    def test_linking_the_same_pair_again_updates_rather_than_duplicates(self):
        a = self.Map.link(self.channel, self.partner, '1001', external_ref='#1001')
        b = self.Map.link(self.channel, self.partner, '1001', external_ref='#1001-b')
        self.assertEqual(a.id, b.id, 'a second link must update, not create')
        self.assertEqual(self.Map.search_count([
            ('channel_id', '=', self.channel.id), ('external_id', '=', '1001')]), 1)

    def test_the_same_store_id_on_two_stores_is_fine(self):
        """Two platforms both numbering their orders from 1 is normal."""
        other_partner = self.env['res.partner'].create({'name': 'Other web customer'})
        self.Map.link(self.channel, self.partner, '1')
        self.Map.link(self.other, other_partner, '1')
        self.assertEqual(self.Map.search_count([('external_id', '=', '1')]), 2)

    def test_already_imported_is_the_guard_an_import_must_call(self):
        self.assertFalse(self.Map.already_imported(self.channel, 'res.partner', '55'))
        self.Map.link(self.channel, self.partner, '55')
        self.assertTrue(self.Map.already_imported(self.channel, 'res.partner', '55'))

    def test_find_returns_a_real_recordset(self):
        self.Map.link(self.channel, self.partner, '77')
        found = self.Map.find_odoo_record(self.channel, 'res.partner', '77')
        self.assertEqual(found, self.partner)

    def test_a_stale_link_repairs_itself(self):
        """If the Odoo record was deleted, the link must not claim it exists."""
        doomed = self.env['res.partner'].create({'name': 'To be deleted'})
        self.Map.link(self.channel, doomed, '88')
        doomed.unlink()
        found = self.Map.find_odoo_record(self.channel, 'res.partner', '88')
        self.assertFalse(found)
        self.assertFalse(self.Map.search([('external_id', '=', '88')]),
                         'the dead link must be cleaned up, not left to mislead')

    def test_external_ids_are_compared_as_text(self):
        """Platforms have changed numeric ids to string ids between API versions.
        Matching on text means that change does not orphan every mapping."""
        self.Map.link(self.channel, self.partner, 1001)
        self.assertTrue(self.Map.already_imported(self.channel, 'res.partner', '1001'))
        self.assertTrue(self.Map.already_imported(self.channel, 'res.partner', 1001))

    def test_a_missing_external_id_never_matches(self):
        self.assertFalse(self.Map.find_odoo_record(self.channel, 'res.partner', None))
        self.assertFalse(self.Map.find_odoo_record(self.channel, 'res.partner', ''))


@tagged('post_install', '-at_install')
class TestJobQueue(TransactionCase):

    def setUp(self):
        super().setUp()
        self.channel = self.env['miko.ecommerce.channel'].create({'name': 'Queue store'})
        self.Job = self.env['miko.ecommerce.job']

    def test_a_job_keeps_the_payload_that_caused_it(self):
        job = self.Job.enqueue(self.channel, 'import_order', external_id='9',
                               payload={'id': 9, 'total': '12.50'},
                               external_ref='#1009')
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.get_payload()['total'], '12.50')

    def test_a_failure_is_kept_not_lost(self):
        job = self.Job.enqueue(self.channel, 'import_order', external_id='9')
        job.mark_failed('the store returned 502')
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.attempts, 1)
        self.assertIn('502', job.error)

    def test_retry_actually_runs_the_job_again(self):
        """Retry has to re-execute, not just relabel.

        The first version of this only wrote state='pending' and nothing ever
        drained the queue, so the button looked like it worked and did nothing at
        all. The contract now is that retrying dispatches back to the channel.
        """
        ran = []
        job = self.Job.enqueue(self.channel, 'demo_op', external_id='9')
        job.mark_failed('first go')

        def handler(payload, job=None):
            ran.append(payload)
            return None

        # A handler the dispatcher can find, added for this test only.
        type(self.channel)._job_demo_op = lambda self, payload, job=None: handler(payload, job)
        try:
            job.action_retry()
        finally:
            del type(self.channel)._job_demo_op

        self.assertEqual(len(ran), 1, 'retry must call the handler')
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.attempts, 1,
                         'attempts must persist so a repeat offender is visible')

    def test_a_job_with_no_handler_is_blocked_not_left_waiting(self):
        """A payload nothing can process must not sit pretending it might."""
        job = self.Job.enqueue(self.channel, 'operation_that_does_not_exist')
        job.run()
        self.assertEqual(job.state, 'blocked')
        self.assertTrue(job.guidance, 'a blocked job must say what to do about it')

    def test_a_transient_failure_backs_off_rather_than_hammering(self):
        job = self.Job.enqueue(self.channel, 'import_order', external_id='9')
        job.mark_failed('network went away')
        self.assertEqual(job.state, 'failed')
        self.assertTrue(job.next_retry, 'a retryable failure must be scheduled')

    def test_a_blocked_job_is_never_retried_on_a_timer(self):
        """A timer cannot fix a missing tax mapping, so it must not try.

        Retrying these would burn the attempt budget and bury the one message
        that says what a person has to change.
        """
        job = self.Job.enqueue(self.channel, 'import_order', external_id='9')
        job.mark_blocked('no tax mapped', 'Map the tax, then retry.')
        self.assertEqual(job.state, 'blocked')
        self.assertFalse(job.next_retry)
        self.Job._cron_retry_failed()
        self.assertEqual(job.state, 'blocked', 'the cron must leave blocked alone')

    def test_the_cron_picks_up_a_due_failure(self):
        ran = []
        job = self.Job.enqueue(self.channel, 'demo_due')
        job.mark_failed('temporary')
        job.next_retry = fields.Datetime.subtract(fields.Datetime.now(), minutes=1)
        type(self.channel)._job_demo_due = lambda self, payload, job=None: ran.append(1)
        try:
            self.Job._cron_retry_failed()
        finally:
            del type(self.channel)._job_demo_due
        self.assertEqual(len(ran), 1)
        self.assertEqual(job.state, 'done')

    def test_skipped_is_not_a_failure(self):
        job = self.Job.enqueue(self.channel, 'import_order', external_id='9')
        job.mark_skipped('already imported')
        self.assertEqual(job.state, 'skipped')
        self.assertEqual(self.Job.search_count(
            [('channel_id', '=', self.channel.id), ('state', '=', 'failed')]), 0)

    def test_a_done_job_records_what_it_created(self):
        partner = self.env['res.partner'].create({'name': 'Created by sync'})
        job = self.Job.enqueue(self.channel, 'import_customer', external_id='3')
        job.mark_done(partner)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.odoo_ref, 'res.partner,%s' % partner.id)

    def test_bad_payload_json_does_not_raise(self):
        job = self.Job.enqueue(self.channel, 'import_order')
        job.payload = '{not json'
        self.assertEqual(job.get_payload(), {})


@tagged('post_install', '-at_install')
class TestChannelDefaults(TransactionCase):

    def test_nothing_irreversible_happens_on_a_first_sync(self):
        ch = self.env['miko.ecommerce.channel'].create({'name': 'Fresh store'})
        self.assertFalse(ch.auto_confirm_orders,
                         'a first sync must not confirm orders')
        self.assertFalse(ch.auto_create_invoice,
                         'a first sync must not invoice anyone')

    def test_failed_job_count_surfaces_on_the_channel(self):
        ch = self.env['miko.ecommerce.channel'].create({'name': 'Counting store'})
        job = self.env['miko.ecommerce.job'].enqueue(ch, 'import_order')
        job.mark_failed('boom')
        ch.invalidate_recordset() if hasattr(ch, 'invalidate_recordset') else None
        self.assertEqual(ch.failed_job_count, 1)
