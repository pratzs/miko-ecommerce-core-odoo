# -*- coding: utf-8 -*-
{
    'name': 'E-Commerce Connector Engine (Miko)',
    'version': '17.0.1.0.0',
    'summary': 'Sync engine for Odoo e-commerce connectors: identity mapping so a re-run never duplicates an order, plus a retryable job queue',
    'description': """
The plumbing every store connector needs and nobody wants to write twice:
identity mapping so a re-run never duplicates an order, a job queue that keeps
failures instead of losing them, and a log you can actually answer questions from.

Install a platform connector alongside it. On its own this does nothing visible,
which is deliberate: it is the engine, not the car.
""",
    'author': 'Tripster Developers',
    'website': 'https://tripsterdevelopers.com/odoo/',
    'category': 'eCommerce',
    'license': 'LGPL-3',
    'depends': ['sale_management', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/miko_ecommerce_core_views.xml',
    ],
    'images': ['images/banner.gif', 'images/banner.png'],
    'application': False,
    'installable': True,
    'support': 'support@tripsterdevelopers.com',
}
