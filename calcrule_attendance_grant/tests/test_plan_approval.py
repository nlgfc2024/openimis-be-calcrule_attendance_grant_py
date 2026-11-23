import datetime

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.test_helpers import LogInHelper
from social_protection.models import BeneficiaryStatus, GroupBeneficiary
from social_protection.tests.test_helpers import create_group
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.services import TaskService

from calcrule_attendance_grant.plan_approval import TASK_SOURCE, PlanApprovalService, approver_name
from calcrule_attendance_grant.tests.helpers import create_participant, create_programme
from calcrule_attendance_grant.tests.test_protected_fields import user_with

PROPOSAL = {
    "grant_types": ["EQUIPMENT", "CASH"], "approved_grant_amount": 1000,
    "tranche_1_equipment_amount": 500, "business_plan_reference": " BP-7 ",
}


class PlanApprovalTest(TestCase):
    """An RMEP-like programme whose approvals go to one task group."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.admin, 'AGAPPR', json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]})
        cls.maker = user_with("ag_pa_maker", "gql_plans_and_grants_perms")
        cls.checker = user_with("ag_pa_checker", "gql_plan_approval_perms")
        cls.both = user_with("ag_pa_both", "gql_plans_and_grants_perms", "gql_plan_approval_perms")
        cls.outsider = user_with("ag_pa_outsider", "gql_plan_approval_perms")
        cls.unentitled = user_with("ag_pa_unentitled")
        cls.group = TaskGroup(code="AG plan approvals", completion_policy="ANY",
                              json_ext={"task_sources": [TASK_SOURCE]})
        cls.group.save(user=cls.admin)
        for executor in (cls.checker, cls.both, cls.unentitled):
            TaskExecutor(user=executor, task_group=cls.group).save(user=cls.admin)

    def setUp(self):
        self.beneficiary = create_participant(self.admin, self.benefit_plan, 'Appraised', {
            "note": "keep", "business_plan_status": "REJECTED", "plan_rejection_reason": "Earlier",
        })

    def submit(self, user=None, proposal=None, beneficiary=None):
        PlanApprovalService(user or self.maker).submit(beneficiary or self.beneficiary, proposal or PROPOSAL)
        return Task.objects.get(entity_id=(beneficiary or self.beneficiary).id, status=Task.Status.ACCEPTED)

    def decide(self, user, task, decision="APPROVED", additional_data=None):
        return TaskService(user).resolve_task({
            "id": task.id, "business_status": {str(user.id): decision}, "additional_data": additional_data,
        })

    def test_a_submission_is_a_task_for_the_approval_group_and_changes_nothing_yet(self):
        task = self.submit()
        self.assertEqual(task.task_group, self.group)
        self.assertEqual(task.data["incoming_data"], {
            "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_equipment_amount": 500, "business_plan_reference": "BP-7",
        })
        self.beneficiary.refresh_from_db()
        self.assertNotIn("approved_grant_amount", self.beneficiary.json_ext)

    def test_only_users_with_the_plans_and_grants_right_can_submit(self):
        with self.assertRaisesMessage(ValidationError, "can submit them for approval"):
            PlanApprovalService(self.checker).submit(self.beneficiary, PROPOSAL)

    def test_proposals_are_checked(self):
        cases = {
            "not: business_plan_status": {"business_plan_status": "APPROVED"},
            "types the programme does not allow: SERVICE": {"grant_types": ["SERVICE"]},
            "approved_grant_amount must be more than 0": {"approved_grant_amount": 0},
            "tranche_1_cash_amount must be 0 or more, with at most two decimals": {"tranche_1_cash_amount": 1.005},
            "tranche_1_equipment_amount must be a number": {"tranche_1_equipment_amount": "500"},
        }
        for message, proposal in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    PlanApprovalService(self.maker).submit(self.beneficiary, proposal)

    def test_one_open_approval_per_beneficiary(self):
        self.submit()
        with self.assertRaises(ValidationError):
            PlanApprovalService(self.maker).submit(self.beneficiary, {"approved_grant_amount": 900})

    def test_an_executor_with_the_approval_right_approves(self):
        task = self.submit()
        self.assertTrue(self.decide(self.checker, task)["success"])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext, {
            "note": "keep", "grant_types": ["CASH", "EQUIPMENT"], "approved_grant_amount": 1000,
            "tranche_1_equipment_amount": 500, "business_plan_reference": "BP-7",
            "business_plan_status": "APPROVED", "plan_approved_on": datetime.date.today().isoformat(),
            "plan_approved_by": approver_name(self.checker), "plan_approved_by_id": str(self.checker.id),
        })

    def test_the_maker_may_approve_when_an_administrator_allows_it(self):
        task = self.submit(user=self.both)
        self.assertTrue(self.decide(self.both, task)["success"])
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["plan_approved_by_id"], str(self.both.id))

    def test_deciders_need_the_approval_right_and_a_place_in_the_task_group(self):
        task = self.submit()
        with self.assertRaisesMessage(ValidationError, "Only executors of the task group"):
            self.decide(self.outsider, task)
        with self.assertRaisesMessage(ValidationError, "right to approve beneficiary plans"):
            self.decide(self.unentitled, task)
        TaskExecutor.objects.filter(user=self.checker).update(is_deleted=True)
        with self.assertRaisesMessage(ValidationError, "Only executors of the task group"):
            self.decide(self.checker, task)
        task.refresh_from_db()
        self.assertEqual((task.status, task.business_status), (Task.Status.ACCEPTED, {}))

    def test_administrators_decide_without_being_executors(self):
        task = self.submit()
        self.assertTrue(self.decide(self.admin, task)["success"])
        self.beneficiary.refresh_from_db()
        self.assertEqual(self.beneficiary.json_ext["business_plan_status"], "APPROVED")

    def test_a_rejection_needs_a_reason_and_records_it(self):
        approved = create_participant(self.admin, self.benefit_plan, 'Reappraised', {
            "approved_grant_amount": 800, "business_plan_status": "APPROVED", "plan_approved_on": "2026-10-01",
            "plan_approved_by": "Earlier", "plan_approved_by_id": "x",
        })
        task = self.submit(beneficiary=approved)
        with self.assertRaisesMessage(ValidationError, "Give a reason for rejecting the plan."):
            self.decide(self.checker, task, "FAILED", {"reason": " "})
        self.assertTrue(self.decide(self.checker, task, "FAILED", {"reason": "Not viable"})["success"])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        approved.refresh_from_db()
        self.assertEqual(approved.json_ext, {
            "approved_grant_amount": 800, "business_plan_status": "REJECTED", "plan_rejection_reason": "Not viable",
        })

    def test_household_beneficiaries_are_approved_the_same_way(self):
        group_plan = create_programme(self.admin, 'AGAPPRG', json_ext={"allowed_grant_types": ["CASH"]},
                                      plan_type='GROUP')
        household = GroupBeneficiary(group=create_group(self.admin.username, {'code': 'AGAPPR-HH'}),
                                     benefit_plan=group_plan, status=BeneficiaryStatus.ACTIVE, json_ext={})
        household.save(user=self.admin)
        task = self.submit(proposal={"business_plan_reference": "HH-1"}, beneficiary=household)
        self.decide(self.checker, task)
        household.refresh_from_db()
        self.assertEqual(household.json_ext["business_plan_reference"], "HH-1")
        self.assertEqual(household.json_ext["business_plan_status"], "APPROVED")


class SubmitBeneficiaryPlanMutationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.admin, 'AGAPPRM', json_ext={"allowed_grant_types": ["CASH", "EQUIPMENT"]})
        cls.maker = user_with("ag_pam_maker", "gql_plans_and_grants_perms")
        cls.clerk = user_with("ag_pam_clerk")
        cls.beneficiary = create_participant(cls.admin, cls.benefit_plan, 'Submitted', {})

    def mutate(self, user, json_ext):
        from calcrule_attendance_grant.gql_mutations import SubmitBeneficiaryPlanMutation
        return SubmitBeneficiaryPlanMutation.async_mutate(
            user, id=self.beneficiary.id, is_group=False, json_ext=json_ext)

    def test_the_mutation_submits_for_approval(self):
        self.assertIsNone(self.mutate(self.maker, '{"approved_grant_amount": 1000}'))
        task = Task.objects.get(entity_id=str(self.beneficiary.id))
        self.assertEqual(task.data["incoming_data"], {"approved_grant_amount": 1000})

    def test_the_mutation_needs_the_plans_and_grants_right(self):
        self.assertIn("authentication_required", str(self.mutate(self.clerk, '{"approved_grant_amount": 1000}')))
        self.assertFalse(Task.objects.filter(entity_id=str(self.beneficiary.id)).exists())
