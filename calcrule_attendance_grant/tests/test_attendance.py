from decimal import Decimal

from django.test import TestCase

from core.test_helpers import LogInHelper
from project_social_protection.models import BeneficiaryProjectTimeEntry, ProjectStatus

from calcrule_attendance_grant.attendance import evaluate_attendance
from calcrule_attendance_grant.tests.helpers import (
    attend,
    create_programme,
    create_module,
    create_participant,
    create_training,
)


class EvaluateAttendanceTest(TestCase):
    """An RMEP-like programme: module A has 5 topics and module B has 10."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_programme(cls.user, 'AGATT')
        cls.module_a = create_module(cls.user, cls.benefit_plan, "Attendance module A", 5, "AGA")
        cls.module_b = create_module(cls.user, cls.benefit_plan, "Attendance module B", 10, "AGB")
        cls.training = create_training(cls.user, cls.benefit_plan, [cls.module_a, cls.module_b], 'Training 1')

    def participant(self, name):
        return create_participant(self.user, self.benefit_plan, name)

    def evaluate(self, beneficiary, minimum=Decimal("80"), completed_only=False):
        return evaluate_attendance(
            [beneficiary.id], self.benefit_plan, minimum, completed_only,
        )[str(beneficiary.id)]

    def test_every_module_meeting_the_minimum_is_eligible(self):
        beneficiary = self.participant('Full')
        attend(self.user, beneficiary, self.training, list(range(1, 5)) + list(range(6, 14)))
        result = self.evaluate(beneficiary)
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(
            [(m.module_name, m.ticked_topics, m.total_topics) for m in result.modules.values()],
            [("Attendance module A", 4, 5), ("Attendance module B", 8, 10)],
        )

    def test_one_module_cannot_compensate_for_another(self):
        beneficiary = self.participant('Compensate')
        # All of module A and 7 of module B's 10 topics: 12 of 15 overall is 80%.
        attend(self.user, beneficiary, self.training, 12)
        result = self.evaluate(beneficiary)
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason,
            "module 'Attendance module B': attended 7 of 10 topics (70.0%), below 80%",
        )

    def test_unticked_and_deleted_ticks_do_not_count(self):
        beneficiary = self.participant('Unticked')
        enrollment = attend(self.user, beneficiary, self.training, 15)
        BeneficiaryProjectTimeEntry.objects.filter(enrollment=enrollment, day_number=1).update(percent_complete=0)
        BeneficiaryProjectTimeEntry.objects.get(enrollment=enrollment, day_number=2).delete(user=self.user)
        module_a = self.evaluate(beneficiary).modules[str(self.module_a.id)]
        self.assertEqual(module_a.ticked_topics, 3)

    def test_no_training_enrollment_is_a_shortfall(self):
        result = self.evaluate(self.participant('NotEnrolled'))
        self.assertEqual(result.reason, "not enrolled in a training of the programme")

    def test_training_status_does_not_matter_by_default(self):
        beneficiary = self.participant('InProgress')
        running = create_training(
            self.user, self.benefit_plan, [self.module_a, self.module_b], 'Training running',
            status=ProjectStatus.IN_PROGRESS,
        )
        attend(self.user, beneficiary, running, 15)
        self.assertTrue(self.evaluate(beneficiary).eligible)

    def test_only_completed_trainings_count_when_the_programme_says_so(self):
        beneficiary = self.participant('UmepRunning')
        running = create_training(
            self.user, self.benefit_plan, [self.module_a, self.module_b], 'Training running 2',
            status=ProjectStatus.IN_PROGRESS,
        )
        attend(self.user, beneficiary, running, 15)
        self.assertEqual(
            self.evaluate(beneficiary, completed_only=True).reason,
            "not enrolled in a completed training of the programme",
        )

    def test_work_projects_and_other_programmes_do_not_count(self):
        beneficiary = self.participant('Elsewhere')
        work = create_training(
            self.user, self.benefit_plan, [self.module_a, self.module_b], 'Work project',
            project_type='WORK',
        )
        other_plan = create_programme(self.user, 'AGOTHER')
        other_training = create_training(self.user, other_plan, [self.module_a, self.module_b], 'Other')
        attend(self.user, beneficiary, work, 15)
        attend(self.user, beneficiary, other_training, 15)
        self.assertEqual(self.evaluate(beneficiary).reason, "not enrolled in a training of the programme")

    def test_each_module_takes_its_best_result_across_trainings(self):
        beneficiary = self.participant('Repeated')
        later = create_training(self.user, self.benefit_plan, [self.module_a, self.module_b], 'Training 2')
        attend(self.user, beneficiary, self.training, 5)  # all of A, none of B
        attend(self.user, beneficiary, later, list(range(6, 16)))  # none of A, all of B
        result = self.evaluate(beneficiary)
        self.assertTrue(result.eligible, result.reason)

    def test_beneficiaries_are_evaluated_independently(self):
        full = self.participant('IndependentFull')
        short = self.participant('IndependentShort')
        attend(self.user, full, self.training, 15)
        attend(self.user, short, self.training, 3)
        results = evaluate_attendance([full.id, short.id], self.benefit_plan, Decimal("80"), False)
        self.assertTrue(results[str(full.id)].eligible)
        self.assertEqual(len(results[str(short.id)].shortfalls), 2)

    def test_the_minimum_is_inclusive_and_shown_without_trailing_zeros(self):
        beneficiary = self.participant('Fraction')
        attend(self.user, beneficiary, self.training, [1, 2, 3] + list(range(6, 16)))
        self.assertTrue(self.evaluate(beneficiary, minimum=Decimal("60")).eligible)
        self.assertEqual(
            self.evaluate(beneficiary, minimum=Decimal("62.50")).reason,
            "module 'Attendance module A': attended 3 of 5 topics (60.0%), below 62.5%",
        )
