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
    create_individual_benefit_plan,
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


APPROVED = {"business_plan_status": "APPROVED", "plan_approved_on": "2026-10-01"}


class AttendanceGrantRunMixin:
    """Payroll plumbing shared by the rule tests. Each module has 5 topics."""

    @classmethod
    def set_up_programme(cls, code, allowed_grant_types):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_individual_benefit_plan(
            cls.user, code, json_ext={"allowed_grant_types": allowed_grant_types})
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
            "already has a benefit under this payment plan that is not rejected",
            "\n".join(logs.output),
        )

    def test_a_rejected_benefit_does_not_block_a_new_payment(self):
        plan = self.payment_plan()
        first = self.benefits(self.run_payroll(plan)).get()
        first.status = BenefitConsumptionStatus.REJECTED
        first.save(username=self.user.username)
        self.assertEqual(self.benefits(self.run_payroll(plan)).count(), 1)

    def test_benefits_under_another_plan_do_not_count_as_duplicates(self):
        self.run_payroll(self.payment_plan())
        self.assertEqual(self.benefits(self.run_payroll(self.payment_plan())).count(), 1)

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
