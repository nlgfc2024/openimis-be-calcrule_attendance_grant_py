from decimal import Decimal

from django.test import TestCase

from core.test_helpers import LogInHelper
from social_protection.models import GroupBeneficiary, BeneficiaryStatus
from social_protection.tests.test_helpers import create_benefit_plan, create_group_with_individual
from project_social_protection.models import (
    GroupBeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectTimeEntry,
    BeneficiaryProjectTimeEntry,
    ProjectStatus,
)
from project_social_protection.tests.test_helpers import find_or_create_activity

from calcrule_attendance_grant.attendance import evaluate_attendance
from calcrule_attendance_grant.tests.helpers import (
    attend,
    create_individual_benefit_plan,
    create_participant,
    create_session,
    grant_parameters,
)


class EvaluateAttendanceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_individual_benefit_plan(cls.user, 'AGATT')
        cls.module_1 = find_or_create_activity("Attendance module 1", cls.user.username)
        cls.module_2 = find_or_create_activity("Attendance module 2", cls.user.username)
        cls.session_1 = create_session(cls.user, cls.benefit_plan, cls.module_1, 'Module 1 - A')
        cls.session_2 = create_session(cls.user, cls.benefit_plan, cls.module_2, 'Module 2 - A')
        cls.params = grant_parameters([cls.module_1, cls.module_2])

    def participant(self, name):
        return create_participant(self.user, self.benefit_plan, name)

    def evaluate(self, beneficiary, **overrides):
        params = grant_parameters([self.module_1, self.module_2], **overrides) if overrides else self.params
        return evaluate_attendance([beneficiary.id], self.benefit_plan, params)[beneficiary.id]

    def test_full_attendance_of_every_module_is_eligible(self):
        beneficiary = self.participant('Full')
        attend(self.user, beneficiary, self.session_1, 5)
        attend(self.user, beneficiary, self.session_2, 5)
        result = self.evaluate(beneficiary)
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(result.modules[str(self.module_1.id)].completion_percentage, 100)

    def test_the_minimum_percentage_is_inclusive(self):
        beneficiary = self.participant('Boundary')
        attend(self.user, beneficiary, self.session_1, 4)
        attend(self.user, beneficiary, self.session_2, 4)
        self.assertTrue(self.evaluate(beneficiary).eligible)

    def test_one_module_cannot_compensate_for_another(self):
        beneficiary = self.participant('Compensate')
        attend(self.user, beneficiary, self.session_1, 5)
        attend(self.user, beneficiary, self.session_2, 3)
        result = self.evaluate(beneficiary)
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason,
            "module 'Attendance module 2': attended 3 of 5 days (60.0%), below 80%",
        )

    def test_a_missing_module_enrollment_is_a_shortfall(self):
        beneficiary = self.participant('Missing')
        attend(self.user, beneficiary, self.session_1, 5)
        result = self.evaluate(beneficiary)
        self.assertEqual(
            result.reason,
            "module 'Attendance module 2': not enrolled in a completed training session",
        )

    def test_partial_days_count_only_from_the_attended_day_threshold(self):
        beneficiary = self.participant('Partial')
        attend(self.user, beneficiary, self.session_1, 5, percent_complete=50)
        attend(self.user, beneficiary, self.session_2, 5)
        self.assertFalse(self.evaluate(beneficiary).eligible)
        self.assertTrue(self.evaluate(beneficiary, attended_day_threshold=50).eligible)

    def test_deleted_and_out_of_range_entries_are_ignored(self):
        beneficiary = self.participant('Ignored')
        enrollment = attend(self.user, beneficiary, self.session_1, [1, 2, 3, 6, 7])
        entry = BeneficiaryProjectTimeEntry.objects.get(enrollment=enrollment, day_number=3)
        entry.delete(user=self.user)
        attend(self.user, beneficiary, self.session_2, 5)
        module_1 = self.evaluate(beneficiary).modules[str(self.module_1.id)]
        self.assertEqual((module_1.attended_days, module_1.scheduled_days), (2, 5))

    def test_only_completed_sessions_count_unless_the_plan_says_otherwise(self):
        beneficiary = self.participant('InProgress')
        in_progress = create_session(
            self.user, self.benefit_plan, self.module_1, 'Module 1 - running',
            status=ProjectStatus.IN_PROGRESS,
        )
        attend(self.user, beneficiary, in_progress, 5)
        attend(self.user, beneficiary, self.session_2, 5)
        self.assertIn("not enrolled in a completed training session", self.evaluate(beneficiary).reason)
        self.assertTrue(self.evaluate(beneficiary, only_completed_sessions=False).eligible)

    def test_work_projects_and_other_programmes_do_not_count(self):
        beneficiary = self.participant('Elsewhere')
        work = create_session(
            self.user, self.benefit_plan, self.module_1, 'Module 1 - work project',
            project_type='WORK',
        )
        other_plan = create_individual_benefit_plan(self.user, 'AGOTHER')
        other_session = create_session(self.user, other_plan, self.module_2, 'Other programme')
        attend(self.user, beneficiary, work, 5)
        attend(self.user, beneficiary, other_session, 5)
        result = self.evaluate(beneficiary)
        self.assertEqual(len(result.shortfalls), 2)

    def test_the_best_session_of_a_repeated_module_counts(self):
        beneficiary = self.participant('Repeated')
        retake = create_session(self.user, self.benefit_plan, self.module_1, 'Module 1 - B', working_days=4)
        attend(self.user, beneficiary, self.session_1, 1)
        attend(self.user, beneficiary, retake, 4)
        attend(self.user, beneficiary, self.session_2, 5)
        result = self.evaluate(beneficiary)
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(result.modules[str(self.module_1.id)].scheduled_days, 4)

    def test_participants_are_evaluated_independently(self):
        full = self.participant('IndependentFull')
        short = self.participant('IndependentShort')
        for beneficiary, days in ((full, 5), (short, 2)):
            attend(self.user, beneficiary, self.session_1, days)
            attend(self.user, beneficiary, self.session_2, 5)
        results = evaluate_attendance([full.id, short.id], self.benefit_plan, self.params)
        self.assertTrue(results[full.id].eligible)
        self.assertFalse(results[short.id].eligible)

    def test_group_time_entries_are_not_participant_evidence(self):
        group_plan = create_benefit_plan(
            self.user.username, payload_override={'code': 'AGGRP', 'type': 'GROUP'}
        )
        individual, group, _ = create_group_with_individual(self.user.username, {'code': 'AGGRP1'})
        group_beneficiary = GroupBeneficiary(
            group=group, benefit_plan=group_plan, status=BeneficiaryStatus.ACTIVE,
        )
        group_beneficiary.save(user=self.user)
        beneficiary = self.participant('GroupMember')
        for session in (self.session_1, self.session_2):
            group_enrollment = GroupBeneficiaryProjectEnrollment(
                group_beneficiary=group_beneficiary, project=session,
            )
            group_enrollment.save(user=self.user)
            for day_number in range(1, 6):
                GroupBeneficiaryProjectTimeEntry(
                    enrollment=group_enrollment, day_number=day_number, percent_complete=100,
                ).save(user=self.user)
        result = self.evaluate(beneficiary)
        self.assertFalse(result.eligible)
        self.assertEqual(len(result.shortfalls), 2)

    def test_fractional_minimum_in_the_reason(self):
        beneficiary = self.participant('Fraction')
        attend(self.user, beneficiary, self.session_1, 3)
        attend(self.user, beneficiary, self.session_2, 5)
        result = self.evaluate(beneficiary, minimum_attendance_percentage=Decimal("62.50"))
        self.assertEqual(
            result.reason,
            "module 'Attendance module 1': attended 3 of 5 days (60.0%), below 62.5%",
        )
        self.assertTrue(self.evaluate(beneficiary, minimum_attendance_percentage=Decimal("60")).eligible)
