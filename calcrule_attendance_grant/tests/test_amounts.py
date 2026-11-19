from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from calcrule_attendance_grant.amounts import resolve_grant_amount, tranche_amount
from calcrule_attendance_grant.tests.helpers import grant_parameters


def beneficiary(json_ext):
    return SimpleNamespace(json_ext=json_ext)


class ResolveGrantAmountTest(SimpleTestCase):
    def setUp(self):
        self.from_beneficiary = grant_parameters(
            grant_amount_source="beneficiary_json", grant_amount=None,
        )

    def test_upg_uses_the_payment_plan_amount(self):
        params = grant_parameters(grant_amount=Decimal("500"))
        result = resolve_grant_amount(beneficiary({"approved_grant_amount": 9}), params)
        self.assertEqual(result.grant_amount, Decimal("500"))

    def test_rmep_and_umep_use_the_beneficiary_amount(self):
        for value, expected in ((1500, Decimal("1500")), (1234.56, Decimal("1234.56"))):
            with self.subTest(value=value):
                result = resolve_grant_amount(
                    beneficiary({"approved_grant_amount": value}), self.from_beneficiary
                )
                self.assertEqual(result.grant_amount, expected)
                self.assertIsNone(result.reason)

    def test_the_amount_key_is_configurable(self):
        params = grant_parameters(
            grant_amount_source="beneficiary_json", beneficiary_grant_amount_key="contribution",
        )
        result = resolve_grant_amount(beneficiary({"contribution": 800}), params)
        self.assertEqual(result.grant_amount, Decimal("800"))

    def test_missing_amounts_are_reported(self):
        for json_ext in (None, {}, {"approved_grant_amount": None}):
            with self.subTest(json_ext=json_ext):
                result = resolve_grant_amount(beneficiary(json_ext), self.from_beneficiary)
                self.assertIsNone(result.grant_amount)
                self.assertEqual(result.reason, "approved_grant_amount is missing")

    def test_non_numeric_amounts_are_reported(self):
        for value in ("1500", "abc", True, [1500], float("nan"), float("inf")):
            with self.subTest(value=value):
                result = resolve_grant_amount(
                    beneficiary({"approved_grant_amount": value}), self.from_beneficiary
                )
                self.assertIsNone(result.grant_amount)
                self.assertEqual(
                    result.reason, f"approved_grant_amount must be a number, got {value!r}"
                )

    def test_non_positive_amounts_are_reported(self):
        for value in (0, -10, 0.0):
            with self.subTest(value=value):
                result = resolve_grant_amount(
                    beneficiary({"approved_grant_amount": value}), self.from_beneficiary
                )
                self.assertIsNone(result.grant_amount)
                self.assertEqual(
                    result.reason, f"approved_grant_amount must be positive, got {value!r}"
                )


class TrancheAmountTest(SimpleTestCase):
    def test_splits_seventy_thirty(self):
        self.assertEqual(tranche_amount(Decimal("1500"), Decimal("70")), Decimal("1050.00"))
        self.assertEqual(tranche_amount(Decimal("1500"), Decimal("30")), Decimal("450.00"))

    def test_rounds_half_up_to_the_cent(self):
        self.assertEqual(tranche_amount(Decimal("0.05"), Decimal("70")), Decimal("0.04"))
        self.assertEqual(tranche_amount(Decimal("1234.56"), Decimal("70")), Decimal("864.19"))
        self.assertEqual(tranche_amount(Decimal("0.01"), Decimal("30")), Decimal("0.00"))
