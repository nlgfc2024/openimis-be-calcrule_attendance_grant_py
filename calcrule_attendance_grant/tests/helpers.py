"""Fixtures shared by the attendance-grant tests."""
from decimal import Decimal

from social_protection.models import Beneficiary, BeneficiaryStatus
from social_protection.services import BeneficiaryService
from social_protection.tests.test_helpers import create_benefit_plan, create_individual
from project_social_protection.models import (
    Activity,
    ActivityTopic,
    BeneficiaryProjectEnrollment,
    BeneficiaryProjectTimeEntry,
    ProjectStatus,
    ProjectTopic,
)
from project_social_protection.tests.test_helpers import create_project

from calcrule_attendance_grant.attendance import TRAINING_PROJECT_TYPE
from calcrule_attendance_grant.parameters import GrantParameters
from calcrule_attendance_grant.programme import ProgrammeSettings


def create_individual_benefit_plan(user, code, json_ext=None):
    return create_benefit_plan(
        user.username,
        payload_override={
            'code': code, 'type': 'INDIVIDUAL', 'max_beneficiaries': None,
            'json_ext': json_ext or {},
        },
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


def create_module(user, benefit_plan, name, topic_count, code_prefix):
    """A training module (Activity) of the programme with ``topic_count`` topics."""
    module = Activity(name=name)
    module.save(username=user.username)
    module.benefit_plans.add(benefit_plan)
    for sequence in range(1, topic_count + 1):
        ActivityTopic(
            activity=module, code=f"{code_prefix}{sequence:02d}",
            name=f"{name} topic {sequence}", sequence=sequence,
        ).save(username=user.username)
    return module


def create_training(user, benefit_plan, modules, name, status=ProjectStatus.COMPLETED,
                    project_type=TRAINING_PROJECT_TYPE):
    """A training with one attendance-sheet column per topic of ``modules``, in order."""
    topics = [
        topic for module in modules
        for topic in module.topics.filter(is_deleted=False).order_by('sequence')
    ]
    training = create_project(
        name, benefit_plan, user.username,
        activity=None, working_days=len(topics), status=status,
        project_type=project_type, allows_multiple_enrollments=True,
    )
    for sequence, topic in enumerate(topics, start=1):
        ProjectTopic(
            project=training, topic=topic, module=topic.activity, sequence=sequence,
            code=topic.code, name=topic.name, module_name=topic.activity.name,
        ).save(username=user.username)
    return training


def tick(user, enrollment, topics, time_entry_model=BeneficiaryProjectTimeEntry, percent_complete=100):
    """Record ``topics`` (a count from column 1, or a list of columns) on the sheet."""
    columns = range(1, topics + 1) if isinstance(topics, int) else topics
    for column in columns:
        time_entry_model(
            enrollment=enrollment, day_number=column, percent_complete=percent_complete,
        ).save(user=user)
    return enrollment


def attend(user, beneficiary, training, topics, percent_complete=100):
    """Enroll the individual beneficiary and tick ``topics`` on the training's sheet."""
    enrollment = BeneficiaryProjectEnrollment(beneficiary=beneficiary, project=training)
    enrollment.save(user=user)
    return tick(user, enrollment, topics, percent_complete=percent_complete)


def grant_parameters(**overrides):
    values = dict(
        grant_amount_source="payment_plan",
        grant_amount=Decimal("1000"),
        beneficiary_grant_amount_key="approved_grant_amount",
        tranche_percentage=Decimal("70"),
        minimum_attendance_percentage=Decimal("80"),
        programme=ProgrammeSettings(),
    )
    values.update(overrides)
    return GrantParameters(**values)
