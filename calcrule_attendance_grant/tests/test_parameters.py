from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase

from core.test_helpers import LogInHelper
from project_social_protection.tests.test_helpers import find_or_create_activity

from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.parameters import GrantParameters


def payment_plan_stub(rule, benefit_plan_type="INDIVIDUAL", code="PP-AG"):
    return SimpleNamespace(
        code=code,
        json_ext={"calculation_rule": rule},
        benefit_plan=SimpleNamespace(type=benefit_plan_type),
    )


class GrantParametersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = LogInHelper().get_or_create_user_api()
        cls.module_1 = find_or_create_activity("Grant parameters module 1", user.username)
        cls.module_2 = find_or_create_activity("Grant parameters module 2", user.username)

    def rule(self, **overrides):
        return {
            "grant_amount_source": "payment_plan",
            "grant_amount": "1500",
            "required_activity_ids": [str(self.module_1.id)],
            **overrides,
        }

    def parse(self, **overrides):
        return GrantParameters.from_payment_plan(payment_plan_stub(self.rule(**overrides)))

    def assert_configuration_error(self, message, rule=None, **stub_overrides):
        plan = payment_plan_stub(self.rule() if rule is None else rule, **stub_overrides)
        with self.assertRaises(AttendanceGrantConfigurationError) as ctx:
            GrantParameters.from_payment_plan(plan)
        self.assertIn(message, ctx.exception.messages[0])
        return ctx.exception.messages[0]

    def test_defaults(self):
        params = self.parse()
        self.assertEqual(params.grant_amount_source, "payment_plan")
        self.assertEqual(params.grant_amount, Decimal("1500"))
        self.assertEqual(params.beneficiary_grant_amount_key, "approved_grant_amount")
        self.assertEqual(params.tranche_percentage, Decimal("70"))
        self.assertEqual(params.minimum_attendance_percentage, Decimal("80"))
        self.assertEqual(params.attended_day_threshold, 100)
        self.assertTrue(params.only_completed_sessions)
        self.assertEqual(params.required_activity_ids, (str(self.module_1.id),))

    def test_reads_values_stored_as_text_by_the_payment_plan_ui(self):
        params = self.parse(
            tranche_percentage="30", minimum_attendance_percentage="75.5",
            attended_day_threshold="50", only_completed_sessions="false",
            required_activity_ids=f"{self.module_1.id}, {self.module_2.id}",
        )
        self.assertEqual(params.tranche_percentage, Decimal("30"))
        self.assertEqual(params.minimum_attendance_percentage, Decimal("75.5"))
        self.assertEqual(params.attended_day_threshold, 50)
        self.assertFalse(params.only_completed_sessions)
        self.assertEqual(
            params.required_activity_ids, (str(self.module_1.id), str(self.module_2.id))
        )

    def test_fractional_attended_day_threshold_rounds_up(self):
        self.assertEqual(self.parse(attended_day_threshold=79.5).attended_day_threshold, 80)

    def test_beneficiary_json_source_uses_the_amount_key(self):
        params = self.parse(
            grant_amount_source="beneficiary_json", grant_amount=None,
            beneficiary_grant_amount_key=" business_grant ",
        )
        self.assertIsNone(params.grant_amount)
        self.assertEqual(params.beneficiary_grant_amount_key, "business_grant")

    def test_rejects_a_missing_or_unknown_grant_source(self):
        for source in (None, "", "fixed"):
            with self.subTest(source=source):
                self.assert_configuration_error(
                    "grant_amount_source must be one of payment_plan, beneficiary_json",
                    rule=self.rule(grant_amount_source=source),
                )

    def test_rejects_a_missing_or_invalid_plan_grant_amount(self):
        for amount in (None, "", "abc", "0", -5, True, "NaN"):
            with self.subTest(amount=amount):
                self.assert_configuration_error(
                    "grant_amount must be a positive number",
                    rule=self.rule(grant_amount=amount),
                )

    def test_rejects_out_of_range_percentages(self):
        for name in ("tranche_percentage", "minimum_attendance_percentage", "attended_day_threshold"):
            for value in ("0", "100.5", "-1", "x", False):
                with self.subTest(name=name, value=value):
                    self.assert_configuration_error(
                        f"{name} must be a number above 0 and at most 100",
                        rule=self.rule(**{name: value}),
                    )

    def test_rejects_an_invalid_completed_sessions_flag(self):
        self.assert_configuration_error(
            "only_completed_sessions must be true or false",
            rule=self.rule(only_completed_sessions="yes"),
        )

    def test_rejects_missing_invalid_or_unknown_activity_ids(self):
        self.assert_configuration_error(
            "required_activity_ids must list at least one Activity ID",
            rule=self.rule(required_activity_ids=[]),
        )
        self.assert_configuration_error(
            "required_activity_ids has invalid IDs: not-an-id",
            rule=self.rule(required_activity_ids=["not-an-id"]),
        )
        unknown = "00000000-0000-0000-0000-000000000001"
        self.assert_configuration_error(
            f"refers to Activities that do not exist: {unknown}",
            rule=self.rule(required_activity_ids=[str(self.module_1.id), unknown]),
        )

    def test_rejects_group_benefit_plans(self):
        self.assert_configuration_error(
            "the benefit plan must be an INDIVIDUAL benefit plan",
            benefit_plan_type="GROUP",
        )

    def test_rejects_second_tranche_plans_until_supported(self):
        self.assert_configuration_error(
            "second-tranche plans (requires_reconciled_payment_plan_id) are not supported yet",
            rule=self.rule(requires_reconciled_payment_plan_id="5"),
        )

    def test_lists_every_problem_with_the_plan_code(self):
        message = self.assert_configuration_error(
            "Payment plan PP-AG cannot be calculated",
            rule={"grant_amount_source": "payment_plan"},
        )
        self.assertIn("grant_amount must be a positive number", message)
        self.assertIn("required_activity_ids must list at least one Activity ID", message)

    def test_rejects_a_plan_without_rule_parameters(self):
        plan = SimpleNamespace(code="PP-EMPTY", json_ext=None, benefit_plan=SimpleNamespace(type="INDIVIDUAL"))
        with self.assertRaises(AttendanceGrantConfigurationError):
            GrantParameters.from_payment_plan(plan)
