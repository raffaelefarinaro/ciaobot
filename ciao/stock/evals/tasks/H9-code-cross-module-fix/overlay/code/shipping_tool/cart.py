"""A shopping cart that prices items and shipping."""

from rates import FREE_SHIPPING_FROM, shipping_fee


class Cart:
    def __init__(self) -> None:
        # (name, unit price in CHF, quantity, weight of one unit in grams)
        self.items: list[tuple[str, float, int, int]] = []

    def add(self, name: str, unit_price: float, qty: int = 1, weight_g: int = 0) -> None:
        self.items.append((name, unit_price, qty, weight_g))

    def subtotal(self) -> float:
        return round(sum(price * qty for _, price, qty, _ in self.items), 2)

    def weight_g(self) -> int:
        return sum(weight * qty for _, _, qty, weight in self.items)

    def shipping(self) -> float:
        if not self.items or self.subtotal() > FREE_SHIPPING_FROM:
            return 0.0
        return shipping_fee(self.weight_g())

    def total(self) -> float:
        return round(self.subtotal() + self.shipping(), 2)
