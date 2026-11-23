from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.parameters import GrantParameters


def payment_plan_stub(rule, benefit_plan_type="INDIVIDUAL", code="PP-AG", programme=None):
    return SimpleNamespace(
        code=code,
        json_ext={"calculation_rule": rule},
        benefit_plan=SimpleNamespace(
            type=benefit_plan_type,
            json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]} if programme is None else programme,
        ),
    )


class GrantParametersTest(SimpleTestCase):
    def rule(self, **overrides):
        return {
            "grant_component": "cash",
            "grant_amount_source": "payment_plan",
            "grant_amount": "1500",
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
        self.assertEqual(params.grant_component, "cash")
        self.assertEqual(params.programme.allowed_grant_types, ("CASH", "EQUIPMENT"))
        self.assertEqual(params.grant_amount_source, "payment_plan")
        self.assertEqual(params.grant_amount, Decimal("1500"))
        self.assertEqual(params.tranche_percentage, Decimal("70"))
        self.assertEqual(params.minimum_attendance_percentage, Decimal("80"))
        self.assertFalse(params.programme.counts_completed_trainings_only)

    def test_reads_values_stored_as_text_by_the_payment_plan_ui(self):
        params = self.parse(tranche_percentage="30", minimum_attendance_percentage="75.5")
        self.assertEqual(params.tranche_percentage, Decimal("30"))
        self.assertEqual(params.minimum_attendance_percentage, Decimal("75.5"))

    def test_reads_the_programme_disqualification_percentage(self):
        for value in (90, "90"):
            with self.subTest(value=value):
                plan = payment_plan_stub(self.rule(minimum_attendance_percentage="90"), programme={
                    "allowed_grant_types": ["CASH"], "training_disqualification_percentage": value})
                programme = GrantParameters.from_payment_plan(plan).programme
                self.assertEqual(programme.training_disqualification_percentage, Decimal("90"))
                self.assertTrue(programme.counts_completed_trainings_only)

    def test_a_disqualifying_programme_needs_its_minimum_set_at_least_that_high(self):
        umep = {"allowed_grant_types": ["CASH"], "training_disqualification_percentage": 90}
        self.assert_configuration_error(
            "minimum_attendance_percentage is required for this programme (at least 90)", programme=umep)
        self.assert_configuration_error(
            "minimum_attendance_percentage must be at least the programme's training_disqualification_percentage (90)",
            rule=self.rule(minimum_attendance_percentage="80"), programme=umep)

    def test_the_forms_empty_choice_counts_as_missing(self):
        self.assert_configuration_error("grant_component must be one of cash, in_kind",
                                        rule=self.rule(grant_component="null"))
        self.assert_configuration_error("grant_amount_source must be one of payment_plan, beneficiary_json",
                                        rule=self.rule(grant_amount_source="null"))
        self.assertEqual(self.parse(minimum_attendance_percentage="null").minimum_attendance_percentage,
                         Decimal("80"))

    def test_rejects_an_invalid_programme_disqualification_percentage(self):
        self.assert_configuration_error(
            "the programme's training_disqualification_percentage must be a number above 0 and at most 100",
            programme={"allowed_grant_types": ["CASH"], "training_disqualification_percentage": "ninety"},
        )

    def test_beneficiary_json_source_needs_no_plan_amount(self):
        params = self.parse(grant_amount_source="beneficiary_json", grant_amount=None)
        self.assertIsNone(params.grant_amount)

    def test_rejects_a_missing_or_unknown_grant_component(self):
        for component in (None, "", "equipment"):
            with self.subTest(component=component):
                self.assert_configuration_error(
                    "grant_component must be one of cash, in_kind",
                    rule=self.rule(grant_component=component),
                )

    def test_rejects_an_in_kind_plan_for_a_cash_only_programme(self):
        self.assertEqual(self.parse(grant_component="in_kind").grant_component, "in_kind")
        self.assert_configuration_error(
            "an in_kind plan needs a programme that allows EQUIPMENT or SERVICE",
            rule=self.rule(grant_component="in_kind"),
            programme={"allowed_grant_types": ["CASH"]},
        )

    def test_rejects_missing_or_invalid_allowed_grant_types(self):
        for allowed in (None, [], "CASH", ["CASH", "CASH"], ["CASH", "LAND"]):
            with self.subTest(allowed=allowed):
                programme = {} if allowed is None else {"allowed_grant_types": allowed}
                self.assert_configuration_error(
                    "the programme's allowed_grant_types must list one or more of CASH, EQUIPMENT, SERVICE",
                    programme=programme,
                )

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
        for name in ("tranche_percentage", "minimum_attendance_percentage"):
            for value in ("0", "100.5", "-1", "x", False):
                with self.subTest(name=name, value=value):
                    self.assert_configuration_error(
                        f"{name} must be a number above 0 and at most 100",
                        rule=self.rule(**{name: value}),
                    )

    def test_accepts_group_and_individual_programmes(self):
        for benefit_plan_type in ("GROUP", "INDIVIDUAL"):
            with self.subTest(benefit_plan_type=benefit_plan_type):
                plan = payment_plan_stub(self.rule(), benefit_plan_type=benefit_plan_type)
                self.assertEqual(GrantParameters.from_payment_plan(plan).grant_component, "cash")

    def test_rejects_second_tranche_plans_until_supported(self):
        self.assert_configuration_error(
            "second-tranche plans (requires_reconciled_payment_plan_ids) are not supported yet",
            rule=self.rule(requires_reconciled_payment_plan_ids=["5"]),
        )

    def test_lists_every_problem_with_the_plan_code(self):
        message = self.assert_configuration_error(
            "Payment plan PP-AG cannot be calculated",
            rule={"grant_amount_source": "payment_plan", "tranche_percentage": "0"},
        )
        self.assertIn("grant_amount must be a positive number", message)
        self.assertIn("tranche_percentage must be a number", message)

    def test_rejects_a_plan_without_rule_parameters(self):
        plan = SimpleNamespace(code="PP-EMPTY", json_ext=None, benefit_plan=SimpleNamespace(type="INDIVIDUAL", json_ext={"allowed_grant_types": ["CASH"]}))
        with self.assertRaises(AttendanceGrantConfigurationError):
            GrantParameters.from_payment_plan(plan)
