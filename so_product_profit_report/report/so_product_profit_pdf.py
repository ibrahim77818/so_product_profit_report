# -*- coding: utf-8 -*-
from odoo import api, models


class ReportSoProductProfit(models.AbstractModel):
    _name = 'report.so_product_profit_report.report_so_product_profit'
    _description = 'Sales Product Profit PDF Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        wizards = self.env['so.product.profit.wizard'].browse(docids)
        reports = {wizard.id: wizard._get_report_data(raise_if_empty=True)
                   for wizard in wizards}
        return {
            'doc_ids': docids,
            'doc_model': 'so.product.profit.wizard',
            'docs': wizards,
            'reports': reports,
            'company': self.env.company,
        }
