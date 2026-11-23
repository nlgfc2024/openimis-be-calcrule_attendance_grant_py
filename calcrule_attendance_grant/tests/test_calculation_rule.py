import uuid
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from calculation.apps import CALCULATION_RULES
from calculation.services import get_calculation_object
from contribution_plan.models import PaymentPlan
from core.test_helpers import LogInHelper
from invoice.models import Bill
from payment_cycle.models import PaymentCycle
from payroll.models import (
    BenefitAttachment,
    BenefitConsumption,
    BenefitConsumptionStatus,
    Payroll,
    PayrollBenefitConsumption,
)
from social_protection.models import Beneficiary, BenefitPlan

from calcrule_attendance_grant.calculation_rule import AttendanceGrantCalculationRule
from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.tests.helpers import (
    attend,
    create_programme,
    create_module,
    create_participant,
    create_training,
)

STRATEGY_LOGGER = "calcrule_attendance_grant.strategy"


class AttendanceGrantRegistrationTest(TestCase):
    def test_rule_is_registered_and_offered_for_payment_plans(self):
        self.assertIn(AttendanceGrantCalculationRule, CALCULATION_RULES)
        self.assertIs(
            get_calculation_object(AttendanceGrantCalculationRule.uuid),
            AttendanceGrantCalculationRule,
        )
        self.assertIs(
            AttendanceGrantCalculationRule.get_rule_name(None, "PaymentPlan"),
            AttendanceGrantCalculationRule,
        )


APPROVED = {"business_plan_status": "APPROVED", "plan_approved_on": "2026-10-01", "plan_approved_by": "Committee",
            "plan_approved_by_id": "approver"}


class AttendanceGrantRunMixin:
    """Payroll plumbing shared by the rule tests. Each module has 5 topics."""

    @classmethod
    def set_up_programme(cls, code, allowed_grant_types, plan_type='INDIVIDUAL'):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(
            cls.user, code, json_ext={"allowed_grant_types": allowed_grant_types}, plan_type=plan_type)
        cls.module_1 = create_module(cls.user, cls.benefit_plan, f"{code} module 1", 5, f"{code}1")
        cls.module_2 = create_module(cls.user, cls.benefit_plan, f"{code} module 2", 5, f"{code}2")
        cls.training = create_training(cls.user, cls.benefit_plan, [cls.module_1, cls.module_2], code)
        cls.payment_cycle = PaymentCycle(
            start_date='2026-11-01', end_date='2026-11-30',
            type=ContentType.objects.get_for_model(BenefitPlan), code=f'{code}-2026-11',
        )
        cls.payment_cycle.save(username=cls.user.username)

    @classmethod
    def participant(cls, name, json_ext=None, topics=10):
        beneficiary = create_participant(cls.user, cls.benefit_plan, name, {**APPROVED, **(json_ext or {})})
        attend(cls.user, beneficiary, cls.training, topics)
        return beneficiary

    def payment_plan(self, calculation=None, **rule):
        plan = PaymentPlan(
            code=f'AG-{uuid.uuid4().hex[:6]}',
            name='Training grant tranche 1',
            benefit_plan=self.benefit_plan,
            periodicity=1,
            calculation=calculation or AttendanceGrantCalculationRule.uuid,
            json_ext={"calculation_rule": {**self.default_rule, **rule}},
        )
        plan.save(username=self.user.username)
        return plan

    def run_payroll(self, payment_plan, **kwargs):
        payroll = Payroll(
            name=f'Payroll {uuid.uuid4().hex[:6]}', payment_plan=payment_plan,
            payment_cycle=self.payment_cycle,
        )
        payroll.save(username=self.user.username)
        AttendanceGrantCalculationRule.calculate_if_active_for_object(
            payment_plan,
            user_id=self.user.id,
            start_date=self.payment_cycle.start_date,
            end_date=self.payment_cycle.end_date,
            payroll=payroll,
            payment_cycle=self.payment_cycle,
            **kwargs,
        )
        return payroll

    @staticmethod
    def benefits(payroll):
        return BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll, is_deleted=False,
        )


