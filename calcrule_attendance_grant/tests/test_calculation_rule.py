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
from project_social_protection.tests.test_helpers import find_or_create_activity

from calcrule_attendance_grant.calculation_rule import AttendanceGrantCalculationRule
from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.tests.helpers import (
    attend,
    create_individual_benefit_plan,
    create_participant,
    create_session,
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


class AttendanceGrantCalculationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_individual_benefit_plan(cls.user, 'AGRUN')
        cls.module_1 = find_or_create_activity("Grant run module 1", cls.user.username)
        cls.module_2 = find_or_create_activity("Grant run module 2", cls.user.username)
        cls.session_1 = create_session(cls.user, cls.benefit_plan, cls.module_1, 'Grant run module 1')
        cls.session_2 = create_session(cls.user, cls.benefit_plan, cls.module_2, 'Grant run module 2')
        cls.payment_cycle = PaymentCycle(
            start_date='2026-11-01', end_date='2026-11-30',
            type=ContentType.objects.get_for_model(BenefitPlan), code='AG-2026-11',
        )
        cls.payment_cycle.save(username=cls.user.username)

        cls.graduate = cls.participant('Graduate', {"approved_grant_amount": 1500})
        cls.dropout = cls.participant('Dropout', {"approved_grant_amount": 1500})
        attend(cls.user, cls.graduate, cls.session_1, 5)
        attend(cls.user, cls.graduate, cls.session_2, 5)
        attend(cls.user, cls.dropout, cls.session_1, 5)
        attend(cls.user, cls.dropout, cls.session_2, 2)

    @classmethod
    def participant(cls, name, json_ext=None):
        return create_participant(cls.user, cls.benefit_plan, name, json_ext)

    def payment_plan(self, calculation=None, **rule):
        plan = PaymentPlan(
            code=f'AG-{uuid.uuid4().hex[:6]}',
            name='Training grant tranche 1',
            benefit_plan=self.benefit_plan,
            periodicity=1,
            calculation=calculation or AttendanceGrantCalculationRule.uuid,
            json_ext={"calculation_rule": {
                "grant_amount_source": "payment_plan",
                "grant_amount": "1000",
                "required_activity_ids": [str(self.module_1.id), str(self.module_2.id)],
                **rule,
            }},
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

    def test_upg_pays_eligible_participants_seventy_percent_of_the_plan_amount(self):
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            payroll = self.run_payroll(self.payment_plan())
        benefit = self.benefits(payroll).get()
        self.assertEqual(benefit.individual_id, self.graduate.individual_id)
        self.assertEqual(benefit.amount, Decimal("700.00"))
        self.assertEqual(benefit.json_ext, {"extra_info": {
            "grant_amount": 1000.0, "tranche_percentage": 70.0, "tranche_amount": 700.0,
        }})
        bill = Bill.objects.get(benefitattachment__benefit=benefit)
        self.assertEqual(bill.amount_total, Decimal("700.00"))
        self.assertEqual(str(bill.subject_id), str(self.graduate.id))
        output = "\n".join(logs.output)
        self.assertIn(
            f"beneficiary {self.dropout.id} (individual {self.dropout.individual_id}) not paid: "
            "module 'Grant run module 2': attended 2 of 5 days (40.0%), below 80%",
            output,
        )
        self.assertIn("1 of 2 participant(s) paid", output)

    def test_rmep_and_umep_pay_the_tranche_of_each_beneficiary_amount(self):
        plan = self.payment_plan(
            grant_amount_source="beneficiary_json", grant_amount="", tranche_percentage="30",
        )
        benefit = self.benefits(self.run_payroll(plan)).get()
        self.assertEqual(benefit.amount, Decimal("450.00"))
        self.assertEqual(benefit.json_ext["extra_info"], {
            "grant_amount": 1500.0, "tranche_percentage": 30.0, "tranche_amount": 450.0,
        })

    def test_invalid_beneficiary_amounts_are_excluded_and_nobody_is_paid_zero(self):
        cases = {
            'NoAmount': {},
            'TextAmount': {"approved_grant_amount": "1500"},
            'ZeroAmount': {"approved_grant_amount": 0},
            'Tiny': {"approved_grant_amount": 0.01},
        }
        participants = {}
        for name, json_ext in cases.items():
            participants[name] = self.participant(name, json_ext)
            attend(self.user, participants[name], self.session_1, 5)
            attend(self.user, participants[name], self.session_2, 5)
        plan = self.payment_plan(grant_amount_source="beneficiary_json", tranche_percentage="30")
        with self.assertLogs(STRATEGY_LOGGER, level="WARNING") as logs:
            payroll = self.run_payroll(plan)
        self.assertEqual(
            list(self.benefits(payroll).values_list("individual_id", flat=True)),
            [self.graduate.individual_id],
        )
        output = "\n".join(logs.output)
        self.assertIn(f"{participants['NoAmount'].id} (individual", output)
        self.assertIn("approved_grant_amount is missing", output)
        self.assertIn("approved_grant_amount must be a number, got '1500'", output)
        self.assertIn("approved_grant_amount must be positive, got 0", output)
        self.assertIn("the tranche of 0.01 at 30% rounds to 0", output)
        self.assertFalse(BenefitConsumption.objects.filter(amount__lte=0).exists())

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

    def test_a_selection_of_another_model_is_a_configuration_error(self):
        with self.assertRaisesMessage(AttendanceGrantConfigurationError, "expects a Beneficiary selection"):
            self.run_payroll(self.payment_plan(), beneficiaries_queryset=Bill.objects.none())

    def test_plans_using_another_rule_are_ignored(self):
        payroll = self.run_payroll(self.payment_plan(calculation=str(uuid.uuid4())))
        self.assertFalse(self.benefits(payroll).exists())
        self.assertFalse(BenefitAttachment.objects.filter(benefit__individual=self.graduate.individual).exists())
