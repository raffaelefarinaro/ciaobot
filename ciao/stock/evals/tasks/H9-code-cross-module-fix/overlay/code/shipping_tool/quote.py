"""Shipping quotes for the order form. The form asks for the weight in kilograms."""

from rates import shipping_fee


def quote(weight_kg: float, parcels: int = 1) -> float:
    """Shipping for ``parcels`` parcels of ``weight_kg`` each."""
    return round(shipping_fee(weight_kg) * parcels, 2)
