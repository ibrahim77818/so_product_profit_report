# -*- coding: utf-8 -*-
{
    'name': 'SO Product Profit Report',
    'version': '17.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Product profit & margin report on confirmed sales orders: '
               'browser (list/pivot), modern PDF and XLSX, Arabic RTL.',
    'description': """
SO Product Profit Report
========================
* Wizard under Sales > Reporting (does not override any standard report).
* Filters: date range, customers (incl. their contacts), companies,
  grouping (none / customer / product / order) and report language.
* Outputs: interactive list & pivot view, landscape PDF with repeated table
  header and no header overlap, XLSX export with subtotals.
* Profit = Sales (untaxed, after discount) - Cost (sale_margin line cost),
  Margin % = Profit / Sales x 100, all amounts in company currency.
* Full Arabic (ar_001) translation and RTL layout in PDF and XLSX.
    """,
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'depends': ['sale_management', 'sale_margin'],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/ir.model.access.csv',
        'security/so_product_profit_security.xml',
        'report/so_product_profit_paperformat.xml',
        'report/so_product_profit_report_templates.xml',
        'views/so_product_profit_report_views.xml',
        'wizard/so_product_profit_wizard_views.xml',
        'views/so_product_profit_menus.xml',
    ],
    'assets': {
        'web.report_assets_common': [
            'so_product_profit_report/static/src/scss/so_product_profit_report.scss',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
