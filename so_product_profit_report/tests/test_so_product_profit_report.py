# -*- coding: utf-8 -*-
import base64
import io
import zipfile
from datetime import date, datetime

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase

from odoo.addons.so_product_profit_report.wizard.so_product_profit_wizard import text_direction


@tagged('post_install', '-at_install', 'so_product_profit_report')
class TestSoProductProfitReport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, tz='UTC'))
        cls.env.user.tz = 'UTC'
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id
        cls.pricelist = cls.env['product.pricelist'].create({
            'name': 'SOPR Test Pricelist',
            'currency_id': cls.currency.id,
            'company_id': cls.company.id,
        })
        cls.customer_a = cls.env['res.partner'].create({'name': 'SOPR Customer A', 'is_company': True})
        cls.contact_a = cls.env['res.partner'].create({
            'name': 'SOPR Contact A', 'parent_id': cls.customer_a.id, 'type': 'contact'})
        cls.customer_b = cls.env['res.partner'].create({'name': 'SOPR Customer B'})

        cls.product_x = cls.env['product.product'].create({
            'name': 'SOPR Product X', 'type': 'consu', 'list_price': 100.0, 'standard_price': 60.0})
        cls.product_y = cls.env['product.product'].create({
            'name': 'SOPR Product Y', 'type': 'consu', 'list_price': 50.0, 'standard_price': 30.0})

        # Order 1 (Customer A, 15 Jan): X 2 x 100 (cost 60) ; Y 4 x 50 -10% (cost 30)
        cls.order_1 = cls._create_order(cls.customer_a, datetime(2026, 1, 15, 10, 0), [
            (cls.product_x, 2.0, 100.0, 0.0),
            (cls.product_y, 4.0, 50.0, 10.0),
        ])
        # Order 2 (contact of Customer A, 20 Jan): Y 1 x 20 (cost 30) -> a loss line
        cls.order_2 = cls._create_order(cls.contact_a, datetime(2026, 1, 20, 9, 0), [
            (cls.product_y, 1.0, 20.0, 0.0),
        ])
        # Order 3 (Customer B, 10 Feb): X 1 x 150
        cls.order_3 = cls._create_order(cls.customer_b, datetime(2026, 2, 10, 12, 0), [
            (cls.product_x, 1.0, 150.0, 0.0),
        ])
        # Quotation (not confirmed) must never appear
        cls.quotation = cls.env['sale.order'].create({
            'partner_id': cls.customer_a.id,
            'pricelist_id': cls.pricelist.id,
            'order_line': [(0, 0, {
                'product_id': cls.product_x.id, 'product_uom_qty': 99.0, 'price_unit': 1.0})],
        })
        cls.quotation.date_order = datetime(2026, 1, 16, 10, 0)

    @classmethod
    def _create_order(cls, partner, date_order, lines):
        order = cls.env['sale.order'].create({
            'partner_id': partner.id,
            'pricelist_id': cls.pricelist.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': qty,
                'price_unit': price,
                'discount': discount,
            }) for product, qty, price, discount in lines],
        })
        order.action_confirm()
        # action_confirm() stamps date_order with "now": set the test date back.
        order.date_order = date_order
        return order

    def _wizard(self, **values):
        vals = {
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 1, 31),
            'company_ids': [(6, 0, self.company.ids)],
            'report_lang': 'en_US',
        }
        vals.update(values)
        return self.env['so.product.profit.wizard'].create(vals)

    # ------------------------------------------------------------------
    # Wizard parameter validation
    # ------------------------------------------------------------------
    def test_01_defaults(self):
        wizard = Form(self.env['so.product.profit.wizard']).save()
        today = fields.Date.context_today(wizard)
        self.assertEqual(wizard.date_from, today.replace(day=1))
        self.assertEqual(wizard.date_to, today)
        self.assertEqual(wizard.company_ids, self.env.companies)
        self.assertEqual(wizard.group_by, 'none')
        self.assertTrue(wizard.report_lang)

    def test_02_start_after_end_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._wizard(date_from=date(2026, 2, 1), date_to=date(2026, 1, 1))
        wizard = self._wizard()
        with self.assertRaises(ValidationError):
            wizard.write({'date_from': date(2026, 3, 1)})

    def test_03_single_day_range_is_valid(self):
        wizard = self._wizard(date_from=date(2026, 1, 15), date_to=date(2026, 1, 15))
        data = wizard._get_report_data()
        self.assertEqual(len(data['lines']), 2)

    def test_04_companies_with_different_currencies_rejected(self):
        other_currency = self.env['res.currency'].search(
            [('id', '!=', self.currency.id), ('active', 'in', [True, False])], limit=1)
        other_currency.active = True
        other_company = self.env['res.company'].create({
            'name': 'SOPR Other Currency Co', 'currency_id': other_currency.id})
        wizard = self._wizard(company_ids=[(6, 0, (self.company | other_company).ids)])
        for method in ('action_view_report', 'action_print_pdf', 'action_export_xlsx'):
            with self.assertRaises(UserError, msg=method):
                getattr(wizard, method)()

    def test_05_empty_result_blocks_pdf_and_xlsx_only(self):
        wizard = self._wizard(date_from=date(2025, 1, 1), date_to=date(2025, 1, 31))
        self.assertFalse(wizard._get_report_data()['lines'])
        with self.assertRaises(UserError):
            wizard.action_print_pdf()
        with self.assertRaises(UserError):
            wizard.action_export_xlsx()
        action = wizard.action_view_report()  # the browser view may be empty
        self.assertEqual(action['res_model'], 'so.product.profit.report')

    # ------------------------------------------------------------------
    # Profit / margin math
    # ------------------------------------------------------------------
    def test_10_line_math(self):
        data = self._wizard()._get_report_data()
        lines = {(l['order'], l['product_id']): l for l in data['lines']}
        self.assertEqual(len(lines), 3, "Quotation and February order must be excluded")

        x = lines[(self.order_1.name, self.product_x.id)]
        self.assertAlmostEqual(x['qty'], 2.0)
        self.assertAlmostEqual(x['unit_cost'], 60.0)
        self.assertAlmostEqual(x['unit_price'], 100.0)
        self.assertAlmostEqual(x['cost'], 120.0)
        self.assertAlmostEqual(x['sales'], 200.0)
        self.assertAlmostEqual(x['profit'], 80.0)
        self.assertAlmostEqual(x['margin'], 40.0, places=4)

        y = lines[(self.order_1.name, self.product_y.id)]
        self.assertAlmostEqual(y['unit_price'], 45.0, msg="Discount must be applied")
        self.assertAlmostEqual(y['cost'], 120.0)
        self.assertAlmostEqual(y['sales'], 180.0)
        self.assertAlmostEqual(y['profit'], 60.0)
        self.assertAlmostEqual(y['margin'], 100.0 / 3.0, places=4)

        loss = lines[(self.order_2.name, self.product_y.id)]
        self.assertAlmostEqual(loss['profit'], -10.0)
        self.assertAlmostEqual(loss['margin'], -50.0, places=4)

    def test_11_totals_are_weighted_not_averaged(self):
        totals = self._wizard()._get_report_data()['totals']
        self.assertAlmostEqual(totals['sales'], 400.0)
        self.assertAlmostEqual(totals['cost'], 270.0)
        self.assertAlmostEqual(totals['profit'], 130.0)
        self.assertAlmostEqual(totals['margin'], 32.5, places=4)
        self.assertEqual(totals['count'], 3)

    def test_12_group_by_product_subtotals(self):
        data = self._wizard(group_by='product')._get_report_data()
        self.assertTrue(data['grouped'])
        groups = {g['label']: g['totals'] for g in data['groups']}
        self.assertEqual(set(groups), {self.product_x.display_name, self.product_y.display_name})
        y = groups[self.product_y.display_name]
        self.assertAlmostEqual(y['qty'], 5.0)
        self.assertAlmostEqual(y['sales'], 200.0)
        self.assertAlmostEqual(y['profit'], 50.0)
        self.assertAlmostEqual(y['margin'], 25.0, places=4)
        self.assertAlmostEqual(
            sum(g['totals']['profit'] for g in data['groups']), data['totals']['profit'])

    def test_13_read_group_recomputes_ratios(self):
        Report = self.env['so.product.profit.report']
        domain = self._wizard()._get_report_domain()
        groups = Report.read_group(
            domain, ['profit:sum', 'total_sales:sum', 'margin_percent:avg', 'unit_price:avg'],
            ['product_id'])
        by_product = {g['product_id'][0]: g for g in groups}
        y = by_product[self.product_y.id]
        # Simple average of (33.33, -50) would be -8.33; weighted margin is 25.
        self.assertAlmostEqual(y['margin_percent'], 25.0, places=4)
        self.assertAlmostEqual(y['unit_price'], 40.0, places=4)
        self.assertNotIn('sopr_sales', y)

    def test_14_multi_currency_order_converted_to_company_currency(self):
        foreign = self.env['res.currency'].search(
            [('id', '!=', self.currency.id), ('active', 'in', [True, False])], limit=1)
        foreign.active = True
        self.env['res.currency.rate'].search([('currency_id', '=', foreign.id)]).unlink()
        self.env['res.currency.rate'].create({
            'currency_id': foreign.id, 'company_id': self.company.id,
            'name': date(2026, 1, 1), 'rate': 2.0})
        pricelist = self.env['product.pricelist'].create(
            {'name': 'SOPR Foreign', 'currency_id': foreign.id})
        order = self.env['sale.order'].create({
            'partner_id': self.customer_b.id,
            'pricelist_id': pricelist.id,
            'date_order': datetime(2026, 1, 25, 8, 0),
            'order_line': [(0, 0, {
                'product_id': self.product_x.id, 'product_uom_qty': 1.0, 'price_unit': 400.0})],
        })
        order.action_confirm()
        order.date_order = datetime(2026, 1, 25, 8, 0)
        data = self._wizard(partner_ids=[(6, 0, self.customer_b.ids)])._get_report_data()
        self.assertEqual(len(data['lines']), 1)
        line = data['lines'][0]
        rate = order.currency_rate or 1.0
        self.assertAlmostEqual(line['sales'], 400.0 / rate, places=2)
        self.assertAlmostEqual(line['cost'], order.order_line.purchase_price / rate, places=2)

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def test_20_customer_filter_includes_child_contacts(self):
        data = self._wizard(partner_ids=[(6, 0, self.customer_a.ids)])._get_report_data()
        self.assertEqual({l['order'] for l in data['lines']},
                         {self.order_1.name, self.order_2.name})
        data_b = self._wizard(date_to=date(2026, 2, 28),
                              partner_ids=[(6, 0, self.customer_b.ids)])._get_report_data()
        self.assertEqual({l['order'] for l in data_b['lines']}, {self.order_3.name})

    def test_21_end_date_is_inclusive(self):
        self.order_3.date_order = datetime(2026, 1, 31, 23, 59, 59)
        data = self._wizard()._get_report_data()
        self.assertIn(self.order_3.name, {l['order'] for l in data['lines']})

    def test_22_timezone_boundaries(self):
        # 2026-01-31 22:30 UTC is already 1 Feb in Riyadh (UTC+3)
        self.order_3.date_order = datetime(2026, 1, 31, 22, 30)
        wizard = self._wizard().with_context(tz='Asia/Riyadh')
        self.assertNotIn(self.order_3.name,
                         {l['order'] for l in wizard._get_report_data()['lines']})

    def test_23_cancelled_orders_disappear(self):
        self.order_2._action_cancel()
        data = self._wizard()._get_report_data()
        self.assertNotIn(self.order_2.name, {l['order'] for l in data['lines']})

    def test_24_locked_orders_are_included(self):
        # 17.0: locking keeps state 'sale' and sets the 'locked' flag
        # (16.0 used the 'done' state instead).
        self.order_1.action_lock()
        self.assertTrue(self.order_1.locked)
        self.assertEqual(self.order_1.state, 'sale')
        data = self._wizard()._get_report_data()
        self.assertIn(self.order_1.name, {l['order'] for l in data['lines']})
        rows = self.env['so.product.profit.report'].search(
            [('order_id', '=', self.order_1.id)])
        self.assertTrue(rows and all(rows.mapped('locked')))

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    def test_30_browser_action(self):
        action = self._wizard(group_by='customer').action_view_report()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'so.product.profit.report')
        self.assertEqual(action['context'].get('group_by'), ['partner_id'])
        records = self.env['so.product.profit.report'].search(action['domain'])
        self.assertEqual(len(records), 3)
        self.assertAlmostEqual(sum(records.mapped('profit')), 130.0)

    def test_31_pdf_action_and_html(self):
        wizard = self._wizard()
        action = wizard.action_print_pdf()
        self.assertEqual(action['type'], 'ir.actions.report')
        self.assertEqual(action['report_name'], 'so_product_profit_report.report_so_product_profit')
        html = self._render_html(wizard)
        self.assertIn('Product Profit Report', html)
        self.assertIn('SOPR Product X', html)
        self.assertIn('dir="ltr"', html)
        self.assertIn('Grand Total', html)

    def test_32_xlsx_export(self):
        wizard = self._wizard(group_by='customer')
        action = wizard.action_export_xlsx()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertIn('field=xlsx_file', action['url'])
        self.assertTrue(wizard.xlsx_filename.endswith('.xlsx'))
        strings = self._xlsx_strings(wizard)
        for expected in ('Sales Product Profit Report', 'Grand Total', 'SOPR Product Y',
                         'Margin %'):
            self.assertIn(expected, strings)

    # ------------------------------------------------------------------
    # Multi-lingual generation (ar_001, RTL)
    # ------------------------------------------------------------------
    def _activate_arabic(self):
        self.env['res.lang']._activate_lang('ar_001')
        self.env['ir.module.module']._load_module_terms(
            ['so_product_profit_report'], ['ar_001'], overwrite=True)

    def _render_html(self, wizard, lang='en_US'):
        report = self.env.ref('so_product_profit_report.action_report_so_product_profit')
        html, _fmt = report.with_context(lang=lang)._render_qweb_html(
            report.report_name, wizard.ids)
        return html.decode() if isinstance(html, bytes) else html

    def _xlsx_strings(self, wizard):
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(wizard.xlsx_file))) as archive:
            parts = [archive.read(name).decode() for name in archive.namelist()
                     if name in ('xl/sharedStrings.xml', 'xl/workbook.xml',
                                 'xl/worksheets/sheet1.xml')]
        return '\n'.join(parts)

    def test_40_arabic_pdf_is_translated_and_rtl(self):
        self._activate_arabic()
        wizard = self._wizard(report_lang='ar_001', group_by='product')
        action = wizard.action_print_pdf()
        self.assertEqual(action['context'].get('lang'), 'ar_001')
        html = self._render_html(wizard, lang='ar_001')
        self.assertIn('dir="rtl"', html)
        self.assertIn('o_sopr_rtl', html)
        self.assertIn('تقرير ربحية المنتجات', html)   # Product Profit Report
        self.assertIn('الإجمالي العام', html)          # Grand Total
        self.assertIn('المجموع الفرعي', html)          # Subtotal
        self.assertNotIn('Grand Total', html)

    def test_41_arabic_xlsx_is_translated_and_rtl(self):
        self._activate_arabic()
        wizard = self._wizard(report_lang='ar_001')
        wizard.action_export_xlsx()
        strings = self._xlsx_strings(wizard)
        self.assertIn('rightToLeft="1"', strings)
        self.assertIn('تقرير ربحية منتجات المبيعات', strings)  # Sales Product Profit Report
        self.assertIn('الإجمالي العام', strings)
        self.assertIn('الهامش %', strings)

    def test_42_english_output_unaffected_by_arabic(self):
        self._activate_arabic()
        wizard = self._wizard(report_lang='en_US')
        html = self._render_html(wizard, lang='en_US')
        self.assertIn('Product Profit Report', html)
        self.assertNotIn('dir="rtl"', html)
        wizard.action_export_xlsx()
        self.assertNotIn('rightToLeft="1"', self._xlsx_strings(wizard))

    def test_43_report_values_are_language_aware(self):
        self._activate_arabic()
        data = self._wizard(report_lang='ar_001').with_context(lang='ar_001')._get_report_data()
        self.assertEqual(data['direction'], 'rtl')
        self.assertEqual(data['customers'], 'جميع العملاء')  # All Customers
        self.assertEqual(data['group_label'], 'بدون تجميع')  # No Grouping

    def test_44_mixed_direction_values_keep_their_order_in_rtl(self):
        self.assertEqual(text_direction('[E-COM07] Large Cabinet'), 'ltr')
        self.assertEqual(text_direction('طاولة [FURN_7800]'), 'rtl')
        self.assertEqual(text_direction(''), 'ltr')
        self._activate_arabic()
        html = self._render_html(self._wizard(report_lang='ar_001'), lang='ar_001')
        self.assertIn('<span dir="ltr">SOPR Product X</span>', html)
