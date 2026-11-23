from decimal import Decimal

from django.test import SimpleTestCase

from calcrule_attendance_grant.approval import plan_approval_shortfall
from calcrule_attendance_grant.grants import (
    GRANT_COMPONENT_CASH,
    GRANT_COMPONENT_IN_KIND,
    resolve_grant,
)

UPG = ["CASH"]
RMEP = ["CASH", "EQUIPMENT"]
UMEP = ["CASH", "EQUIPMENT", "SERVICE"]


def resolve(json_ext, allowed=RMEP, plan_amount=None, percentage="70", tranche=1):
    return resolve_grant(
        json_ext,
        allowed_grant_types=allowed,
        grant_amount_source_is_plan=plan_amount is not None,
        plan_grant_amount=Decimal(plan_amount) if plan_amount is not None else None,
        tranche_percentage=Decimal(percentage),
        tranche=tranche,
    )


class PlanApprovalTest(SimpleTestCase):
    def test_a_plan_approved_through_a_plan_approval_passes(self):
        self.assertIsNone(plan_approval_shortfall(
            {"business_plan_status": "APPROVED", "plan_approved_on": "2026-11-01", "plan_approved_by_id": "u1"}))

    def test_other_statuses_and_missing_approval_details_fail_with_a_reason(self):
        cases = {
            "business plan not approved (no business_plan_status)": {},
            "business plan not approved (business_plan_status is 'PENDING')": {
                "business_plan_status": "PENDING", "plan_approved_on": "2026-11-01"},
            "business plan approved without plan_approved_on": {
                "business_plan_status": "APPROVED", "plan_approved_on": " ", "plan_approved_by_id": "u1"},
            # Marked approved outside a plan approval, so nobody is recorded as approver.
            "business plan approved without plan_approved_by_id": {
                "business_plan_status": "APPROVED", "plan_approved_on": "2026-11-01"},
        }
        for reason, json_ext in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(plan_approval_shortfall(json_ext), reason)
        self.assertIsNotNone(plan_approval_shortfall(None))


class GrantTypesTest(SimpleTestCase):
    def test_a_single_allowed_type_is_assumed_when_none_is_recorded(self):
        grant = resolve({}, allowed=UPG, plan_amount="1000")
        self.assertEqual(grant.grant_types, ("CASH",))
        self.assertEqual(grant.amounts, {"CASH": Decimal("700.00")})

    def test_missing_invalid_repeated_or_disallowed_types_are_reasons(self):
        cases = {
            "grant_types is missing": {},
            "grant_types must be a non-empty list, got []": {"grant_types": []},
            "grant_types must be a non-empty list, got 'CASH'": {"grant_types": "CASH"},
            "grant_types repeats a type: ['CASH', 'CASH']": {"grant_types": ["CASH", "CASH"]},
            "grant_types has types the programme does not allow: SERVICE": {
                "grant_types": ["CASH", "SERVICE"]},
        }
        for reason, json_ext in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(resolve({**json_ext, "approved_grant_amount": 1000}).reason, reason)

    def test_types_keep_a_stable_order(self):
        grant = resolve({
            "grant_types": ["SERVICE", "CASH"], "approved_grant_amount": 1000,
            "tranche_1_cash_amount": 700,
        }, allowed=UMEP)
        self.assertEqual(grant.grant_types, ("CASH", "SERVICE"))


