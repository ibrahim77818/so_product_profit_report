# -*- coding: utf-8 -*-
from odoo import api, fields, models, tools
from odoo.tools import SQL


class SoProductProfitReport(models.Model):
    """Read-only SQL view: one row per confirmed sales order product line.

    All amounts are converted to the company currency with the order's
    stored ``currency_rate`` (same approach as the standard ``sale.report``),
    so rows of one company can be summed safely.
    """
    _name = 'so.product.profit.report'
    _description = 'Sales Product Profit Analysis'
    _auto = False
    _rec_name = 'order_reference'
    _order = 'date_order desc, order_reference desc, id desc'

    order_id = fields.Many2one('sale.order', string='Sales Order', readonly=True)
    order_reference = fields.Char(string='Order Reference', readonly=True)
    date_order = fields.Datetime(string='Order Date', readonly=True)
    state = fields.Selection([
        ('sale', 'Sales Order'),
    ], string='Status', readonly=True)
    locked = fields.Boolean(string='Locked', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    commercial_partner_id = fields.Many2one(
        'res.partner', string='Customer Entity', readonly=True)
    user_id = fields.Many2one('res.users', string='Salesperson', readonly=True)
    team_id = fields.Many2one('crm.team', string='Sales Team', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    product_tmpl_id = fields.Many2one(
        'product.template', string='Product Template', readonly=True)
    categ_id = fields.Many2one(
        'product.category', string='Product Category', readonly=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    quantity = fields.Float(
        string='Quantity', digits='Product Unit of Measure', readonly=True)
    unit_cost = fields.Monetary(string='Cost Price', readonly=True)
    unit_price = fields.Monetary(string='Sale Price', readonly=True)
    total_cost = fields.Monetary(string='Total Cost', readonly=True)
    total_sales = fields.Monetary(string='Total Sales', readonly=True)
    profit = fields.Monetary(string='Profit', readonly=True)
    margin_percent = fields.Float(
        string='Margin (%)', digits=(16, 2), readonly=True, aggregator='avg',
        help='Profit / Total Sales x 100. Group totals are weighted by sales.')

    # ------------------------------------------------------------------
    # SQL view
    # ------------------------------------------------------------------
    @api.model
    def _rate_sql(self):
        return "(CASE COALESCE(s.currency_rate, 0) WHEN 0 THEN 1.0 ELSE s.currency_rate END)"

    @api.model
    def _line_uom_column(self):
        # 19.0 renamed sale.order.line.product_uom -> product_uom_id.
        line_fields = self.env['sale.order.line']._fields
        return 'product_uom_id' if 'product_uom_id' in line_fields else 'product_uom'

    @api.model
    def _select_sql(self):
        rate = self._rate_sql()
        uom = self._line_uom_column()
        cost = "(COALESCE(l.purchase_price, 0) * l.product_uom_qty)"
        return f"""
            l.id AS id,
            l.order_id AS order_id,
            s.name AS order_reference,
            s.date_order AS date_order,
            s.state AS state,
            COALESCE(s.locked, FALSE) AS locked,
            s.partner_id AS partner_id,
            partner.commercial_partner_id AS commercial_partner_id,
            s.user_id AS user_id,
            s.team_id AS team_id,
            s.company_id AS company_id,
            company.currency_id AS currency_id,
            l.product_id AS product_id,
            pp.product_tmpl_id AS product_tmpl_id,
            pt.categ_id AS categ_id,
            l.{uom} AS product_uom_id,
            l.product_uom_qty AS quantity,
            COALESCE(l.purchase_price, 0) / {rate} AS unit_cost,
            CASE WHEN l.product_uom_qty <> 0
                 THEN l.price_subtotal / l.product_uom_qty / {rate}
                 ELSE 0 END AS unit_price,
            {cost} / {rate} AS total_cost,
            l.price_subtotal / {rate} AS total_sales,
            (l.price_subtotal - {cost}) / {rate} AS profit,
            CASE WHEN l.price_subtotal <> 0
                 THEN (l.price_subtotal - {cost}) / l.price_subtotal * 100.0
                 ELSE 0 END AS margin_percent
        """

    @api.model
    def _from_sql(self):
        return """
            sale_order_line l
            JOIN sale_order s ON s.id = l.order_id
            JOIN res_company company ON company.id = s.company_id
            JOIN res_partner partner ON partner.id = s.partner_id
            JOIN product_product pp ON pp.id = l.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
        """

    @api.model
    def _where_sql(self):
        return """
            s.state = 'sale'
            AND l.display_type IS NULL
            AND COALESCE(l.is_downpayment, FALSE) = FALSE
        """

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                SELECT {self._select_sql()}
                FROM {self._from_sql()}
                WHERE {self._where_sql()}
            )
        """)
        # Index used by the date/company/state filter of the view.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS sale_order_sopr_state_date_company_idx
            ON sale_order (state, date_order, company_id)
        """)

    # ------------------------------------------------------------------
    # Aggregation: ratios must be recomputed from sums, never averaged.
    # 19.0: the web client groups through formatted_read_group/_read_group
    # (read_group is no longer called by list/pivot/graph), so the ratio is
    # computed in SQL at the single point every grouping path goes through.
    # ------------------------------------------------------------------
    _RATIO_FIELDS = {
        # field: (numerator field, denominator field, factor)
        'margin_percent': ('profit', 'total_sales', 100.0),
        'unit_cost': ('total_cost', 'quantity', 1.0),
        'unit_price': ('total_sales', 'quantity', 1.0),
    }

    def _read_group_select(self, aggregate_spec, query):
        fname, __, func = aggregate_spec.partition(':')
        if fname in self._RATIO_FIELDS and func in ('avg', 'sum'):
            num_name, den_name, factor = self._RATIO_FIELDS[fname]
            num = self._field_to_sql(self._table, num_name, query)
            den = self._field_to_sql(self._table, den_name, query)
            return SQL(
                "(CASE WHEN SUM(%s) <> 0 THEN SUM(%s) / SUM(%s) * %s ELSE 0 END)",
                den, num, den, factor,
            )
        return super()._read_group_select(aggregate_spec, query)

    @api.model
    def _compute_margin(self, profit, sales):
        return (profit / sales * 100.0) if sales else 0.0
