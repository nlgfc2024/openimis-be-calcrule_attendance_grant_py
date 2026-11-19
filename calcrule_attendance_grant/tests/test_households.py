from decimal import Decimal

from django.test import TestCase

from invoice.models import Bill
from project_social_protection.models import GroupBeneficiaryProjectEnrollment

from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.tests.helpers import (
    attend_household,
    create_household,
    create_training,
)
from calcrule_attendance_grant.tests.test_calculation_rule import (
    APPROVED,
    STRATEGY_LOGGER,
    AttendanceGrantRunMixin,
)

RMEP_GRANT = {
    **APPROVED, "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
    "tranche_1_cash_amount": 700,
}


class HouseholdGrantTest(AttendanceGrantRunMixin, TestCase):
    """UPG and RMEP are GROUP programmes paid to each household's primary worker."""
    default_rule = {"grant_component": "cash", "grant_amount_source": "beneficiary_json"}

    @classmethod
    def setUpTestData(cls):
        cls.set_up_programme('AGHH', ["CASH", "EQUIPMENT"], plan_type='GROUP')

        cls.household, cls.members = create_household(cls.user, cls.benefit_plan, 'AGHH-OK', json_ext=RMEP_GRANT)
        cls.primary_worker = cls.members[1]
        attend_household(cls.user, cls.household, cls.training, 10)

    def household_enrollments(self):
        return GroupBeneficiaryProjectEnrollment.objects.filter(
            group_beneficiary__benefit_plan=self.benefit_plan, is_deleted=False)

    def test_the_household_is_paid_through_its_primary_worker(self):
        payroll = self.run_payroll(self.payment_plan(), enrollments_queryset=self.household_enrollments())
        benefit = self.benefits(payroll).get()
        self.assertEqual(benefit.individual_id, self.primary_worker.individual_id)
        self.assertEqual((benefit.amount, benefit.type), (Decimal("700.00"), "CASH"))
        bill = Bill.objects.get(benefitattachment__benefit=benefit)
        self.assertEqual(str(bill.subject_id), str(self.household.id))

    def test_a_household_with_several_enrollments_is_paid_once(self):
        retake = create_training(self.user, self.benefit_plan, [self.module_1, self.module_2], 'AGHH retake')
        attend_household(self.user, self.household, retake, 3)
        payroll = self.run_payroll(self.payment_plan(), enrollments_queryset=self.household_enrollments())
        self.assertEqual(self.benefits(payroll).count(), 1)

    def test_a_household_is_not_paid_twice_under_the_plan(self):
        plan = self.payment_plan()
        self.run_payroll(plan, enrollments_queryset=self.household_enrollments())
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            second = self.run_payroll(plan, enrollments_queryset=self.household_enrollments())
        self.assertFalse(self.benefits(second).exists())
        self.assertIn(
            f"household beneficiary {self.household.id} (primary worker {self.primary_worker.individual_id}) "
            "not paid: already has a benefit under this payment plan that is not rejected",
            "\n".join(logs.output),
        )

    def test_households_without_exactly_one_primary_worker_are_excluded(self):
        nobody, _ = create_household(self.user, self.benefit_plan, 'AGHH-NONE', 0, RMEP_GRANT)
        two, _ = create_household(self.user, self.benefit_plan, 'AGHH-TWO', 2, RMEP_GRANT)
        for household in (nobody, two):
            attend_household(self.user, household, self.training, 10)
        with self.assertLogs(STRATEGY_LOGGER, level="WARNING") as logs:
            payroll = self.run_payroll(self.payment_plan(), enrollments_queryset=self.household_enrollments())
        self.assertEqual(self.benefits(payroll).count(), 1)
        output = "\n".join(logs.output)
        self.assertIn(f"household beneficiary {nobody.id} (primary worker None) not paid: "
                      "the household has no primary worker", output)
        self.assertIn(f"household beneficiary {two.id} (primary worker None) not paid: "
                      "the household has 2 primary workers", output)

    def test_household_attendance_is_its_primary_workers_attendance(self):
        short, _ = create_household(self.user, self.benefit_plan, 'AGHH-SHORT', json_ext=RMEP_GRANT)
        attend_household(self.user, short, self.training, 7)
        with self.assertLogs(STRATEGY_LOGGER, level="INFO") as logs:
            self.run_payroll(self.payment_plan(), enrollments_queryset=self.household_enrollments())
        self.assertIn(f"household beneficiary {short.id}", "\n".join(logs.output))
        self.assertIn("module 'AGHH module 2': attended 2 of 5 topics (40.0%), below 80%", "\n".join(logs.output))

    def test_an_enrollment_selection_of_another_model_is_a_configuration_error(self):
        with self.assertRaisesMessage(AttendanceGrantConfigurationError,
                                      "expects a GroupBeneficiary enrollment selection"):
            self.run_payroll(self.payment_plan(), enrollments_queryset=Bill.objects.none())
