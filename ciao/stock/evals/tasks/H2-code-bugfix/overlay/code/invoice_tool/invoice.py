"""Invoice totals for the small invoice tool."""


def total(lines: list[tuple[float, int]], discount_pct: float = 0.0, vat_pct: float = 8.1) -> float:
    """Sum (unit price, quantity) lines, then apply the discount and VAT."""
    result = 0.0
    for price, qty in lines:
        # Bug: VAT is applied per line before the discount, and each line is rounded.
        result += round(price * qty * (1 + vat_pct / 100), 2)
    return round(result * (1 - discount_pct / 100), 2)
