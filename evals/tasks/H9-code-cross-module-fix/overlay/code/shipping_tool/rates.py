"""Parcel shipping rates in CHF."""

# Orders of CHF 100.00 or more ship free.
FREE_SHIPPING_FROM = 100.0


def shipping_fee(weight: float) -> float:
    """Fee for one parcel.

    Up to and including 2 kg: CHF 7.00. Up to and including 10 kg: CHF 9.70.
    Heavier parcels: CHF 20.50.
    """
    if weight < 2:
        return 7.0
    if weight <= 10:
        return 9.7
    return 20.5