class GrantAmountTest(SimpleTestCase):
    def test_invalid_beneficiary_amounts_are_reasons(self):
        cases = {
            "approved_grant_amount is missing": {},
            "approved_grant_amount must be a number, got '1000'": {"approved_grant_amount": "1000"},
            "approved_grant_amount must be a number, got True": {"approved_grant_amount": True},
            "approved_grant_amount must be positive, got 0": {"approved_grant_amount": 0},
            "the tranche of 0.01 at 30% rounds to 0": {"approved_grant_amount": 0.01},
        }
        for reason, json_ext in cases.items():
            with self.subTest(reason=reason):
                grant = resolve({"grant_types": ["CASH"], **json_ext}, percentage="30")
                self.assertEqual(grant.reason, reason)

    def test_a_grant_with_cash_pays_the_plan_percentage(self):
        grant = resolve({"grant_types": ["CASH"], "approved_grant_amount": 1500}, percentage="30")
        self.assertEqual((grant.percentage, grant.share), (Decimal("30"), Decimal("450.00")))

    def test_a_grant_without_cash_pays_the_whole_amount(self):
        grant = resolve({
            "grant_types": ["EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_equipment_amount": 1000,
        })
        self.assertEqual((grant.percentage, grant.share), (Decimal("100"), Decimal("1000.00")))
        self.assertEqual(grant.amounts, {"EQUIPMENT": Decimal("1000.00")})


class TrancheMakeUpTest(SimpleTestCase):
    def test_a_tranche_splits_into_cash_and_in_kind_parts(self):
        grant = resolve({
            "grant_types": ["CASH", "EQUIPMENT", "SERVICE"], "approved_grant_amount": 1000,
            "tranche_1_cash_amount": 200, "tranche_1_equipment_amount": 400.5,
            "tranche_1_service_amount": 99.5,
        }, allowed=UMEP)
        self.assertIsNone(grant.reason)
        self.assertEqual(grant.part(GRANT_COMPONENT_CASH), Decimal("200.00"))
        self.assertEqual(grant.part(GRANT_COMPONENT_IN_KIND), Decimal("500.00"))
        self.assertEqual(grant.part_types(GRANT_COMPONENT_IN_KIND), ("EQUIPMENT", "SERVICE"))

    def test_missing_amounts_count_as_zero_once_any_is_recorded(self):
        grant = resolve({
            "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_equipment_amount": 700,
        })
        self.assertEqual(grant.amounts, {"CASH": Decimal("0.00"), "EQUIPMENT": Decimal("700.00")})
        self.assertEqual(grant.part(GRANT_COMPONENT_CASH), 0)
        self.assertEqual(grant.part_types(GRANT_COMPONENT_IN_KIND), ("EQUIPMENT",))

    def test_cash_is_the_rest_of_the_plan_share_when_not_recorded(self):
        json_ext = {"grant_types": ["CASH", "EQUIPMENT", "SERVICE"], "approved_grant_amount": 1000,
                    "tranche_1_equipment_amount": 400, "tranche_1_service_amount": 100}
        self.assertEqual(resolve(json_ext, allowed=UMEP).amounts, {
            "CASH": Decimal("200.00"), "EQUIPMENT": Decimal("400.00"), "SERVICE": Decimal("100.00")})
        # Another tranche percentage on the payment plan changes only the cash part.
        self.assertEqual(resolve(json_ext, allowed=UMEP, percentage="60").amounts["CASH"], Decimal("100.00"))
        # Tranche two needs no recorded amounts: it is the plan's share in cash.
        self.assertEqual(resolve(json_ext, allowed=UMEP, percentage="30", tranche=2).amounts, {
            "CASH": Decimal("300.00"), "EQUIPMENT": Decimal("0.00"), "SERVICE": Decimal("0.00")})

    def test_a_missing_in_kind_amount_next_to_a_recorded_cash_amount_is_noted(self):
        grant = resolve({
            "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000, "tranche_1_cash_amount": 700,
        })
        self.assertIsNone(grant.reason)
        self.assertEqual(grant.notes, (
            "tranche_1_equipment_amount is missing and counts as 0 although EQUIPMENT is one of the grant types",))
        self.assertEqual(resolve({"grant_types": ["CASH"], "approved_grant_amount": 1000}).notes, ())

    def test_a_cash_only_grant_needs_no_recorded_split(self):
        grant = resolve({"grant_types": ["CASH"], "approved_grant_amount": 1000})
        self.assertEqual(grant.amounts, {"CASH": Decimal("700.00")})

    def test_make_up_errors_are_reasons(self):
        base = {"grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000}
        cases = {
            "tranche-one amounts are missing (tranche_1_equipment_amount)": {},
            "tranche-one amounts: equipment and service add up to 800.00, more than the share 700.00": {
                "tranche_1_equipment_amount": 800},
            "tranche-one amounts add up to 650.00, expected 700.00": {
                "tranche_1_cash_amount": 150, "tranche_1_equipment_amount": 500},
            "tranche_1_cash_amount must be a number, got '700'": {"tranche_1_cash_amount": "700"},
            "tranche_1_cash_amount must not be negative, got -1": {
                "tranche_1_cash_amount": -1, "tranche_1_equipment_amount": 701},
            "tranche_1_service_amount is 100 but SERVICE is not one of the grant types": {
                "tranche_1_cash_amount": 600, "tranche_1_service_amount": 100},
        }
        for reason, amounts in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(resolve({**base, **amounts}).reason, reason)

    def test_tranche_two_reads_its_own_keys(self):
        grant = resolve({
            "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_cash_amount": 700, "tranche_2_equipment_amount": 300,
        }, percentage="30", tranche=2)
        self.assertEqual(grant.amounts, {"CASH": Decimal("0.00"), "EQUIPMENT": Decimal("300.00")})
