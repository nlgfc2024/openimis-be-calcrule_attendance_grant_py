from django.core.exceptions import ValidationError
from django.test import TestCase

from core.test_helpers import LogInHelper, create_test_interactive_user, create_test_role
from social_protection.models import Beneficiary, BeneficiaryStatus, GroupBeneficiary
from social_protection.services import BeneficiaryService, GroupBeneficiaryService
from social_protection.tests.test_helpers import create_benefit_plan, create_group, create_individual
from tasks_management.models import Task
from tasks_management.services import TaskService

from calcrule_attendance_grant.tests.helpers import create_participant, create_programme

APPROVED_PLAN = {
    "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
    "business_plan_status": "APPROVED", "plan_approved_on": "2026-10-01", "plan_approved_by": "Committee",
}
ASSESSMENT = {
    "utilization_percentage": 85, "utilization_assessed_on": "2026-11-01", "utilization_assessed_by": "Officer",
}


def user_with(username, *perm_names):
    role = create_test_role(list(perm_names), name=f"AG {username}")
    return create_test_interactive_user(username=username, roles=[role.id])


class ProtectedFieldsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(admin, 'AGPROT', json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]})
        cls.clerk = user_with("ag_clerk")
        cls.planner = user_with("ag_planner", "gql_plans_and_grants_perms")
        cls.assessor = user_with("ag_assessor", "gql_utilization_assessment_perms")
        cls.beneficiary = create_participant(admin, cls.benefit_plan, 'Protected', {**APPROVED_PLAN, "note": "a"})

    def update(self, user, json_ext, beneficiary=None):
        beneficiary = beneficiary or self.beneficiary
        return BeneficiaryService(user).update({
            "id": beneficiary.id, "benefit_plan_id": beneficiary.benefit_plan_id,
            "individual_id": beneficiary.individual_id, "status": beneficiary.status, "json_ext": json_ext,
        })

    def assert_rejected(self, user, json_ext, message):
        with self.assertRaises(ValidationError) as ctx:
            self.update(user, json_ext)
        self.assertIn(message, " ".join(ctx.exception.messages))
        self.beneficiary.refresh_from_db()

    def test_a_user_without_the_right_cannot_change_plan_or_grant_keys(self):
        self.assert_rejected(
            self.clerk, {**APPROVED_PLAN, "approved_grant_amount": 5000, "note": "a"},
            "Only users with the right to manage beneficiary plans and grants can change: approved_grant_amount.",
        )
        self.assertEqual(self.beneficiary.json_ext["approved_grant_amount"], 1000)

    def test_removing_a_protected_value_is_a_change(self):
        self.assert_rejected(self.clerk, {"note": "a"}, "can change: grant_types, approved_grant_amount,")

    def test_unchanged_protected_values_let_other_keys_be_saved(self):
        result = self.update(self.clerk, {**APPROVED_PLAN, "note": "b"})
        self.assertTrue(result["success"], result)
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["note"], "b")

    def test_each_right_covers_only_its_own_keys(self):
        result = self.update(self.planner, {**APPROVED_PLAN, "approved_grant_amount": 1200})
        self.assertTrue(result["success"], result)
        with self.assertRaisesMessage(ValidationError, "record utilization assessments can change: utilization_percentage"):
            self.update(self.planner, {**APPROVED_PLAN, "approved_grant_amount": 1200, **ASSESSMENT})
        result = self.update(self.assessor, {**APPROVED_PLAN, "approved_grant_amount": 1200, **ASSESSMENT})
        self.assertTrue(result["success"], result)

    def test_an_approved_plan_and_a_utilization_result_need_their_date_and_author(self):
        with self.assertRaisesMessage(ValidationError, "An APPROVED plan needs plan_approved_on and plan_approved_by."):
            self.update(self.planner, {**APPROVED_PLAN, "plan_approved_on": "", "plan_approved_by": None})
        with self.assertRaisesMessage(ValidationError, "A utilization result needs utilization_assessed_by."):
            self.update(self.assessor, {**APPROVED_PLAN, **ASSESSMENT, "utilization_assessed_by": " "})

    def test_creating_a_beneficiary_with_protected_keys_needs_the_right(self):
        individual = create_individual(self.clerk.username, {'first_name': 'NewProtected'})
        data = {"individual_id": individual.id, "benefit_plan_id": self.benefit_plan.id,
                "status": BeneficiaryStatus.ACTIVE, "json_ext": {"approved_grant_amount": 900}}
        with self.assertRaisesMessage(ValidationError, "can change: approved_grant_amount"):
            BeneficiaryService(self.clerk).create(data)
        self.assertFalse(Beneficiary.objects.filter(individual=individual).exists())
        self.assertTrue(BeneficiaryService(self.planner).create(data)["success"])

    def test_household_beneficiaries_are_protected_too(self):
        group_plan = create_benefit_plan(self.clerk.username, {'code': 'AGPROTG', 'type': 'GROUP'})
        household = GroupBeneficiary(group=create_group(self.clerk.username, {'code': 'AGPROT-HH'}),
                                     benefit_plan=group_plan, status=BeneficiaryStatus.ACTIVE)
        household.save(user=self.clerk)
        data = {"id": household.id, "benefit_plan_id": group_plan.id, "group_id": household.group_id,
                "status": household.status, "json_ext": {"business_plan_status": "PENDING"}}
        with self.assertRaisesMessage(ValidationError, "can change: business_plan_status"):
            GroupBeneficiaryService(self.clerk).update(data)
        self.assertTrue(GroupBeneficiaryService(self.planner).update(data)["success"])

    def test_admins_hold_every_right(self):
        admin = LogInHelper().get_or_create_user_api()
        self.assertTrue(self.update(admin, {**APPROVED_PLAN, **ASSESSMENT})["success"])


