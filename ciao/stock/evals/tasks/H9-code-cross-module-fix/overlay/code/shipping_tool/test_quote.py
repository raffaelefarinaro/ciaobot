import unittest

from quote import quote


class QuoteTests(unittest.TestCase):
    def test_single_light_parcel(self) -> None:
        self.assertEqual(quote(1.5), 7.0)

    def test_single_medium_parcel(self) -> None:
        self.assertEqual(quote(5), 9.7)

    def test_two_heavy_parcels(self) -> None:
        self.assertEqual(quote(15, parcels=2), 41.0)


if __name__ == "__main__":
    unittest.main()
