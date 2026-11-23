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
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.admin, 'AGPROT', json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]})
        cls.clerk = user_with("ag_clerk")
        cls.planner = user_with("ag_planner", "gql_plans_and_grants_perms", "gql_plan_approval_perms")
        cls.assessor = user_with("ag_assessor", "gql_utilization_assessment_perms")
        cls.beneficiary = create_participant(cls.admin, cls.benefit_plan, 'Protected', {**APPROVED_PLAN, "note": "a"})

    def update(self, user, json_ext, beneficiary=None):
        beneficiary = beneficiary or self.beneficiary
        return BeneficiaryService(user).update({
            "id": beneficiary.id, "benefit_plan_id": beneficiary.benefit_plan_id,
            "individual_id": beneficiary.individual_id, "status": beneficiary.status, "json_ext": json_ext,
        })

    def test_grant_and_plan_keys_change_only_through_a_plan_approval(self):
        # Even users holding both plan rights, and administrators, submit instead.
        for user in (self.clerk, self.planner, self.admin):
            with self.subTest(user=user.username):
                with self.assertRaisesMessage(ValidationError, "change only through a plan approval"):
                    self.update(user, {**APPROVED_PLAN, "approved_grant_amount": 5000, "note": "a"})
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["approved_grant_amount"], 1000)

    def test_removing_a_protected_value_is_a_change(self):
        with self.assertRaisesMessage(ValidationError, "Changed: grant_types, approved_grant_amount,"):
            self.update(self.planner, {"note": "a"})

    def test_unchanged_values_let_other_keys_be_saved_whatever_their_type(self):
        # 1000 sent back as "1000" or 1000.0, or text with spaces, is not a change.
        result = self.update(self.clerk, {
            **APPROVED_PLAN, "approved_grant_amount": "1000", "plan_approved_by": " Committee ", "note": "b",
        })
        self.assertTrue(result["success"], result)
        result = self.update(self.clerk, {**APPROVED_PLAN, "approved_grant_amount": 1000.0, "note": "c"})
        self.assertTrue(result["success"], result)
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["note"], "c")

    def test_utilization_keys_need_their_right_and_date_and_author(self):
        with self.assertRaisesMessage(ValidationError, "record utilization assessments can change: utilization_percentage"):
            self.update(self.planner, {**APPROVED_PLAN, **ASSESSMENT})
        with self.assertRaisesMessage(ValidationError, "A utilization result needs utilization_assessed_by."):
            self.update(self.assessor, {**APPROVED_PLAN, **ASSESSMENT, "utilization_assessed_by": " "})
        self.assertTrue(self.update(self.assessor, {**APPROVED_PLAN, **ASSESSMENT})["success"])

    def test_creating_a_beneficiary_with_plan_keys_is_refused(self):
        individual = create_individual(self.clerk.username, {'first_name': 'NewProtected'})
        data = {"individual_id": individual.id, "benefit_plan_id": self.benefit_plan.id,
                "status": BeneficiaryStatus.ACTIVE, "json_ext": {"approved_grant_amount": 900}}
        with self.assertRaisesMessage(ValidationError, "Changed: approved_grant_amount"):
            BeneficiaryService(self.planner).create(data)
        self.assertFalse(Beneficiary.objects.filter(individual=individual).exists())

    def test_household_beneficiaries_are_protected_too(self):
        group_plan = create_benefit_plan(self.clerk.username, {'code': 'AGPROTG', 'type': 'GROUP'})
        household = GroupBeneficiary(group=create_group(self.clerk.username, {'code': 'AGPROT-HH'}),
                                     benefit_plan=group_plan, status=BeneficiaryStatus.ACTIVE)
        household.save(user=self.clerk)
        data = {"id": household.id, "benefit_plan_id": group_plan.id, "group_id": household.group_id,
                "status": household.status, "json_ext": {"business_plan_status": "APPROVED"}}
        with self.assertRaisesMessage(ValidationError, "Changed: business_plan_status"):
            GroupBeneficiaryService(self.planner).update(data)


class GenericTaskProtectedFieldsTest(TestCase):
    """Generic beneficiary update tasks are checked when created and when approved."""

    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(admin, 'AGPROTMC', json_ext={"allowed_grant_types": ["CASH"]})
        cls.clerk = user_with("ag_mc_clerk")
        cls.assessor = user_with("ag_mc_assessor", "gql_utilization_assessment_perms")
        cls.planner = user_with("ag_mc_planner", "gql_plans_and_grants_perms", "gql_plan_approval_perms")
        cls.beneficiary = create_participant(admin, cls.benefit_plan, 'MakerChecker', {"note": "a"})

    def update_data(self, json_ext):
        return {"id": self.beneficiary.id, "benefit_plan_id": self.benefit_plan.id,
                "individual_id": self.beneficiary.individual_id, "status": self.beneficiary.status,
                "json_ext": json_ext}

    def test_a_generic_task_cannot_carry_plan_keys(self):
        result = BeneficiaryService(self.planner).create_update_task(
            self.update_data({"note": "a", "business_plan_status": "APPROVED"}))
        self.assertFalse(result["success"])
        self.assertIn("change only through a plan approval", str(result))
        self.assertFalse(Task.objects.filter(entity_id=self.beneficiary.id).exists())

    def test_an_assessment_task_needs_the_right_from_the_maker_and_each_approver(self):
        assessment = self.update_data({"note": "a", **ASSESSMENT})
        self.assertFalse(BeneficiaryService(self.clerk).create_update_task(assessment)["success"])
        self.assertTrue(BeneficiaryService(self.assessor).create_update_task(assessment)["success"])
        task = Task.objects.get(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update")
        with self.assertRaisesMessage(ValidationError, "can change: utilization_percentage"):
            TaskService(self.clerk).resolve_task({"id": task.id, "business_status": {str(self.clerk.id): "APPROVED"}})
        result = TaskService(self.assessor).resolve_task(
            {"id": task.id, "business_status": {str(self.assessor.id): "APPROVED"}})
        self.assertTrue(result["success"], result)

    def test_rejecting_a_generic_task_needs_no_right(self):
        BeneficiaryService(self.assessor).create_update_task(self.update_data({"note": "a", **ASSESSMENT}))
        task = Task.objects.get(entity_id=self.beneficiary.id, business_event="BeneficiaryService.update")
        result = TaskService(self.clerk).resolve_task({"id": task.id, "business_status": {str(self.clerk.id): "FAILED"}})
        self.assertTrue(result["success"], result)
