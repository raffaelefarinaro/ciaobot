import unittest

from rates import shipping_fee


class ShippingFeeTests(unittest.TestCase):
    def test_light_parcel(self) -> None:
        self.assertEqual(shipping_fee(0.5), 7.0)

    def test_two_kg_is_still_light(self) -> None:
        self.assertEqual(shipping_fee(2), 7.0)

    def test_medium_parcel(self) -> None:
        self.assertEqual(shipping_fee(7.5), 9.7)

    def test_ten_kg_is_medium(self) -> None:
        self.assertEqual(shipping_fee(10), 9.7)

    def test_heavy_parcel(self) -> None:
        self.assertEqual(shipping_fee(12), 20.5)


if __name__ == "__main__":
    unittest.main()
