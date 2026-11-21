"""Beem SMS delivery shared by workflow notifications."""

import base64
import logging
import re

import requests

from django.conf import settings

logger = logging.getLogger(__name__)


def normalize_msisdn(phone: str | None) -> str:
    """Return a valid Tanzania MSISDN, or an empty string for unusable input."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = f"255{digits[1:]}"
    elif len(digits) == 9:
        digits = f"255{digits}"
    if len(digits) != 12 or not digits.startswith("255"):
        return ""
    return digits


def send_sms(phone_numbers, message: str, *, context: str) -> bool:
    """Send one Beem message to the unique, valid Tanzanian numbers provided."""
    if getattr(settings, "GEPG_SIMULATE", False):
        return False

    api_key = getattr(settings, "BEEM_AFRICA_API_KEY", "")
    secret = getattr(settings, "BEEM_AFRICA_SECRET_KEY", "")
    numbers = list(
        dict.fromkeys(
            number
            for number in (normalize_msisdn(phone) for phone in phone_numbers)
            if number
        )
    )
    if not api_key or not secret or not numbers:
        return False

    payload = {
        "source_addr": "TGC-INFO",
        "schedule_time": "",
        "encoding": 0,
        "message": message,
        "recipients": [
            {"recipient_id": index, "dest_addr": number}
            for index, number in enumerate(numbers, start=1)
        ],
    }
    try:
        response = requests.post(
            "https://apisms.beem.africa/v1/send",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic "
                + base64.b64encode(f"{api_key}:{secret}".encode()).decode(),
            },
            timeout=30,
        )
    except requests.RequestException:
        logger.exception("Beem SMS request failed for %s", context)
        return False

    if response.status_code != 200:
        logger.warning(
            "Beem SMS failed for %s with HTTP %s", context, response.status_code
        )
        return False
    return True
