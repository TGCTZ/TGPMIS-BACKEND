"""Report sections and the existing permissions protecting their source data."""

from zoneinfo import ZoneInfo

REPORT_TIMEZONE = ZoneInfo("Africa/Dar_es_Salaam")
EXPORT_ROW_LIMIT = 10_000
REPORT_TITLES = {"financial": "Financial reports", "operational": "Operational reports"}
REPORT_GATES = {
    "financial": "core.report_financial",
    "operational": "core.report_operational",
}

SECTIONS = {
    "billing": {
        "report": "financial",
        "title": "Billing summary",
        "description": (
            "Bills issued in the period. Statuses reflect their current state."
        ),
        "permissions": ("billing.view_bill",),
    },
    "collections": {
        "report": "financial",
        "title": "Payments received",
        "description": "Processed payments by transaction date; exceptions are separate.",
        "permissions": ("billing.view_payment",),
    },
    "exceptions": {
        "report": "financial",
        "title": "Unprocessed payments",
        "description": (
            "Unprocessed notifications by transaction date, excluded from payments received."
        ),
        "permissions": ("billing.view_payment",),
    },
    "outstanding": {
        "report": "financial",
        "title": "Outstanding bills",
        "description": (
            "Bills issued in the period with balances still owed today. "
            "Cancelled bills are excluded."
        ),
        "permissions": ("billing.view_bill", "billing.view_payment"),
    },
    "orders": {
        "report": "operational",
        "title": "Orders received",
        "description": "Orders by received date. A matching order is counted once.",
        "permissions": ("orders.view_order",),
    },
    "stones": {
        "report": "operational",
        "title": "Stones registered",
        "description": (
            "Registered stone records by creation date, rather than submitted quantities."
        ),
        "permissions": ("orders.view_stone",),
    },
    "certificates": {
        "report": "operational",
        "title": "Certificates issued",
        "description": (
            "Certificates by issue date, including those subsequently revoked."
        ),
        "permissions": ("certificates.view_certificate",),
    },
}


def allowed_sections(user, report: str) -> list[str]:
    """Require report access and every source permission for each section."""
    if not user.has_perms(("core.module_reports", REPORT_GATES[report])):
        return []
    return [
        key
        for key, section in SECTIONS.items()
        if section["report"] == report and user.has_perms(section["permissions"])
    ]


def section_info(key: str) -> dict:
    """Expose descriptions without leaking permission implementation details."""
    section = SECTIONS[key]
    return {"key": key, "title": section["title"], "description": section["description"]}
