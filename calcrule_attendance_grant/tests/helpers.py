"""Fixtures shared by the attendance-grant tests."""
from decimal import Decimal

from social_protection.models import Beneficiary, BeneficiaryStatus
from social_protection.services import BeneficiaryService
from social_protection.tests.test_helpers import create_benefit_plan, create_individual
from project_social_protection.models import (
    BeneficiaryProjectEnrollment,
    BeneficiaryProjectTimeEntry,
    ProjectStatus,
)
from project_social_protection.tests.test_helpers import create_project

from calcrule_attendance_grant.attendance import TRAINING_PROJECT_TYPE
from calcrule_attendance_grant.parameters import GrantParameters


def create_individual_benefit_plan(user, code):
    return create_benefit_plan(
        user.username,
        payload_override={'code': code, 'type': 'INDIVIDUAL', 'max_beneficiaries': None},
    )


def create_participant(user, benefit_plan, first_name, json_ext=None):
    individual = create_individual(user.username, payload_override={'first_name': first_name})
    result = BeneficiaryService(user).create({
        "individual_id": individual.id,
        "benefit_plan_id": benefit_plan.id,
        "status": BeneficiaryStatus.ACTIVE,
        "json_ext": json_ext or {},
    })
    assert result.get('success'), result
    return Beneficiary.objects.get(uuid=result['data']['uuid'])


def create_session(user, benefit_plan, activity, name, working_days=5,
                   status=ProjectStatus.COMPLETED, project_type=TRAINING_PROJECT_TYPE):
    return create_project(
        name, benefit_plan, user.username,
        activity=activity, working_days=working_days, status=status,
        project_type=project_type, allows_multiple_enrollments=True,
    )


def attend(user, beneficiary, session, days, percent_complete=100):
    """Enroll the beneficiary and record ``days`` (count or list of day numbers)."""
    enrollment = BeneficiaryProjectEnrollment(beneficiary=beneficiary, project=session)
    enrollment.save(user=user)
    day_numbers = range(1, days + 1) if isinstance(days, int) else days
    for day_number in day_numbers:
        BeneficiaryProjectTimeEntry(
            enrollment=enrollment, day_number=day_number, percent_complete=percent_complete,
        ).save(user=user)
    return enrollment


def grant_parameters(activities, **overrides):
    values = dict(
        grant_amount_source="payment_plan",
        grant_amount=Decimal("1000"),
        beneficiary_grant_amount_key="approved_grant_amount",
        tranche_percentage=Decimal("70"),
        minimum_attendance_percentage=Decimal("80"),
        attended_day_threshold=100,
        required_activity_ids=tuple(str(activity.id) for activity in activities),
        only_completed_sessions=True,
    )
    values.update(overrides)
    return GrantParameters(**values)
