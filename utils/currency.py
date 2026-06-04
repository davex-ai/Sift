"""
Currency utilities — NGN formatting, FX conversion.
"""

import re
from typing import Optional
from config import USD_TO_NGN, EUR_TO_NGN


def format_ngn(amount: Optional[float]) -> str:
    """₦450,000 format."""
    if amount is None:
        return "Price N/A"
    return f"₦{amount:,.0f}"


def to_ngn(amount: float, currency: str) -> float:
    """Convert foreign currency to NGN."""
    currency = currency.upper()
    rates = {
        "USD": USD_TO_NGN,
        "EUR": EUR_TO_NGN,
        "GBP": 2050.0,
        "NGN": 1.0,
    }
    rate = rates.get(currency, 1.0)
    return amount * rate


def parse_ngn(price_str: str) -> Optional[float]:
    """
    Parse NGN price from a raw string.
    Handles: '₦ 450,000', 'NGN450000', '450,000.50', '₦42k'
    """
    if not price_str:
        return None
    s = price_str.strip().upper()

    # Handle shorthand like "42K" or "₦1.5M"
    shorthand = re.search(r"([\d,]+\.?\d*)\s*([KM])", s)
    if shorthand:
        num = float(shorthand.group(1).replace(",", ""))
        suffix = shorthand.group(2)
        if suffix == "K":
            return num * 1_000
        if suffix == "M":
            return num * 1_000_000

    # Strip currency symbols, letters, spaces
    cleaned = re.sub(r"[^\d.]", "", s.replace(",", ""))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_budget(query: str) -> Optional[float]:
    """
    Parse budget ceiling from natural language.
    Examples:
      'laptop under 500k'    → 500_000
      'phone below ₦200000'  → 200_000
      'budget of 1.5m'       → 1_500_000
      'around ₦50,000'       → 50_000
    Returns None if no budget detected.
    """
    q = query.lower()

    patterns = [
        # 'under 500k', 'below ₦1.5m', 'less than 200000'
        r"(?:under|below|max|less\s+than|not\s+more\s+than|maximum)\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
        # 'budget of 50k', 'budget: ₦50,000'
        r"budget\s*(?:of|:)?\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
        # 'around ₦50,000', '₦50k range'
        r"(?:around|about|roughly|~)\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
        # bare '₦500,000'
        r"[₦#]\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
    ]

    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            amount_str = match.group(1).replace(",", "")
            mult = (match.group(2) or "").lower()
            try:
                amount = float(amount_str)
                if mult in ("k", "thousand"):
                    amount *= 1_000
                elif mult in ("m", "million"):
                    amount *= 1_000_000
                if amount > 0:
                    return amount
            except ValueError:
                continue

    return None
