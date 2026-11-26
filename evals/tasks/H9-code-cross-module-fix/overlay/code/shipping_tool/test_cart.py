import unittest

from cart import Cart


class CartTests(unittest.TestCase):
    def test_empty_cart_is_free(self) -> None:
        self.assertEqual(Cart().total(), 0.0)

    def test_light_parcel_fee(self) -> None:
        cart = Cart()
        cart.add("Seeds", 4.5, qty=2, weight_g=250)
        self.assertEqual(cart.shipping(), 7.0)
        self.assertEqual(cart.total(), 16.0)

    def test_two_kg_parcel_is_light(self) -> None:
        cart = Cart()
        cart.add("Soil", 12.0, qty=1, weight_g=2000)
        self.assertEqual(cart.shipping(), 7.0)

    def test_medium_parcel_fee(self) -> None:
        cart = Cart()
        cart.add("Watering can", 18.0, qty=1, weight_g=3500)
        self.assertEqual(cart.shipping(), 9.7)
        self.assertEqual(cart.total(), 27.7)

    def test_free_shipping_from_100(self) -> None:
        cart = Cart()
        cart.add("Hose", 50.0, qty=2, weight_g=1200)
        self.assertEqual(cart.shipping(), 0.0)
        self.assertEqual(cart.total(), 100.0)

    def test_just_under_free_shipping_pays(self) -> None:
        cart = Cart()
        cart.add("Hose", 49.95, qty=2, weight_g=1200)
        self.assertEqual(cart.shipping(), 9.7)
        self.assertEqual(cart.total(), 109.6)


if __name__ == "__main__":
    unittest.main()
