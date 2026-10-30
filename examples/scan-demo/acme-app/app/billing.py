"""Billing: charges with Stripe, invoices from the internal billing service.

Docs: https://docs.acme.com/billing
"""
import os

import requests
import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
BILLING_URL = os.environ.get("BILLING_URL", "https://billing.internal.acme.com")


def charge(customer_id: str, cents: int):
    return stripe.PaymentIntent.create(customer=customer_id, amount=cents, currency="usd")


def list_invoices():
    r = requests.get("https://billing.internal.acme.com/v1/invoices", timeout=10)
    return r.json()


def get_invoice(invoice_id: str):
    return requests.get(f"https://billing.internal.acme.com/v1/invoices/{invoice_id}", timeout=10).json()


def create_invoice(data: dict):
    return requests.post("https://billing.internal.acme.com/v1/invoices", json=data, timeout=10).json()


def health():
    # local dev only
    return requests.get("http://localhost:8080/health").ok
