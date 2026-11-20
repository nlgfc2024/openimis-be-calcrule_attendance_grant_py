from unittest.mock import patch

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import IndividualDataSource, IndividualDataSourceUpload
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BeneficiaryStatus, GroupBeneficiary
from social_protection.tests.test_helpers import create_group
from tasks_management.models import Task
from workflow.exceptions import PythonWorkflowHandlerException
from workflow.systems.python import PythonWorkflowAdaptor

from calcrule_attendance_grant.tests.helpers import create_participant, create_programme
from calcrule_attendance_grant.tests.test_protected_fields import user_with
from calcrule_attendance_grant.upload import (
    WORKFLOW_GROUP,
    WORKFLOW_NAME,
    process_grant_and_utilization_update,
)

APPROVED = {"business_plan_status": "APPROVED", "plan_approved_on": "2026-10-01", "plan_approved_by": "Committee"}


@patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", False)
class GrantUtilizationUploadTest(TestCase):
    """An RMEP-like INDIVIDUAL programme that allows cash and equipment."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.admin, 'AGUPL', json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]})
        cls.beneficiary = create_participant(cls.admin, cls.benefit_plan, 'Uploaded', {"note": "keep"})
        cls.other = create_participant(cls.admin, cls.benefit_plan, 'Other', {**APPROVED, "approved_grant_amount": 900})
        cls.clerk = user_with("ag_upl_clerk")
        cls.assessor = user_with("ag_upl_assessor", "gql_utilization_assessment_perms")

    def upload(self, *rows, user=None):
        user = user or self.admin
        upload = IndividualDataSourceUpload(source_name="grants.csv", source_type="beneficiary import")
        upload.save(username=user.login_name)
        for row in rows:
            IndividualDataSource(upload=upload, json_ext=row, validations={}).save(username=user.login_name)
        summary = process_grant_and_utilization_update(user.id, self.benefit_plan.id, upload.id)
        upload.refresh_from_db()
        return upload, summary

    def errors(self, upload):
        return {
            str(row.json_ext.get("ID")): row.validations.get("validation_errors")
            for row in IndividualDataSource.objects.filter(upload=upload)
        }

    def test_the_workflow_is_offered_in_the_beneficiary_upload_dialog_group(self):
        workflows = PythonWorkflowAdaptor.get_workflows(WORKFLOW_NAME, WORKFLOW_GROUP)["data"]["workflows"]
        self.assertEqual([workflow.name for workflow in workflows], [WORKFLOW_NAME])

    def test_only_the_submitted_keys_are_merged_as_numbers_and_lists(self):
        upload, summary = self.upload({
            "ID": str(self.beneficiary.id), "grant_types": "cash; EQUIPMENT", "approved_grant_amount": "1000",
            "tranche_1_cash_amount": 200, "tranche_1_equipment_amount": 500.0, **APPROVED,
        })
        self.assertEqual((upload.status, summary), ("SUCCESS", {"applied": 1, "tasks": 0, "invalid": 0}))
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {
            "note": "keep", "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_cash_amount": 200, "tranche_1_equipment_amount": 500, **APPROVED,
        })
        self.assertTrue(self.beneficiary.history.filter(json_ext__approved_grant_amount=1000).exists())

    def test_blank_cells_do_not_clear_recorded_values(self):
        self.upload({"ID": str(self.other.id), "approved_grant_amount": None, "business_plan_reference": " ",
                     "plan_approved_by": "Board"})
        self.other.refresh_from_db()
        self.assertEqual(self.other.json_ext["approved_grant_amount"], 900)
        self.assertEqual(self.other.json_ext["plan_approved_by"], "Board")
        self.assertNotIn("business_plan_reference", self.other.json_ext)

    def test_unknown_or_missing_id_columns_fail_the_whole_upload(self):
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "Unknown columns: first_name."):
            self.upload({"ID": str(self.beneficiary.id), "first_name": "X", "approved_grant_amount": 1000})
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "needs an ID column"):
            self.upload({"approved_grant_amount": 1000})
        self.beneficiary.refresh_from_db()
        self.assertNotIn("approved_grant_amount", self.beneficiary.json_ext)

    def test_columns_need_the_uploaders_rights(self):
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "plan and grant columns need the right"):
            self.upload({"ID": str(self.beneficiary.id), "approved_grant_amount": 1000}, user=self.assessor)
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "utilization columns need the right"):
            self.upload({"ID": str(self.beneficiary.id), "utilization_percentage": 85}, user=self.clerk)

        upload, _ = self.upload({
            "ID": str(self.beneficiary.id), "utilization_percentage": "85.5", "utilization_assessed_on": "2026-11-02",
            "utilization_assessed_by": "Officer",
        }, user=self.assessor)
        self.assertEqual(upload.status, "SUCCESS")
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["utilization_percentage"], 85.5)

    def test_invalid_rows_are_reported_and_valid_rows_applied(self):
        unknown = "00000000-0000-0000-0000-000000000001"
        upload, summary = self.upload(
            {"ID": str(self.beneficiary.id), "approved_grant_amount": 1000.5, "business_plan_status": "pending"},
            {"ID": unknown, "approved_grant_amount": 1000},
            {"ID": str(self.other.id), "approved_grant_amount": "10.555", "tranche_1_cash_amount": -1,
             "business_plan_status": "DONE", "plan_approved_on": "01/10/2026", "grant_types": "CASH;SERVICE"},
        )
        self.assertEqual((upload.status, summary["applied"], summary["invalid"]), ("PARTIAL_SUCCESS", 1, 2))
        errors = self.errors(upload)
        self.assertEqual(errors[str(self.beneficiary.id)], [])
        self.assertEqual(errors[unknown], [{"field_name": "ID", "note": f"'{unknown}' is not a beneficiary of this programme"}])
        self.assertEqual({error["field_name"] for error in errors[str(self.other.id)]}, {
            "approved_grant_amount", "tranche_1_cash_amount", "business_plan_status", "plan_approved_on", "grant_types",
        })
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["business_plan_status"], "PENDING")
        self.assertEqual(IndividualDataSource.objects.filter(upload=upload).exclude(
            validations__validation_errors=[]).count(), 2)

    def test_approval_and_assessment_need_their_date_and_author(self):
        upload, _ = self.upload(
            {"ID": str(self.beneficiary.id), "business_plan_status": "APPROVED", "plan_approved_on": "2026-10-01"},
            {"ID": str(self.other.id), "utilization_confirmed": "maybe"},
        )
        errors = self.errors(upload)
        self.assertEqual(errors[str(self.beneficiary.id)][0]["note"], "An APPROVED plan needs plan_approved_by.")
        self.assertIn("must be one of YES, NO", errors[str(self.other.id)][0]["note"])

    def test_tranche_amounts_follow_the_grant_types(self):
        upload, _ = self.upload(
            {"ID": str(self.beneficiary.id), "grant_types": "EQUIPMENT", "approved_grant_amount": 800,
             "tranche_1_cash_amount": 100, "tranche_2_equipment_amount": 100},
            {"ID": str(self.other.id), "grant_types": "CASH;EQUIPMENT", "tranche_1_cash_amount": 600},
        )
        errors = self.errors(upload)
        self.assertEqual({error["note"] for error in errors[str(self.beneficiary.id)]}, {
            "is not zero but CASH is not one of the grant types", "a grant without cash has no second tranche",
        })
        # 600 of a 900 grant is not the default 70% (630): a warning, and the row still applies.
        row = IndividualDataSource.objects.get(upload=upload, json_ext__ID=str(self.other.id))
        self.assertEqual(row.validations["validation_errors"], [])
        self.assertIn("tranche-one amounts add up to 600.00, not the default 70% share of 900 (630.00)",
                      row.validations["validation_warnings"][0]["note"])
        self.other.refresh_from_db()
        self.assertEqual(self.other.json_ext["tranche_1_cash_amount"], 600)

    def test_spreadsheet_dates_are_stored_as_iso_dates(self):
        self.upload({"ID": str(self.beneficiary.id), "plan_approved_on": 1790812800000})
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["plan_approved_on"], "2026-10-01")

    def test_with_maker_checker_an_individual_row_becomes_an_update_task(self):
        # Inside the test: a method-level patch would be undone by the class-level one.
        with patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", True):
            upload, summary = self.upload({"ID": str(self.beneficiary.id), "business_plan_reference": "BP-1"})
        self.assertEqual((upload.status, summary["tasks"]), ("WAITING_FOR_VERIFICATION", 1))
        self.assertTrue(Task.objects.filter(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update").exists())
        self.beneficiary.refresh_from_db()
        self.assertNotIn("business_plan_reference", self.beneficiary.json_ext)


@patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", True)
class HouseholdGrantUploadTest(TestCase):
    def test_household_rows_update_the_group_beneficiary_directly(self):
        admin = LogInHelper().get_or_create_user_api()
        benefit_plan = create_programme(admin, 'AGUPLG', json_ext={"allowed_grant_types": ["CASH"]}, plan_type='GROUP')
        household = GroupBeneficiary(group=create_group(admin.username, {'code': 'AGUPL-HH'}),
                                     benefit_plan=benefit_plan, status=BeneficiaryStatus.ACTIVE, json_ext={"note": "keep"})
        household.save(user=admin)
        upload = IndividualDataSourceUpload(source_name="grants.csv", source_type="beneficiary import")
        upload.save(username=admin.login_name)
        IndividualDataSource(upload=upload, json_ext={"ID": str(household.id), **APPROVED}, validations={}).save(
            username=admin.login_name)

        self.assertEqual(process_grant_and_utilization_update(admin.id, benefit_plan.id, upload.id)["applied"], 1)
        household.refresh_from_db()
        self.assertEqual(household.json_ext, {"note": "keep", **APPROVED})
