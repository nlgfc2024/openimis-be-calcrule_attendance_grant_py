import datetime
from unittest.mock import patch

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import IndividualDataSource, IndividualDataSourceUpload
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BeneficiaryStatus, GroupBeneficiary
from social_protection.tests.test_helpers import create_group
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.services import TaskService
from workflow.exceptions import PythonWorkflowHandlerException
from workflow.systems.python import PythonWorkflowAdaptor

from calcrule_attendance_grant.plan_approval import (
    TASK_SOURCE,
    UPLOAD_BUSINESS_EVENT,
    PlanApprovalService,
    beneficiary_label,
)
from calcrule_attendance_grant.tests.helpers import create_participant, create_programme
from calcrule_attendance_grant.tests.test_protected_fields import user_with
from calcrule_attendance_grant.upload import (
    WORKFLOW_GROUP,
    WORKFLOW_NAME,
    process_grant_and_utilization_update,
)

ASSESSED = {"utilization_assessed_on": "2026-11-02", "utilization_assessed_by": "Officer"}


def upload_rows(user, benefit_plan, *rows):
    upload = IndividualDataSourceUpload(source_name="grants.csv", source_type="beneficiary import")
    upload.save(username=user.login_name)
    for row in rows:
        IndividualDataSource(upload=upload, json_ext=row, validations={}).save(username=user.login_name)
    summary = process_grant_and_utilization_update(user.id, benefit_plan.id, upload.id)
    upload.refresh_from_db()
    return upload, summary


def row_errors(upload):
    return {
        str(row.json_ext.get("ID")): row.validations.get("validation_errors")
        for row in IndividualDataSource.objects.filter(upload=upload)
    }


@patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", False)
class GrantUploadTest(TestCase):
    """Grant files go for approval as one task; an RMEP-like INDIVIDUAL programme."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.admin, 'AGUPL', json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]})
        cls.maker = user_with("ag_upl_maker", "gql_plans_and_grants_perms")
        cls.checker = user_with("ag_upl_checker", "gql_plan_approval_perms")
        cls.assessor = user_with("ag_upl_assessor", "gql_utilization_assessment_perms")
        group = TaskGroup(code="AG upload approvals", completion_policy="ANY", json_ext={"task_sources": [TASK_SOURCE]})
        group.save(user=cls.admin)
        TaskExecutor(user=cls.checker, task_group=group).save(user=cls.admin)

    def setUp(self):
        self.beneficiary = create_participant(self.admin, self.benefit_plan, 'Uploaded', {"note": "keep"})
        self.other = create_participant(self.admin, self.benefit_plan, 'Other', {"approved_grant_amount": 900})

    def upload(self, *rows, user=None):
        return upload_rows(user or self.maker, self.benefit_plan, *rows)

    def task(self, upload):
        return Task.objects.get(entity_id=str(upload.id), business_event=UPLOAD_BUSINESS_EVENT)

    def decide(self, task, decision="APPROVED", additional_data=None):
        return TaskService(self.checker).resolve_task({
            "id": task.id, "business_status": {str(self.checker.id): decision}, "additional_data": additional_data,
        })

    def test_the_workflow_is_offered_in_the_beneficiary_upload_dialog_group(self):
        workflows = PythonWorkflowAdaptor.get_workflows(WORKFLOW_NAME, WORKFLOW_GROUP)["data"]["workflows"]
        self.assertEqual([workflow.name for workflow in workflows], [WORKFLOW_NAME])

    def test_a_grant_file_becomes_one_approval_task_and_changes_nothing_yet(self):
        upload, summary = self.upload(
            {"ID": str(self.beneficiary.id), "grant_types": "cash; EQUIPMENT", "approved_grant_amount": "1000",
             "tranche_1_equipment_amount": 500.0, "business_plan_reference": "BP-1"},
            {"ID": str(self.other.id), "business_plan_reference": 77},
        )
        self.assertEqual((upload.status, summary), ("WAITING_FOR_VERIFICATION", {"applied": 0, "tasks": 1, "invalid": 0}))
        task = self.task(upload)
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual({row["id"]: row["incoming_data"] for row in task.data["rows"]}, {
            str(self.beneficiary.id): {"grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
                                       "tranche_1_equipment_amount": 500, "business_plan_reference": "BP-1"},
            str(self.other.id): {"business_plan_reference": "77"},
        })
        self.assertEqual(task.data["rows"][0]["label"], beneficiary_label(self.beneficiary))
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {"note": "keep"})

    def test_approving_the_file_records_every_row_as_approved(self):
        upload, _ = self.upload(
            {"ID": str(self.beneficiary.id), "grant_types": "CASH", "approved_grant_amount": 1000},
            {"ID": str(self.other.id), "business_plan_reference": "BP-2", "approved_grant_amount": None},
        )
        self.assertTrue(self.decide(self.task(upload))["success"])
        upload.refresh_from_db()
        self.assertEqual(upload.status, "SUCCESS")
        self.other.refresh_from_db()
        # A blank cell is not submitted, so the recorded amount stays.
        self.assertEqual(self.other.json_ext, {
            "approved_grant_amount": 900, "business_plan_reference": "BP-2", "business_plan_status": "APPROVED",
            "plan_approved_on": datetime.date.today().isoformat(), "plan_approved_by": self.other.json_ext["plan_approved_by"],
            "plan_approved_by_id": str(self.checker.id),
        })
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["grant_types"], ["CASH"])

    def test_rejecting_the_file_records_the_reason_on_every_row(self):
        upload, _ = self.upload({"ID": str(self.beneficiary.id), "approved_grant_amount": 1000})
        self.assertTrue(self.decide(self.task(upload), "FAILED", {"reason": "Amounts not agreed"})["success"])
        upload.refresh_from_db()
        self.assertEqual(upload.status, "FAIL")
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {
            "note": "keep", "business_plan_status": "REJECTED", "plan_rejection_reason": "Amounts not agreed",
        })

    def test_decision_columns_mixed_files_and_unknown_columns_fail_the_whole_upload(self):
        cases = {
            "Remove the columns: business_plan_status": {"business_plan_status": "APPROVED"},
            "in separate files": {"approved_grant_amount": 1000, "utilization_percentage": 80, **ASSESSED},
            "Unknown columns: first_name.": {"first_name": "X"},
        }
        for message, columns in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesMessage(PythonWorkflowHandlerException, message):
                    self.upload({"ID": str(self.beneficiary.id), **columns})
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "needs an ID column"):
            self.upload({"approved_grant_amount": 1000})
        self.assertFalse(Task.objects.filter(business_event=UPLOAD_BUSINESS_EVENT).exists())

    def test_columns_need_the_uploaders_rights(self):
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "grant columns need the right"):
            self.upload({"ID": str(self.beneficiary.id), "approved_grant_amount": 1000}, user=self.assessor)
        with self.assertRaisesMessage(PythonWorkflowHandlerException, "utilization columns need the right"):
            self.upload({"ID": str(self.beneficiary.id), "utilization_percentage": 85, **ASSESSED}, user=self.maker)

    def test_invalid_repeated_and_waiting_rows_are_reported_and_left_out(self):
        waiting = create_participant(self.admin, self.benefit_plan, 'Waiting', {})
        PlanApprovalService(self.maker).submit(waiting, {"approved_grant_amount": 500})
        unknown = "00000000-0000-0000-0000-000000000001"
        upload, summary = self.upload(
            {"ID": str(self.beneficiary.id), "approved_grant_amount": 1000.5},
            {"ID": unknown, "approved_grant_amount": 1000},
            {"ID": str(self.other.id), "approved_grant_amount": 1000},
            {"ID": str(self.other.id), "approved_grant_amount": 1200},
            {"ID": str(waiting.id), "approved_grant_amount": 700},
        )
        self.assertEqual((upload.status, summary["tasks"], summary["invalid"]), ("WAITING_FOR_VERIFICATION", 1, 4))
        errors = row_errors(upload)
        self.assertEqual(errors[str(self.beneficiary.id)], [])
        self.assertEqual(errors[unknown], [{"field_name": "ID", "note": f"'{unknown}' is not a beneficiary of this programme"}])
        self.assertEqual(errors[str(self.other.id)][0]["note"], "appears in more than one row of the file")
        self.assertEqual(errors[str(waiting.id)][0]["note"], "already has a plan approval waiting for a decision")
        self.assertEqual([row["id"] for row in self.task(upload).data["rows"]], [str(self.beneficiary.id)])
        # The beneficiary is waiting on this file now, so the form cannot submit it too.
        with self.assertRaisesMessage(Exception, "already has a plan approval waiting"):
            PlanApprovalService(self.maker).submit(self.beneficiary, {"approved_grant_amount": 1100})
        # Approving leaves the upload partly successful.
        self.decide(self.task(upload))
        upload.refresh_from_db()
        self.assertEqual(upload.status, "PARTIAL_SUCCESS")

    def test_rows_are_checked(self):
        upload, summary = self.upload(
            {"ID": str(self.beneficiary.id), "grant_types": "EQUIPMENT", "tranche_1_cash_amount": 100,
             "tranche_2_equipment_amount": 100},
            {"ID": str(self.other.id), "tranche_1_cash_amount": -1, "grant_types": "CASH;SERVICE"},
        )
        self.assertEqual((upload.status, summary["invalid"]), ("FAIL", 2))
        errors = row_errors(upload)
        self.assertEqual({error["note"] for error in errors[str(self.beneficiary.id)]}, {
            "is not zero but CASH is not one of the grant types", "a grant without cash has no second tranche",
        })
        self.assertEqual({error["field_name"] for error in errors[str(self.other.id)]},
                         {"tranche_1_cash_amount", "grant_types"})

    def test_a_row_without_grant_details_is_reported(self):
        upload, _ = self.upload({"ID": str(self.beneficiary.id), "approved_grant_amount": " "},
                                {"ID": str(self.other.id), "business_plan_reference": "BP-3"})
        self.assertEqual(row_errors(upload)[str(self.beneficiary.id)][0]["note"], "has no grant details to submit")


@patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", False)
class UtilizationUploadTest(TestCase):
    """Utilization files are saved directly through the beneficiary services."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.admin, 'AGUPLU', json_ext={"allowed_grant_types": ["CASH"]})
        cls.assessor = user_with("ag_uplu_assessor", "gql_utilization_assessment_perms")

    def setUp(self):
        self.beneficiary = create_participant(self.admin, self.benefit_plan, 'Assessed', {"note": "keep"})

    def test_an_assessment_is_saved_with_its_numbers_and_dates(self):
        upload, summary = upload_rows(self.assessor, self.benefit_plan, {
            "ID": str(self.beneficiary.id), "utilization_percentage": "85.5", "utilization_assessed_on": 45980,
            "utilization_assessed_by": "Officer", "utilization_notes": None,
        })
        self.assertEqual((upload.status, summary), ("SUCCESS", {"applied": 1, "tasks": 0, "invalid": 0}))
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {
            "note": "keep", "utilization_percentage": 85.5, "utilization_assessed_on": "2025-11-19",
            "utilization_assessed_by": "Officer",
        })

    def test_spreadsheet_dates_stored_as_epoch_milliseconds_are_converted(self):
        upload_rows(self.assessor, self.benefit_plan, {
            "ID": str(self.beneficiary.id), "utilization_confirmed": "yes",
            "utilization_assessed_on": 1790812800000, "utilization_assessed_by": "Officer",
        })
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["utilization_assessed_on"], "2026-10-01")
        self.assertEqual(self.beneficiary.json_ext["utilization_confirmed"], "YES")

    def test_an_assessment_needs_its_date_and_author(self):
        upload, _ = upload_rows(self.assessor, self.benefit_plan,
                                {"ID": str(self.beneficiary.id), "utilization_percentage": 80})
        self.assertIn("A utilization result needs utilization_assessed_on and utilization_assessed_by.",
                      row_errors(upload)[str(self.beneficiary.id)][0]["note"])

    def test_with_maker_checker_an_individual_row_becomes_an_update_task(self):
        # Inside the test: a method-level patch would be undone by the class-level one.
        with patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", True):
            upload, summary = upload_rows(self.assessor, self.benefit_plan, {
                "ID": str(self.beneficiary.id), "utilization_percentage": 90, **ASSESSED})
        self.assertEqual((upload.status, summary["tasks"]), ("WAITING_FOR_VERIFICATION", 1))
        self.assertTrue(Task.objects.filter(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update").exists())
        self.beneficiary.refresh_from_db()
        self.assertNotIn("utilization_percentage", self.beneficiary.json_ext)

    def test_household_rows_are_saved_directly(self):
        group_plan = create_programme(self.admin, 'AGUPLG', json_ext={"allowed_grant_types": ["CASH"]}, plan_type='GROUP')
        household = GroupBeneficiary(group=create_group(self.admin.username, {'code': 'AGUPL-HH'}),
                                     benefit_plan=group_plan, status=BeneficiaryStatus.ACTIVE, json_ext={"note": "keep"})
        household.save(user=self.admin)
        with patch.object(SocialProtectionConfig, "enable_maker_checker_for_beneficiary_update", True):
            _, summary = upload_rows(self.assessor, group_plan, {
                "ID": str(household.id), "utilization_percentage": 80, **ASSESSED})
        self.assertEqual(summary["applied"], 1)
        household.refresh_from_db()
        self.assertEqual(household.json_ext, {"note": "keep", "utilization_percentage": 80, **ASSESSED})