class MakerCheckerProtectedFieldsTest(TestCase):
    """With maker-checker, the maker is checked when the update task is created and
    each approver when they approve it, before the task runs as that approver."""

    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(admin, 'AGPROTMC', json_ext={"allowed_grant_types": ["CASH"]})
        cls.clerk = user_with("ag_mc_clerk")
        cls.planner = user_with("ag_mc_planner", "gql_plans_and_grants_perms")
        cls.beneficiary = create_participant(admin, cls.benefit_plan, 'MakerChecker', {"note": "a"})

    def update_data(self, json_ext):
        return {"id": self.beneficiary.id, "benefit_plan_id": self.benefit_plan.id,
                "individual_id": self.beneficiary.individual_id, "status": self.beneficiary.status,
                "json_ext": json_ext}

    def create_task(self, maker):
        return BeneficiaryService(maker).create_update_task(self.update_data({"note": "a", "business_plan_status": "PENDING"}))

    def test_a_maker_without_the_right_cannot_request_the_change(self):
        result = self.create_task(self.clerk)
        self.assertFalse(result["success"])
        self.assertIn("can change: business_plan_status", str(result))
        self.assertFalse(Task.objects.filter(entity_id=self.beneficiary.id).exists())

    def test_a_maker_with_the_right_can_request_it_and_only_an_approver_with_it_can_approve(self):
        result = self.create_task(self.planner)
        self.assertTrue(result["success"], result)
        task = Task.objects.get(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update")

        with self.assertRaisesMessage(ValidationError, "can change: business_plan_status"):
            TaskService(self.clerk).resolve_task({"id": task.id, "business_status": {str(self.clerk.id): "APPROVED"}})
        task.refresh_from_db()
        self.assertEqual(task.business_status, {})

        result = TaskService(self.planner).resolve_task(
            {"id": task.id, "business_status": {str(self.planner.id): "APPROVED"}})
        self.assertTrue(result["success"], result)

    def test_rejecting_a_task_needs_no_right(self):
        self.create_task(self.planner)
        task = Task.objects.get(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update")
        result = TaskService(self.clerk).resolve_task({"id": task.id, "business_status": {str(self.clerk.id): "FAILED"}})
        self.assertTrue(result["success"], result)

    def test_the_approved_task_runs_as_the_approver_and_is_checked_again(self):
        with self.assertRaisesMessage(ValidationError, "can change: business_plan_status"):
            BeneficiaryService(self.clerk).update(self.update_data({"note": "a", "business_plan_status": "PENDING"}))
        self.assertTrue(BeneficiaryService(self.planner).update(
            self.update_data({"note": "a", "business_plan_status": "PENDING"}))["success"])
