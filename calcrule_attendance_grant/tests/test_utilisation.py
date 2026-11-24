import datetime
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.test_helpers import LogInHelper
from social_protection.apps import SocialProtectionConfig
from payroll.models import BenefitConsumptionStatus
from social_protection.models import Beneficiary, BeneficiaryStatus
from tasks_management.models import Task

from calcrule_attendance_grant.plan_approval import approver_name
from calcrule_attendance_grant.tests.helpers import create_participant, create_programme
from calcrule_attendance_grant.tests.test_protected_fields import user_with
from calcrule_attendance_grant.utilisation import record_utilisation


@patch.object(SocialProtectionConfig, "gql_check_beneficiary_crud", False)
class RecordUtilisationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(admin, 'AGUTIL', json_ext={"allowed_grant_types": ["CASH"]})
        cls.assessor = user_with("ag_util_assessor", "gql_utilization_assessment_perms")
        cls.clerk = user_with("ag_util_clerk")

    def setUp(self):
        self.beneficiary = create_participant(self.assessor, self.benefit_plan, 'Assessed', {"note": "keep"})
        # Tranche one paid in full; TrancheTwoTest records an assessment against real benefits.
        self.tranche_one = [{"grant_component": "cash", "grant_types": ["CASH"], "grant_amount": 1000.0,
                             "tranche_share": 700.0, "cash_amount": 700.0,
                             "_status": BenefitConsumptionStatus.RECONCILED}]
        patcher = patch("calcrule_attendance_grant.strategy.tranche_benefits",
                        side_effect=lambda *args: {str(self.beneficiary.id): self.tranche_one})
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, values, user=None):
        return record_utilisation(user or self.assessor, Beneficiary, self.beneficiary.id, values)

    def test_the_assessment_is_merged_into_the_current_record_and_signed(self):
        # Changed after the page loaded: kept, as is the record's own status.
        Beneficiary.objects.filter(id=self.beneficiary.id).update(
            json_ext={"note": "keep", "kyc": "approved"}, status=BeneficiaryStatus.GRADUATED)
        self.assertFalse(self.record({"utilization_percentage": 85.5, "utilization_notes": " Fine "}))
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.status, BeneficiaryStatus.GRADUATED)
        self.assertEqual(self.beneficiary.json_ext, {
            "note": "keep", "kyc": "approved", "utilization_percentage": 85.5, "utilization_notes": "Fine",
            "utilization_assessed_on": datetime.date.today().isoformat(),
            "utilization_assessed_by": approver_name(self.assessor),
        })

    def test_clearing_the_result_removes_the_assessment_date_and_author(self):
        self.record({"utilization_percentage": 80})
        self.record({"utilization_percentage": None})
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {"note": "keep"})

    def test_an_assessment_waits_for_tranche_one_to_be_paid_in_full(self):
        self.tranche_one[0]["_status"] = BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
        with self.assertRaisesMessage(
                ValidationError, "The utilization can be assessed only once tranche one is paid in full: "
                                 "tranche one is not reconciled yet (1 of 1 part(s) not RECONCILED)."):
            self.record({"utilization_percentage": 80})
        self.tranche_one.clear()
        with self.assertRaisesMessage(ValidationError, "no tranche-one payment of the programme"):
            self.record({"utilization_confirmed": "YES"})
        # Clearing an assessment is allowed: it adds nothing tranche two would count.
        Beneficiary.objects.filter(id=self.beneficiary.id).update(json_ext={"utilization_percentage": 80})
        self.record({"utilization_percentage": None})
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {})

    def test_values_and_rights_are_checked(self):
        cases = {
            "must be a number from 0 to 100": {"utilization_percentage": 120},
            "must be YES or NO": {"utilization_confirmed": "maybe"},
            "not: approved_grant_amount": {"approved_grant_amount": 1000},
        }
        for message, values in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    self.record(values)
        with self.assertRaisesMessage(ValidationError, "record utilization assessments can change"):
            self.record({"utilization_percentage": 80}, user=self.clerk)

    def test_with_maker_checker_the_assessment_becomes_an_update_task(self):
        with patch.object(SocialProtectionConfig, "gql_check_beneficiary_crud", True):
            self.assertTrue(self.record({"utilization_confirmed": "yes"}))
        task = Task.objects.get(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update")
        self.assertEqual(task.data["incoming_data"]["json_ext"]["utilization_confirmed"], "YES")
        self.beneficiary.refresh_from_db()
        self.assertNotIn("utilization_confirmed", self.beneficiary.json_ext)

    def test_the_mutation_needs_the_utilization_right(self):
        from calcrule_attendance_grant.gql_mutations import RecordUtilisationAssessmentMutation

        self.assertIn("authentication_required", str(RecordUtilisationAssessmentMutation.async_mutate(
            self.clerk, id=self.beneficiary.id, is_group=False, json_ext='{"utilization_percentage": 80}')))
        self.assertIsNone(RecordUtilisationAssessmentMutation.async_mutate(
            self.assessor, id=self.beneficiary.id, is_group=False, json_ext='{"utilization_percentage": 80}'))