class UpgCashGrantTest(AttendanceGrantRunMixin, TestCase):
    default_rule = {"grant_component": "cash", "grant_amount_source": "payment_plan", "grant_amount": "1000"}

    @classmethod
    def setUpTestData(cls):
        cls.set_up_programme('AGUPG', ["CASH"])
        cls.graduate = cls.participant('Graduate')
        cls.dropout = cls.participant('Dropout', topics=7)
        cls.unapproved = create_participant(
            cls.user, cls.benefit_plan, 'Unapproved', {"business_plan_status": "PENDING"})
        attend(cls.user, cls.unapproved, cls.training, 10)

    def test_pays_eligible_participants_seventy_percent_of_the_plan_amount_in_cash(self):
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            payroll = self.run_payroll(self.payment_plan())
        benefit = self.benefits(payroll).get()
        self.assertEqual(benefit.individual_id, self.graduate.individual_id)
        self.assertEqual(benefit.amount, Decimal("700.00"))
        self.assertEqual(benefit.type, "CASH")
        self.assertEqual(benefit.json_ext, {
            "attendance_grant": {
                "tranche": 1, "grant_component": "cash", "grant_types": ["CASH"],
                "grant_amount": 1000.0, "tranche_percentage": 70.0, "tranche_share": 700.0,
                "cash_amount": 700.0, "equipment_amount": 0.0, "service_amount": 0.0,
            },
            "extra_info": {},
        })
        bill = Bill.objects.get(benefitattachment__benefit=benefit)
        self.assertEqual(bill.amount_total, Decimal("700.00"))
        self.assertEqual(str(bill.subject_id), str(self.graduate.id))
        output = "\n".join(logs.output)
        self.assertIn(
            f"beneficiary {self.dropout.id} (individual {self.dropout.individual_id}) not paid: "
            "module 'AGUPG module 2': attended 2 of 5 topics (40.0%), below 80%",
            output,
        )
        self.assertIn(
            f"beneficiary {self.unapproved.id} (individual {self.unapproved.individual_id}) not paid: "
            "business plan not approved (business_plan_status is 'PENDING')",
            output,
        )
        self.assertIn("1 of 3 participant(s) paid", output)

    def test_participants_with_a_benefit_under_the_plan_are_not_paid_again(self):
        plan = self.payment_plan()
        self.assertEqual(self.benefits(self.run_payroll(plan)).count(), 1)
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            second = self.run_payroll(plan)
        self.assertFalse(self.benefits(second).exists())
        self.assertIn(
            f"beneficiary {self.graduate.id} (individual {self.graduate.individual_id}) not paid: "
            "already has a cash benefit for this tranche that is not rejected",
            "\n".join(logs.output),
        )

    def test_a_rejected_benefit_does_not_block_a_new_payment(self):
        plan = self.payment_plan()
        first = self.benefits(self.run_payroll(plan)).get()
        first.status = BenefitConsumptionStatus.REJECTED
        first.save(username=self.user.username)
        self.assertEqual(self.benefits(self.run_payroll(plan)).count(), 1)

    def test_another_cash_plan_of_the_programme_does_not_pay_again(self):
        # For example a plan re-created to fix a mistake.
        self.run_payroll(self.payment_plan())
        self.assertFalse(self.benefits(self.run_payroll(self.payment_plan())).exists())

    def test_a_payment_under_a_deleted_plan_still_counts(self):
        original = self.payment_plan()
        self.assertEqual(self.benefits(self.run_payroll(original)).count(), 1)
        original.is_deleted = True
        original.save(username=self.user.username)
        self.assertFalse(self.benefits(self.run_payroll(self.payment_plan())).exists())

    def test_the_rule_declares_its_payment_plan_parameters(self):
        parameters = AttendanceGrantCalculationRule.get_parameters(None, "PaymentPlan", self.payment_plan())
        self.assertEqual([parameter["name"] for parameter in parameters], [
            "tranche", "grant_component", "grant_amount_source", "grant_amount", "tranche_percentage",
            "minimum_attendance_percentage", "utilization_measure", "minimum_utilization_percentage",
        ])
        self.assertEqual([option["value"] for option in parameters[1]["optionSet"]], ["null", "cash", "in_kind"])

    def test_the_payroll_selection_narrows_who_is_paid(self):
        payroll = self.run_payroll(
            self.payment_plan(),
            beneficiaries_queryset=Beneficiary.objects.filter(id=self.dropout.id),
        )
        self.assertFalse(self.benefits(payroll).exists())

    def test_a_configuration_error_stops_the_run_before_anyone_is_paid(self):
        plan = self.payment_plan(grant_amount="")
        with self.assertRaises(AttendanceGrantConfigurationError) as ctx:
            self.run_payroll(plan)
        self.assertIn("grant_amount must be a positive number", ctx.exception.messages[0])
        self.assertFalse(
            PayrollBenefitConsumption.objects.filter(payroll__payment_plan=plan).exists()
        )

    def test_an_in_kind_plan_is_a_configuration_error_for_a_cash_only_programme(self):
        with self.assertRaisesMessage(AttendanceGrantConfigurationError, "an in_kind plan needs a programme"):
            self.run_payroll(self.payment_plan(grant_component="in_kind"))

    def test_a_selection_of_another_model_is_a_configuration_error(self):
        with self.assertRaisesMessage(AttendanceGrantConfigurationError, "expects a Beneficiary selection"):
            self.run_payroll(self.payment_plan(), beneficiaries_queryset=Bill.objects.none())

    def test_plans_using_another_rule_are_ignored(self):
        payroll = self.run_payroll(self.payment_plan(calculation=str(uuid.uuid4())))
        self.assertFalse(self.benefits(payroll).exists())
        self.assertFalse(BenefitAttachment.objects.filter(benefit__individual=self.graduate.individual).exists())


