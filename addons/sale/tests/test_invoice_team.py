# Part of Odoo. See LICENSE file for full copyright and licensing details.
from unittest.mock import patch

from odoo import Command
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('-at_install', 'post_install')
class TestInvoiceTeam(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(
            cls.env.context, tracking_disable=True, no_reset_password=True,
        ))
        cls.env['ir.config_parameter'].set_param('sales_team.membership_multi', False)
        cls.salesperson = new_test_user(
            cls.env, login='invoice_team_salesperson', password='InvoiceTeamTest123!',
        )
        cls.other_salesperson = new_test_user(
            cls.env, login='invoice_team_other_salesperson', password='InvoiceTeamTest123!',
        )
        cls.team, cls.other_team = cls.env['crm.team'].create([
            {'name': 'Invoice team', 'member_ids': [Command.link(cls.salesperson.id)]},
            {'name': 'Other invoice team', 'member_ids': [Command.link(cls.other_salesperson.id)]},
        ])
        cls.partners = cls.env['res.partner'].create([
            {'name': 'Invoice team customer', 'user_id': cls.salesperson.id},
            {'name': 'Other team customer', 'user_id': cls.other_salesperson.id},
        ])
        cls.journal = cls.env['account.journal'].create({
            'name': 'Invoice team test journal', 'code': 'TTEAM', 'type': 'sale',
            'company_id': cls.env.company.id,
        })
        cls.invoices = cls.env['account.move'].create([
            {'move_type': 'out_invoice', 'journal_id': cls.journal.id, 'partner_id': partner.id}
            for partner in cls.partners
        ])
        cls.env.flush_all()

    def test_invoice_team_field_contract(self):
        field = self.invoices._fields['team_id']
        self.assertTrue(field.store)
        self.assertTrue(field.readonly)
        self.assertTrue(field.compute_sudo)
        self.assertFalse(field.copy)
        self.assertEqual(field.comodel_name, 'crm.team')
        self.assertEqual(field.get_depends(self.invoices)[0], ['commercial_partner_id.user_id.sale_team_id'])

    def test_invoice_team_follows_membership_transfer_and_removal(self):
        self.assertEqual(self.invoices[0].team_id, self.team)
        self.other_team.write({'member_ids': [Command.link(self.salesperson.id)]})
        self.env.flush_all()
        self.assertFalse(self.team.member_ids)
        self.assertEqual(self.invoices[0].team_id, self.other_team)

        self.other_team.write({'member_ids': [Command.unlink(self.salesperson.id)]})
        self.env.flush_all()
        self.assertFalse(self.invoices[0].team_id)

    def test_invoice_team_follows_customer_and_salesperson(self):
        self.partners[0].user_id = self.other_salesperson
        self.env.flush_all()
        self.assertEqual(self.invoices[0].team_id, self.other_team)

        self.invoices[1].partner_id = self.partners[0]
        self.partners[0].user_id = self.salesperson
        self.env.flush_all()
        self.assertEqual(self.invoices.mapped('team_id'), self.team)

        self.partners[0].user_id = False
        self.env.flush_all()
        self.assertFalse(self.invoices.mapped('team_id'))

    def test_invoice_team_uses_commercial_partner(self):
        child = self.env['res.partner'].create({
            'name': 'Customer contact', 'parent_id': self.partners[0].id,
            'user_id': self.other_salesperson.id,
        })
        self.invoices[0].partner_id = child
        self.env.flush_all()
        self.assertEqual(self.invoices[0].team_id, self.team)

    def test_unchanged_members_still_trigger_invoice_team_recomputation(self):
        computed_ids = set()
        model = type(self.invoices)
        original = model._compute_field_value

        def observe(records, field):
            if field.name == 'team_id':
                computed_ids.update(records.ids)
            return original(records, field)

        with patch.object(model, '_compute_field_value', observe):
            self.team.write({'member_ids': [Command.set(self.team.member_ids.ids)]})
            self.env.flush_all()
        self.assertIn(self.invoices[0].id, computed_ids)
        self.assertEqual(self.invoices[0].team_id, self.team)

    def test_single_team_synchronization_after_multi_team_mode(self):
        params = self.env['ir.config_parameter']
        params.set_param('sales_team.membership_multi', True)
        self.other_team.write({'member_ids': [Command.link(self.salesperson.id)]})
        self.env.flush_all()
        self.assertIn(self.salesperson, self.other_team.member_ids)

        params.set_param('sales_team.membership_multi', False)
        self.team.write({'member_ids': [Command.set(self.team.member_ids.ids)]})
        self.env.flush_all()
        self.assertIn(self.salesperson, self.team.member_ids)
        self.assertNotIn(self.salesperson, self.other_team.member_ids)
        self.assertEqual(self.invoices[0].team_id, self.team)

    def test_invoice_team_does_not_prefetch_unrelated_invoice_fields(self):
        read_fields = set()
        model = type(self.invoices)
        original = model._read

        def observe(records, names):
            if set(records.ids).intersection(self.invoices.ids):
                read_fields.update(names)
            return original(records, names)

        self.invoices.invalidate_recordset()
        self.env.add_to_compute(self.invoices._fields['team_id'], self.invoices)
        with patch.object(model, '_read', observe):
            self.invoices._recompute_recordset(['team_id'])
        self.assertIn('commercial_partner_id', read_fields)
        self.assertNotIn('narration', read_fields)
        self.assertEqual(self.invoices[0].team_id, self.team)
        self.assertEqual(self.invoices[1].team_id, self.other_team)
