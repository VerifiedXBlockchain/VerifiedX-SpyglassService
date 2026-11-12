from decimal import Decimal

from django.test import SimpleTestCase

from p2p import rules


class UrlTests(SimpleTestCase):
    def test_normalizing_strips_prefixes_and_case(self):
        for raw, expected in (
            ("vfx://CoolStuff", "coolstuff"),
            ("rbx://coolstuff", "coolstuff"),
            ("  VFX://Cool-Stuff.2 ", "cool-stuff.2"),
            ("coolstuff", "coolstuff"),
            ("rbx://vfx://x", "x"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(rules.normalize_url(raw), expected)
        self.assertEqual(rules.normalize_url(None), "")

    def test_core_url_pattern(self):
        # DecShop.CheckURL: a letter, then up to 62 letters, digits, dots or dashes.
        for url in ("a", "vfx://Shop", "shop-1.two", "a" * 63, "rbx://x9"):
            with self.subTest(url=url):
                self.assertTrue(rules.is_valid_url(url))
        for url in ("", "1shop", "-shop", "a" * 64, "sh op", "shop!", "shop_1", "vfx://", "ünï", None):
            with self.subTest(url=url):
                self.assertFalse(rules.is_valid_url(url))

    def test_lengths_count_utf16_units_as_core_does(self):
        self.assertEqual(rules.utf16_length("abc"), 3)
        self.assertEqual(rules.utf16_length("é"), 1)
        self.assertEqual(rules.utf16_length("😀"), 2)
        self.assertEqual(rules.word_count("  two\twords\n"), 2)


class AmountTests(SimpleTestCase):
    def test_parses_strings_and_numbers_up_to_eight_places(self):
        self.assertEqual(rules.parse_amount("12.5"), Decimal("12.5"))
        self.assertEqual(rules.parse_amount(3), Decimal("3"))
        self.assertEqual(rules.parse_amount(0.1), Decimal("0.1"))
        self.assertEqual(rules.parse_amount("0.00000001"), Decimal("0.00000001"))

    def test_refuses_what_is_not_an_amount(self):
        for value in ("0.000000001", "abc", "", None, True, "NaN", "Infinity", [1], {"a": 1}, "1e40"):
            with self.subTest(value=value):
                self.assertIsNone(rules.parse_amount(value))

    def test_formats_without_trailing_zeros_or_exponent(self):
        self.assertEqual(rules.format_amount(Decimal("10.50000000")), "10.5")
        self.assertEqual(rules.format_amount(Decimal("10")), "10")
        self.assertEqual(rules.format_amount(Decimal("100")), "100")
        self.assertEqual(rules.format_amount(Decimal("0.00000001")), "0.00000001")
        self.assertIsNone(rules.format_amount(None))


class BidMessageTests(SimpleTestCase):
    def test_core_message_uses_whole_1e8_units(self):
        self.assertEqual(rules.bid_message("Key1762900000", Decimal("12.5"), "xAddr"), "Key1762900000_1250000000_xAddr")
        self.assertEqual(rules.bid_message("K", Decimal("0.00000001"), "x"), "K_1_x")
        self.assertEqual(rules.bid_message("K", Decimal("123456.78901234"), "x"), "K_12345678901234_x")

    def test_increment_ladder_has_no_gaps(self):
        self.assertEqual(rules.bid_increment(Decimal("0")), Decimal("0.01"))
        self.assertEqual(rules.bid_increment(Decimal("0.995")), Decimal("0.01"))
        self.assertEqual(rules.bid_increment(Decimal("1")), Decimal("0.5"))
        self.assertEqual(rules.bid_increment(Decimal("24.999")), Decimal("0.5"))
        self.assertEqual(rules.bid_increment(Decimal("25")), Decimal("1"))
        self.assertEqual(rules.bid_increment(Decimal("249.995")), Decimal("1"))
        self.assertEqual(rules.bid_increment(Decimal("250")), Decimal("5"))
        self.assertEqual(rules.bid_increment(Decimal("999.99")), Decimal("10"))
        self.assertEqual(rules.bid_increment(Decimal("2499.99")), Decimal("50"))
        self.assertEqual(rules.bid_increment(Decimal("4999.99")), Decimal("75"))
        self.assertEqual(rules.bid_increment(Decimal("5000")), Decimal("100"))


class ContractTests(SimpleTestCase):
    def test_royalty_from_either_contract_shape(self):
        feature = {"FeatureName": 1, "FeatureFeatures": {"RoyaltyType": 1, "RoyaltyAmount": 0.1,
                                                          "RoyaltyPayToAddress": "xPayee"}}
        self.assertEqual(rules.contract_royalty({"Features": [feature]}), (Decimal("0.1"), "xPayee"))
        self.assertEqual(
            rules.contract_royalty('{"SmartContractMain": {"Features": [%s]}}' % __import__("json").dumps(feature)),
            (Decimal("0.1"), "xPayee"),
        )
        self.assertIsNone(rules.contract_royalty({"Features": []}))
        self.assertIsNone(rules.contract_royalty("not json"))

    def test_tokens_and_vaults_are_not_sellable(self):
        for name in (3, 13, 14):
            self.assertFalse(rules.is_sellable_contract({"Features": [{"FeatureName": name}]}))
        self.assertTrue(rules.is_sellable_contract({"Features": [{"FeatureName": 1}, {"FeatureName": 0}]}))
        self.assertTrue(rules.is_sellable_contract(""))
