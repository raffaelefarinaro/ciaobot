import unittest

from invoice import total


class TotalTests(unittest.TestCase):
    def test_no_discount_no_vat(self) -> None:
        self.assertEqual(total([(10.0, 2)], 0, 0), 20.0)

    def test_vat_only(self) -> None:
        self.assertEqual(total([(10.0, 2)], 0, 10), 22.0)

    def test_discount_and_vat(self) -> None:
        self.assertEqual(total([(19.99, 3)], 10, 8.1), 58.34)

    def test_empty_lines_is_zero(self) -> None:
        self.assertEqual(total([]), 0.0)


if __name__ == "__main__":
    unittest.main()
