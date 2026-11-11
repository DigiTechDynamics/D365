"""Schema the model fills in when it reads an invoice. Each header field carries its own confidence."""
from typing import List, Optional

from pydantic import BaseModel


class TextField(BaseModel):
    value: Optional[str]
    confidence: float


class AmountField(BaseModel):
    value: Optional[float]
    confidence: float


class InvoiceLine(BaseModel):
    item_code: Optional[str]
    description: str
    quantity: Optional[float]
    unit_price: Optional[float]
    amount: float
    is_charge: bool
    confidence: float


class ExtractedInvoice(BaseModel):
    vendor_name: TextField
    vendor_tax_id: TextField
    invoice_number: TextField
    invoice_date: TextField
    due_date: TextField
    currency: TextField
    po_number: TextField
    project_reference: TextField
    subtotal: AmountField
    tax_amount: AmountField
    total: AmountField
    lines: List[InvoiceLine]
    notes: Optional[str]


HEADER_FIELDS = [
    "vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date",
    "currency", "po_number", "project_reference", "subtotal", "tax_amount", "total",
]
