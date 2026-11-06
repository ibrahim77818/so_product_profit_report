# -*- coding: utf-8 -*-
import base64
import io
import re
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import xlsxwriter


# First strong character decides the text direction (Hebrew/Arabic blocks -> RTL).
_RTL_CHARS = re.compile('[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]')
_STRONG_CHARS = re.compile('[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]')


def text_direction(text):
    """'rtl' or 'ltr' for one value, so mixed data such as "[E-COM07] Desk"
    keeps its bracket order inside an RTL report (wkhtmltopdf has no dir=auto)."""
    match = _STRONG_CHARS.search(text or '')
    return 'rtl' if match and _RTL_CHARS.match(match.group()) else 'ltr'


GROUP_FIELDS = {
    'customer': 'partner_id',
    'product': 'product_id',
    'order': 'order_id',
}

REPORT_COLUMNS = (
    'id', 'order_id', 'order_reference', 'date_order', 'partner_id',
    'product_id', 'product_uom_id', 'quantity', 'unit_cost', 'unit_price',
    'total_cost', 'total_sales', 'profit', 'margin_percent', 'company_id',
)


class SoProductProfitWizard(models.TransientModel):
    _name = 'so.product.profit.wizard'
    _description = 'Sales Product Profit Report Wizard'

    @api.model
    def _default_date_from(self):
        return fields.Date.context_today(self).replace(day=1)

    @api.model
    def _get_lang_selection(self):
        return self.env['res.lang'].get_installed()

    @api.model
    def _default_report_lang(self):
        installed = dict(self.env['res.lang'].get_installed())
        lang = self.env.lang or self.env.user.lang
        return lang if lang in installed else (next(iter(installed), 'en_US'))

    date_from = fields.Date(
        string='Start Date', required=True, default=_default_date_from)
    date_to = fields.Date(
        string='End Date', required=True,
        default=lambda self: fields.Date.context_today(self))
    partner_ids = fields.Many2many(
        'res.partner', string='Customers',
        help='Leave empty for all customers. Orders of child contacts are included.')
    company_ids = fields.Many2many(
        'res.company', string='Companies', required=True,
        default=lambda self: self.env.companies)
    group_by = fields.Selection([
        ('none', 'No Grouping'),
        ('customer', 'Customer'),
        ('product', 'Product'),
        ('order', 'Sales Order'),
    ], string='Group By', required=True, default='none')
    report_lang = fields.Selection(
        selection='_get_lang_selection', string='Report Language',
        required=True, default=_default_report_lang)
    xlsx_file = fields.Binary(string='Excel File', attachment=False, readonly=True)
    xlsx_filename = fields.Char(string='Excel File Name', readonly=True)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from and wizard.date_to and wizard.date_from > wizard.date_to:
                raise ValidationError(_("The start date must be on or before the end date."))

    def _check_parameters(self):
        self.ensure_one()
        if not self.date_from or not self.date_to:
            raise UserError(_("Please set both the start date and the end date."))
        if self.date_from > self.date_to:
            raise ValidationError(_("The start date must be on or before the end date."))
        if not self.company_ids:
            raise UserError(_("Please select at least one company."))
        if len(self.company_ids.currency_id) > 1:
            raise UserError(_(
                "The selected companies use different currencies (%s). "
                "Please select companies that share one currency.",
                ", ".join(self.company_ids.currency_id.mapped('name'))))

    # ------------------------------------------------------------------
    # Domain / data
    # ------------------------------------------------------------------
    def _get_tz(self):
        tz_name = self.env.context.get('tz') or self.env.user.tz or 'UTC'
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    def _date_to_utc(self, date_value, end_of_day=False):
        """Local day boundary -> naive UTC datetime (end bound is exclusive)."""
        day = date_value + timedelta(days=1) if end_of_day else date_value
        local_dt = self._get_tz().localize(datetime.combine(day, time.min))
        return local_dt.astimezone(pytz.utc).replace(tzinfo=None)

    def _get_report_domain(self):
        self.ensure_one()
        domain = [
            ('date_order', '>=', fields.Datetime.to_string(self._date_to_utc(self.date_from))),
            ('date_order', '<', fields.Datetime.to_string(
                self._date_to_utc(self.date_to, end_of_day=True))),
            ('company_id', 'in', self.company_ids.ids),
        ]
        if self.partner_ids:
            domain.append(('partner_id', 'child_of', self.partner_ids.ids))
        return domain

    def _fetch_rows(self):
        """Single SQL round-trip that still honours access rights & record rules."""
        Report = self.env['so.product.profit.report']
        for model in ('sale.order', 'sale.order.line'):
            self.env[model].flush_model()
        query = Report._search(
            self._get_report_domain(),
            order='date_order asc, order_reference asc, id asc')
        table = Report._table
        query_str, params = query.select(
            *('"%s"."%s"' % (table, col) for col in REPORT_COLUMNS))
        self.env.cr.execute(query_str, params)
        return self.env.cr.dictfetchall()

    @api.model
    def _format_margin(self, margin):
        # Formatted in Python: QWeb collapses '%%' inside t-esc expressions.
        return '{:.2f} %'.format(margin or 0.0)

    @api.model
    def _compute_totals(self, lines, with_qty=False):
        cost = sum(line['cost'] for line in lines)
        sales = sum(line['sales'] for line in lines)
        profit = sum(line['profit'] for line in lines)
        margin = self.env['so.product.profit.report']._compute_margin(profit, sales)
        return {
            'qty': sum(line['qty'] for line in lines) if with_qty else False,
            'cost': cost,
            'sales': sales,
            'profit': profit,
            'margin': margin,
            'margin_str': self._format_margin(margin),
            'count': len(lines),
        }

    def _get_report_data(self, raise_if_empty=False):
        """Build the structure shared by the PDF and XLSX outputs."""
        self.ensure_one()
        self._check_parameters()
        rows = self._fetch_rows()
        if raise_if_empty and not rows:
            raise UserError(_("No confirmed sales order lines match the selected filters."))

        partners = {p.id: p.display_name for p in self.env['res.partner'].browse(
            {r['partner_id'] for r in rows})}
        products = {p.id: p.display_name for p in self.env['product.product'].browse(
            {r['product_id'] for r in rows})}
        uoms = {u.id: u.name for u in self.env['uom.uom'].browse(
            {r['product_uom_id'] for r in rows if r['product_uom_id']})}

        lines = []
        for row in rows:
            local_dt = fields.Datetime.context_timestamp(self, row['date_order'])
            lines.append({
                'id': row['id'],
                'date': local_dt.date(),
                'order': row['order_reference'],
                'order_id': row['order_id'],
                'partner_id': row['partner_id'],
                'product_id': row['product_id'],
                'customer': partners.get(row['partner_id'], ''),
                'product': products.get(row['product_id'], ''),
                'uom': uoms.get(row['product_uom_id'], ''),
                'qty': row['quantity'] or 0.0,
                'unit_cost': row['unit_cost'] or 0.0,
                'unit_price': row['unit_price'] or 0.0,
                'cost': row['total_cost'] or 0.0,
                'sales': row['total_sales'] or 0.0,
                'profit': row['profit'] or 0.0,
                'margin': row['margin_percent'] or 0.0,
                'margin_str': self._format_margin(row['margin_percent'] or 0.0),
            })
            lines[-1]['customer_dir'] = text_direction(lines[-1]['customer'])
            lines[-1]['product_dir'] = text_direction(lines[-1]['product'])

        groups = []
        if self.group_by == 'none':
            groups.append({'label': False, 'label_dir': 'ltr', 'lines': lines,
                           'totals': self._compute_totals(lines)})
        else:
            key_field = GROUP_FIELDS[self.group_by]
            label_key = {'customer': 'customer', 'product': 'product', 'order': 'order'}[self.group_by]
            buckets = {}
            for line in lines:
                buckets.setdefault(line[key_field], []).append(line)
            for group_lines in buckets.values():
                groups.append({
                    'label': group_lines[0][label_key],
                    'label_dir': text_direction(group_lines[0][label_key]),
                    'lines': group_lines,
                    'totals': self._compute_totals(
                        group_lines, with_qty=self.group_by == 'product'),
                })
            groups.sort(key=lambda g: (g['label'] or '').lower())

        lang = self.env['res.lang']._lang_get(self.env.lang or 'en_US')
        companies = ', '.join(self.company_ids.mapped('name'))
        customers = ', '.join(self.partner_ids.mapped('display_name')) or _("All Customers")
        group_labels = dict(self._fields['group_by']._description_selection(self.env))
        return {
            'lines': lines,
            'groups': groups,
            'grouped': self.group_by != 'none',
            'totals': self._compute_totals(lines),
            'currency': self.company_ids.currency_id[:1] or self.env.company.currency_id,
            'companies': companies,
            'companies_dir': text_direction(companies),
            'customers': customers,
            'customers_dir': text_direction(customers),
            'group_label': group_labels.get(self.group_by, ''),
            'date_from': self.date_from,
            'date_to': self.date_to,
            'direction': (lang and lang.direction) or 'ltr',
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_view_report(self):
        self.ensure_one()
        self._check_parameters()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'so_product_profit_report.action_so_product_profit_report')
        action['domain'] = self._get_report_domain()
        context = {}
        if self.group_by != 'none':
            context.update({
                'group_by': [GROUP_FIELDS[self.group_by]],
                'pivot_row_groupby': [GROUP_FIELDS[self.group_by]],
            })
        action['context'] = context
        action['name'] = _(
            "Product Profit: %(from)s - %(to)s",
            **{'from': fields.Date.to_string(self.date_from),
               'to': fields.Date.to_string(self.date_to)})
        return action

    def action_print_pdf(self):
        self.ensure_one()
        wizard = self.with_context(lang=self.report_lang)
        wizard._get_report_data(raise_if_empty=True)
        return self.env.ref(
            'so_product_profit_report.action_report_so_product_profit'
        ).with_context(lang=self.report_lang).report_action(self, config=False)

    def action_export_xlsx(self):
        self.ensure_one()
        wizard = self.with_context(lang=self.report_lang)
        content = wizard._generate_xlsx()
        self.write({
            'xlsx_file': base64.b64encode(content),
            'xlsx_filename': 'SO_Product_Profit_%s_%s.xlsx' % (
                fields.Date.to_string(self.date_from), fields.Date.to_string(self.date_to)),
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content?model=%s&id=%s&field=xlsx_file'
                   '&filename_field=xlsx_filename&download=true' % (self._name, self.id),
            'target': 'self',
        }

    # ------------------------------------------------------------------
    # XLSX
    # ------------------------------------------------------------------
    def _generate_xlsx(self):
        self.ensure_one()
        data = self._get_report_data(raise_if_empty=True)
        currency = data['currency']
        rtl = data['direction'] == 'rtl'
        lang = self.env['res.lang']._lang_get(self.env.lang or 'en_US')
        date_fmt = (lang.date_format or '%m/%d/%Y').replace('%d', 'dd').replace(
            '%m', 'mm').replace('%Y', 'yyyy').replace('%y', 'yy').replace('%b', 'mmm')
        money_fmt = '#,##0.%s' % ('0' * currency.decimal_places) if currency.decimal_places else '#,##0'

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet(_("Product Profit")[:31])
        if rtl:
            sheet.right_to_left()
        font = 'Arial' if rtl else 'Calibri'
        align = 'right' if rtl else 'left'

        def fmt(**kw):
            base = {'font_name': font, 'font_size': 10, 'valign': 'vcenter'}
            base.update(kw)
            return workbook.add_format(base)

        f_title = fmt(bold=True, font_size=16, font_color='#1F3A5F')
        f_meta_label = fmt(bold=True, font_color='#5B6B7F', align=align)
        f_meta = fmt(align=align)
        f_head = fmt(bold=True, font_color='#FFFFFF', bg_color='#1F3A5F', border=1,
                     border_color='#1F3A5F', align='center', text_wrap=True)
        f_text = fmt(border=1, border_color='#D9E1EA', align=align)
        f_date = fmt(border=1, border_color='#D9E1EA', num_format=date_fmt, align='center')
        f_qty = fmt(border=1, border_color='#D9E1EA', num_format='#,##0.00')
        f_money = fmt(border=1, border_color='#D9E1EA', num_format=money_fmt)
        f_money_neg = fmt(border=1, border_color='#D9E1EA', num_format=money_fmt,
                          font_color='#C0392B')
        f_pct = fmt(border=1, border_color='#D9E1EA', num_format='0.00%')
        f_group = fmt(bold=True, bg_color='#E8EEF5', border=1, border_color='#D9E1EA',
                      align=align)
        f_sub_label = fmt(bold=True, bg_color='#F4F6F9', border=1, border_color='#D9E1EA',
                          align=align)
        f_sub_money = fmt(bold=True, bg_color='#F4F6F9', border=1, border_color='#D9E1EA',
                          num_format=money_fmt)
        f_sub_qty = fmt(bold=True, bg_color='#F4F6F9', border=1, border_color='#D9E1EA',
                        num_format='#,##0.00')
        f_sub_pct = fmt(bold=True, bg_color='#F4F6F9', border=1, border_color='#D9E1EA',
                        num_format='0.00%')
        f_tot_label = fmt(bold=True, font_color='#FFFFFF', bg_color='#2E7D5B', border=1,
                          align=align)
        f_tot_money = fmt(bold=True, font_color='#FFFFFF', bg_color='#2E7D5B', border=1,
                          num_format=money_fmt)
        f_tot_pct = fmt(bold=True, font_color='#FFFFFF', bg_color='#2E7D5B', border=1,
                        num_format='0.00%')

        headers = [
            (_("Order Date"), 12), (_("Order"), 14), (_("Customer"), 28),
            (_("Product"), 34), (_("Quantity"), 11), (_("Unit"), 9),
            (_("Cost Price"), 13), (_("Sale Price"), 13), (_("Total Cost"), 15),
            (_("Total Sales"), 15), (_("Profit"), 15), (_("Margin %"), 10),
        ]
        last_col = len(headers) - 1
        for col, (_label, width) in enumerate(headers):
            sheet.set_column(col, col, width)

        sheet.set_row(0, 26)
        sheet.merge_range(0, 0, 0, last_col, _("Sales Product Profit Report"), f_title)
        meta = [
            (_("Period"), '%s  →  %s' % (
                self.date_from.strftime(lang.date_format or '%m/%d/%Y'),
                self.date_to.strftime(lang.date_format or '%m/%d/%Y'))),
            (_("Companies"), data['companies']),
            (_("Customers"), data['customers']),
            (_("Group By"), data['group_label']),
            (_("Currency"), currency.name),
        ]
        row = 2
        for label, value in meta:
            sheet.write(row, 0, label, f_meta_label)
            sheet.merge_range(row, 1, row, last_col, value, f_meta)
            row += 1
        row += 1
        header_row = row
        sheet.set_row(header_row, 30)
        for col, (label, _width) in enumerate(headers):
            sheet.write(header_row, col, label, f_head)
        row += 1

        def write_totals(r, label, totals, f_label, f_money_, f_pct_, f_qty_=None):
            sheet.merge_range(r, 0, r, 3, label, f_label)
            if totals['qty'] is not False and f_qty_:
                sheet.write_number(r, 4, totals['qty'], f_qty_)
            else:
                sheet.write_blank(r, 4, None, f_label)
            for col in (5, 6, 7):
                sheet.write_blank(r, col, None, f_label)
            sheet.write_number(r, 8, totals['cost'], f_money_)
            sheet.write_number(r, 9, totals['sales'], f_money_)
            sheet.write_number(r, 10, totals['profit'], f_money_)
            sheet.write_number(r, 11, totals['margin'] / 100.0, f_pct_)

        for group in data['groups']:
            if data['grouped']:
                sheet.merge_range(row, 0, row, last_col, group['label'] or '', f_group)
                row += 1
            for line in group['lines']:
                sheet.write_datetime(row, 0, datetime.combine(line['date'], time.min), f_date)
                sheet.write(row, 1, line['order'] or '', f_text)
                sheet.write(row, 2, line['customer'], f_text)
                sheet.write(row, 3, line['product'], f_text)
                sheet.write_number(row, 4, line['qty'], f_qty)
                sheet.write(row, 5, line['uom'], f_text)
                sheet.write_number(row, 6, line['unit_cost'], f_money)
                sheet.write_number(row, 7, line['unit_price'], f_money)
                sheet.write_number(row, 8, line['cost'], f_money)
                sheet.write_number(row, 9, line['sales'], f_money)
                sheet.write_number(row, 10, line['profit'],
                                   f_money_neg if line['profit'] < 0 else f_money)
                sheet.write_number(row, 11, line['margin'] / 100.0, f_pct)
                row += 1
            if data['grouped']:
                write_totals(row, _("Subtotal: %s", group['label'] or ''), group['totals'],
                             f_sub_label, f_sub_money, f_sub_pct, f_sub_qty)
                row += 1

        write_totals(row, _("Grand Total"), data['totals'],
                     f_tot_label, f_tot_money, f_tot_pct)

        sheet.freeze_panes(header_row + 1, 0)
        sheet.autofilter(header_row, 0, max(row - 1, header_row), last_col)
        sheet.set_landscape()
        sheet.set_paper(9)  # A4
        sheet.fit_to_pages(1, 0)
        sheet.repeat_rows(header_row)
        sheet.set_footer('&C&P / &N')
        workbook.close()
        return output.getvalue()