class RmepCashAndInKindGrantTest(AttendanceGrantRunMixin, TestCase):
    default_rule = {"grant_component": "cash", "grant_amount_source": "beneficiary_json"}

    @classmethod
    def setUpTestData(cls):
        cls.set_up_programme('AGRMEP', ["CASH", "EQUIPMENT"])
        cls.mixed = cls.participant('Mixed', {
            "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_cash_amount": 200, "tranche_1_equipment_amount": 500,
        })
        cls.equipment_only = cls.participant('EquipmentOnly', {
            "grant_types": ["EQUIPMENT"], "approved_grant_amount": 800,
            "tranche_1_equipment_amount": 800,
        })

    def test_the_cash_plan_pays_only_the_cash_part(self):
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            payroll = self.run_payroll(self.payment_plan())
        benefit = self.benefits(payroll).get()
        self.assertEqual((benefit.individual_id, benefit.amount, benefit.type),
                         (self.mixed.individual_id, Decimal("200.00"), "CASH"))
        self.assertEqual(benefit.json_ext["attendance_grant"]["equipment_amount"], 500.0)
        self.assertIn(
            f"beneficiary {self.equipment_only.id} (individual {self.equipment_only.individual_id}) "
            "not paid: its tranche has no cash part",
            "\n".join(logs.output),
        )

    def test_the_in_kind_plan_pays_the_equipment_part_for_the_piu_team(self):
        payroll = self.run_payroll(self.payment_plan(grant_component="in_kind"))
        benefits = {benefit.individual_id: benefit for benefit in self.benefits(payroll)}
        mixed = benefits[self.mixed.individual_id]
        self.assertEqual((mixed.amount, mixed.type), (Decimal("500.00"), "EQUIPMENT"))
        self.assertEqual(mixed.json_ext["extra_info"], {"equipment_amount": 500.0})
        # A grant without cash is given in full at tranche one.
        equipment_only = benefits[self.equipment_only.individual_id]
        self.assertEqual(equipment_only.amount, Decimal("800.00"))
        self.assertEqual(equipment_only.json_ext["attendance_grant"]["tranche_percentage"], 100.0)

    def test_the_cash_and_in_kind_payrolls_add_up_to_the_tranche_share(self):
        cash = self.benefits(self.run_payroll(self.payment_plan())).get()
        in_kind = self.benefits(self.run_payroll(self.payment_plan(grant_component="in_kind"))).get(
            individual_id=self.mixed.individual_id)
        self.assertEqual(cash.amount + in_kind.amount, Decimal("700.00"))

    def test_the_second_part_is_paid_from_the_tranche_recorded_with_the_first(self):
        self.benefits(self.run_payroll(self.payment_plan())).get()
        # The grant changes after the cash part (200 of a 700 share) was paid.
        Beneficiary.objects.filter(id=self.mixed.id).update(json_ext={
            **self.mixed.json_ext, "tranche_1_equipment_amount": 600, "tranche_1_cash_amount": 100})
        with self.assertLogs(STRATEGY_LOGGER, level="WARNING") as logs:
            payroll = self.run_payroll(self.payment_plan(grant_component="in_kind"))
        in_kind = self.benefits(payroll).get(individual_id=self.mixed.individual_id)
        self.assertEqual(in_kind.amount, Decimal("500.00"))
        self.assertIn("the grant details changed after the cash part of this tranche was paid", "\n".join(logs.output))

    def test_a_tranches_plans_must_use_the_same_tranche_percentage(self):
        cash = self.payment_plan(tranche_percentage="70")
        in_kind = self.payment_plan(grant_component="in_kind", tranche_percentage="60")
        for plan in (cash, in_kind):
            with self.subTest(plan=plan.code):
                with self.assertRaisesMessage(AttendanceGrantConfigurationError,
                                              "A tranche's cash and in-kind plans must use the same percentage"):
                    self.run_payroll(plan)
        self.assertFalse(BenefitConsumption.objects.filter(individual=self.mixed.individual).exists())

    def test_the_same_percentage_written_differently_matches(self):
        self.payment_plan(grant_component="in_kind", tranche_percentage=70)
        self.assertEqual(self.benefits(self.run_payroll(self.payment_plan(tranche_percentage="70.0"))).count(), 1)

    def test_invalid_amounts_or_make_up_exclude_the_beneficiary(self):
        cases = {
            'NoAmount': ({"grant_types": ["CASH"]}, "approved_grant_amount is missing"),
            'TextAmount': ({"grant_types": ["CASH"], "approved_grant_amount": "1500"},
                           "approved_grant_amount must be a number, got '1500'"),
            'NoTypes': ({"approved_grant_amount": 1000}, "grant_types is missing"),
            'BadSplit': ({"grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
                          "tranche_1_cash_amount": 100, "tranche_1_equipment_amount": 100},
                         "tranche-one amounts add up to 200.00, expected 700.00"),
        }
        participants = {name: self.participant(name, json_ext) for name, (json_ext, _) in cases.items()}
        with self.assertLogs(STRATEGY_LOGGER, level="WARNING") as logs:
            payroll = self.run_payroll(self.payment_plan())
        self.assertEqual(
            list(self.benefits(payroll).values_list("individual_id", flat=True)),
            [self.mixed.individual_id],
        )
        output = "\n".join(logs.output)
        for name, (_, reason) in cases.items():
            with self.subTest(name=name):
                self.assertIn(f"{participants[name].id} (individual {participants[name].individual_id}) "
                              f"not paid: {reason}", output)
        self.assertFalse(BenefitConsumption.objects.filter(amount__lte=0).exists())


