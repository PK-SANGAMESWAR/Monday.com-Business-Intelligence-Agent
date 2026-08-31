"""Canonical field schema: monday.com column title -> field -> type -> flags.

One table per board (NFR-8), matching F03's `scripts/seeding/schema.py` in spirit but in
the opposite direction: that module encodes workbook values *into* monday.com columns,
this one decodes monday.com's `column_values` *into* canonical fields. See F04/F05 plan
section 3.2 for the full column-by-column rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DEALS_FIELDS",
    "DEALS_ITEM_NAME_FIELD",
    "DEALS_JUNK_MARKER_FIELD",
    "DEALS_JUNK_MARKER_VALUE",
    "FieldSpec",
    "FieldType",
    "WON_CONSISTENT_STAGES",
    "WORK_ORDERS_FIELDS",
    "WORK_ORDERS_ITEM_NAME_FIELD",
    "always_null_fields",
    "field_by_canonical",
    "summable_fields",
]

FieldType = Literal["date", "number", "text", "list"]


@dataclass(frozen=True)
class FieldSpec:
    canonical: str
    header: str
    field_type: FieldType
    always_null: bool = False
    #: False for the mixed-unit quantity fields CLAUDE.md says are not summable.
    summable: bool = True


DEALS_ITEM_NAME_FIELD = "deal_name"

DEALS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("owner_code", "Owner code", "text"),
    FieldSpec("client_code", "Client Code", "text"),
    FieldSpec("status", "Deal Status", "text"),
    FieldSpec("close_date_actual", "Close Date (A)", "date"),
    FieldSpec("closure_probability", "Closure Probability", "text"),
    FieldSpec("deal_value", "Masked Deal value", "number"),
    FieldSpec("tentative_close_date", "Tentative Close Date", "date"),
    FieldSpec("stage", "Deal Stage", "text"),
    FieldSpec("product_type", "Product deal", "text"),
    FieldSpec("sector", "Sector/service", "text"),
    FieldSpec("created_date", "Created Date", "date"),
    FieldSpec("source_row", "Source Row", "text"),
)

DEALS_JUNK_MARKER_FIELD = "status"
DEALS_JUNK_MARKER_VALUE = "Deal Status"

#: Stages measured in DATA_PROFILE.md as self-consistent with `status == "Won"`.
#: Everything else (overwhelmingly `A. Lead Generated`) is a real contradiction
#: CLAUDE.md requires surfacing, not hiding (decision D-6).
WON_CONSISTENT_STAGES = frozenset(
    {
        "G. Project Won",
        "H. Work Order Received",
        "Project Completed",
        "J. Invoice sent",
        "K. Amount Accrued",
    }
)

WORK_ORDERS_ITEM_NAME_FIELD = "serial_no"

WORK_ORDERS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("deal_name", "Deal name masked", "text"),
    FieldSpec("customer_code", "Customer Name Code", "text"),
    FieldSpec("nature_of_work", "Nature of Work", "text"),
    FieldSpec("last_recurring_month", "Last executed month of recurring project", "text"),
    FieldSpec("execution_status", "Execution Status", "text"),
    FieldSpec("data_delivery_date", "Data Delivery Date", "date"),
    FieldSpec("po_date", "Date of PO/LOI", "date"),
    FieldSpec("document_type", "Document Type", "text"),
    FieldSpec("start_date", "Probable Start Date", "date"),
    FieldSpec("end_date", "Probable End Date", "date"),
    FieldSpec("owner_code", "BD/KAM Personnel code", "text"),
    FieldSpec("sector", "Sector", "text"),
    FieldSpec("work_types", "Type of Work", "list"),
    FieldSpec(
        "skylark_platform",
        "Is any Skylark software platform part of the client deliverables in this deal?",
        "text",
    ),
    FieldSpec("last_invoice_date", "Last invoice date", "date"),
    FieldSpec("last_invoice_no", "latest invoice no.", "text"),
    FieldSpec("amount_excl_gst", "Amount in Rupees (Excl of GST) (Masked)", "number"),
    FieldSpec("amount_incl_gst", "Amount in Rupees (Incl of GST) (Masked)", "number"),
    FieldSpec(
        "billed_excl_gst", "Billed Value in Rupees (Excl of GST.) (Masked)", "number"
    ),
    FieldSpec(
        "billed_incl_gst", "Billed Value in Rupees (Incl of GST.) (Masked)", "number"
    ),
    FieldSpec(
        "collected_incl_gst",
        "Collected Amount in Rupees (Incl of GST.) (Masked)",
        "number",
    ),
    FieldSpec(
        "to_bill_excl_gst", "Amount to be billed in Rs. (Exl. of GST) (Masked)", "number"
    ),
    FieldSpec(
        "to_bill_incl_gst", "Amount to be billed in Rs. (Incl. of GST) (Masked)", "number"
    ),
    FieldSpec("receivable", "Amount Receivable (Masked)", "number"),
    FieldSpec("ar_priority", "AR Priority account", "text"),
    FieldSpec("qty_ops_raw", "Quantity by Ops", "text", summable=False),
    FieldSpec("qty_po_raw", "Quantities as per PO", "text", summable=False),
    FieldSpec("qty_billed_raw", "Quantity billed (till date)", "text", summable=False),
    FieldSpec("qty_balance_raw", "Balance in quantity", "text", summable=False),
    FieldSpec("invoice_status", "Invoice Status", "text"),
    FieldSpec("expected_billing_month", "Expected Billing Month", "text", always_null=True),
    FieldSpec("billing_month_actual", "Actual Billing Month", "text"),
    FieldSpec(
        "actual_collection_month", "Actual Collection Month", "text", always_null=True
    ),
    FieldSpec("wo_status_billed", "WO Status (billed)", "text"),
    FieldSpec("collection_status", "Collection status", "text", always_null=True),
    FieldSpec("collection_date", "Collection Date", "date", always_null=True),
    FieldSpec("billing_status", "Billing Status", "text"),
    FieldSpec("source_row", "Source Row", "text"),
)


def field_by_canonical(fields: tuple[FieldSpec, ...]) -> dict[str, FieldSpec]:
    return {spec.canonical: spec for spec in fields}


def always_null_fields(fields: tuple[FieldSpec, ...]) -> list[str]:
    return [spec.canonical for spec in fields if spec.always_null]


def summable_fields(fields: tuple[FieldSpec, ...]) -> set[str]:
    return {spec.canonical for spec in fields if spec.field_type == "number" and spec.summable}