class TrancheTwoTest(AttendanceGrantRunMixin, TestCase):
    """Tranche two pays the rest of a grant with cash once tranche one is reconciled."""
    default_rule = {"grant_component": "cash", "grant_amount_source": "beneficiary_json"}
    ASSESSED = {"utilization_assessed_on": "2026-12-01", "utilization_assessed_by": "Officer"}

    @classmethod
    def setUpTestData(cls):
        cls.set_up_programme('AGT2', ["CASH", "EQUIPMENT"])
        cls.mixed = cls.participant('Mixed', {
            "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000, "tranche_1_equipment_amount": 500})
        cls.equipment_only = cls.participant('EquipmentOnly', {
            "grant_types": ["EQUIPMENT"], "approved_grant_amount": 800, "tranche_1_equipment_amount": 800})

    def pay_tranche_one(self, reconcile=True):
        self.run_payroll(self.payment_plan())
        self.run_payroll(self.payment_plan(grant_component="in_kind"))
        if reconcile:
            BenefitConsumption.objects.filter(
                individual__in=[self.mixed.individual, self.equipment_only.individual],
            ).update(status=BenefitConsumptionStatus.RECONCILED)

    def assess(self, beneficiary, **values):
        Beneficiary.objects.filter(id=beneficiary.id).update(
            json_ext={**Beneficiary.objects.get(id=beneficiary.id).json_ext, **values})

    def tranche_two(self, **rule):
        return self.payment_plan(**{"tranche": "2", "utilization_measure": "percentage", **rule})

    def run_tranche_two(self, **rule):
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            payroll = self.run_payroll(self.tranche_two(**rule))
        return payroll, "\n".join(logs.output)

    def test_the_rest_of_a_grant_with_cash_is_paid_once_tranche_one_is_reconciled(self):
        self.pay_tranche_one()
        self.assess(self.mixed, utilization_percentage=80, **self.ASSESSED)
        payroll, output = self.run_tranche_two()
        benefit = self.benefits(payroll).get()
        self.assertEqual((benefit.individual_id, benefit.amount, benefit.type),
                         (self.mixed.individual_id, Decimal("300.00"), "CASH"))
        self.assertEqual(benefit.json_ext["attendance_grant"]["tranche"], 2)
        self.assertEqual(benefit.json_ext["attendance_grant"]["cash_amount"], 300.0)
        self.assertIn("a grant without cash was given in full at tranche one", output)

    def test_tranche_one_must_be_reconciled_in_every_part(self):
        self.pay_tranche_one(reconcile=False)
        BenefitConsumption.objects.filter(individual=self.mixed.individual, type="CASH").update(
            status=BenefitConsumptionStatus.RECONCILED)
        self.assess(self.mixed, utilization_percentage=90, **self.ASSESSED)
        payroll, output = self.run_tranche_two()
        self.assertFalse(self.benefits(payroll).exists())
        self.assertIn("tranche one is not reconciled yet (1 of 2 part(s) not RECONCILED)", output)

    def test_the_utilization_assessment_must_meet_the_plan(self):
        self.pay_tranche_one()
        cases = {
            "utilization not assessed (no utilization_assessed_on)": {"utilization_percentage": 90},
            "utilization 65% below 80%": {"utilization_percentage": 65, **self.ASSESSED},
            "utilization_percentage must be a number, got '90'": {"utilization_percentage": "90", **self.ASSESSED},
        }
        for reason, values in cases.items():
            with self.subTest(reason=reason):
                Beneficiary.objects.filter(id=self.mixed.id).update(json_ext={
                    key: value for key, value in Beneficiary.objects.get(id=self.mixed.id).json_ext.items()
                    if not key.startswith("utilization_")})
                self.assess(self.mixed, **values)
                payroll, output = self.run_tranche_two()
                self.assertFalse(self.benefits(payroll).exists())
                self.assertIn(reason, output)

    def test_a_yes_no_programme_needs_a_confirmed_use(self):
        self.pay_tranche_one()
        self.assess(self.mixed, utilization_confirmed="NO", **self.ASSESSED)
        payroll, output = self.run_tranche_two(utilization_measure="yes_no")
        self.assertFalse(self.benefits(payroll).exists())
        self.assertIn("utilization not confirmed (utilization_confirmed is 'NO')", output)
        self.assess(self.mixed, utilization_confirmed="YES")
        payroll, _ = self.run_tranche_two(utilization_measure="yes_no")
        self.assertEqual(self.benefits(payroll).get().amount, Decimal("300.00"))

    def test_tranche_two_is_paid_once_from_the_grant_recorded_with_tranche_one(self):
        self.pay_tranche_one()
        self.assess(self.mixed, utilization_percentage=100, **self.ASSESSED)
        Beneficiary.objects.filter(id=self.mixed.id).update(json_ext={
            **Beneficiary.objects.get(id=self.mixed.id).json_ext, "approved_grant_amount": 2000})
        payroll, output = self.run_tranche_two()
        self.assertEqual(self.benefits(payroll).get().amount, Decimal("300.00"))
        self.assertIn("the grant details changed since tranche one", output)
        second, output = self.run_tranche_two()
        self.assertFalse(self.benefits(second).exists())
        self.assertIn("already has a tranche-two benefit that is not rejected", output)

    def test_nobody_is_paid_tranche_two_without_tranche_one(self):
        self.assess(self.mixed, utilization_percentage=100, **self.ASSESSED)
        payroll, output = self.run_tranche_two()
        self.assertFalse(self.benefits(payroll).exists())
        self.assertIn("no tranche-one payment of the programme", output)
