"""Training completion per beneficiary and module.

A training always covers all of its programme's modules, and its attendance sheet
has one column per topic. For each module of the beneficiary's training

    ticked topics of the module / topics of the module * 100

must reach the plan's minimum. Modules are evaluated independently: topics attended
in one module never compensate for topics missed in another. The per-module figures
come from the project module's shared helper, so the payroll agrees with the
attendance grid. If a beneficiary has several trainings of the programme, each
module's best result counts.

Attendance is read from the beneficiary's own enrollments: the household's for a
GROUP programme (its primary worker attends), the individual's for an INDIVIDUAL one.
"""
from dataclasses import dataclass, field

from project_social_protection.models import (
    BeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectEnrollment,
    ProjectStatus,
)
try:
    # Two queries for the whole payroll; older project modules only have the
    # per-enrollment helper.
    from project_social_protection.training_topics import training_module_completion_for
except ImportError:  # pragma: no cover - depends on the installed project module
    from project_social_protection.training_topics import training_module_completion

    def training_module_completion_for(enrollments):
        return {enrollment.pk: training_module_completion(enrollment) for enrollment in enrollments}

# Project.project_type of a training.
TRAINING_PROJECT_TYPE = "TRAINING"
GROUP_BENEFIT_PLAN = "GROUP"


@dataclass(frozen=True)
class ModuleCompletion:
    module_name: str
    ticked_topics: int
    total_topics: int

    @property
    def percentage(self):
        return self.ticked_topics * 100 / self.total_topics

    def meets(self, minimum_percentage):
        # Compare whole numbers so a boundary value is never lost to rounding.
        return self.ticked_topics * 100 >= minimum_percentage * self.total_topics

    def beats(self, other):
        return self.ticked_topics * other.total_topics > other.ticked_topics * self.total_topics


@dataclass
class BeneficiaryAttendance:
    # Best result per module ID, in sheet order.
    modules: dict = field(default_factory=dict)
    shortfalls: list = field(default_factory=list)
    # Names of the beneficiary's trainings whose attendance sheet has no topics.
    trainings_without_topics: list = field(default_factory=list)

    @property
    def eligible(self):
        return not self.shortfalls

    @property
    def reason(self):
        return "; ".join(self.shortfalls)


def evaluate_attendance(beneficiary_ids, benefit_plan, minimum_percentage, completed_only):
    """Return {str(beneficiary_id): BeneficiaryAttendance}.

    ``beneficiary_ids`` are Beneficiary IDs for an INDIVIDUAL programme and
    GroupBeneficiary IDs for a GROUP one.
    """
    if benefit_plan.type == GROUP_BENEFIT_PLAN:
        enrollment_model, beneficiary_field = GroupBeneficiaryProjectEnrollment, "group_beneficiary_id"
    else:
        enrollment_model, beneficiary_field = BeneficiaryProjectEnrollment, "beneficiary_id"

    enrollments = enrollment_model.objects.filter(
        **{f"{beneficiary_field}__in": beneficiary_ids},
        is_deleted=False,
        project__is_deleted=False,
        project__benefit_plan=benefit_plan,
        project__project_type=TRAINING_PROJECT_TYPE,
    ).select_related("project").order_by("project__date_created", "id")
    if completed_only:
        enrollments = enrollments.filter(project__status=ProjectStatus.COMPLETED)

    results = {str(beneficiary_id): BeneficiaryAttendance() for beneficiary_id in beneficiary_ids}
    enrollments = list(enrollments)
    completions = training_module_completion_for(enrollments)
    for enrollment in enrollments:
        result = results[str(getattr(enrollment, beneficiary_field))]
        rows = [row for row in completions.get(enrollment.pk, []) if row["total_topics"]]
        if not rows:
            result.trainings_without_topics.append(enrollment.project.name)
        for row in rows:
            completion = ModuleCompletion(
                module_name=row["module"]["name"],
                ticked_topics=row["ticked_topics"],
                total_topics=row["total_topics"],
            )
            module_id = str(row["module"]["id"])
            best = result.modules.get(module_id)
            if best is None or completion.beats(best):
                result.modules[module_id] = completion

    training_kind = "completed training" if completed_only else "training"
    for result in results.values():
        if not result.modules:
            if result.trainings_without_topics:
                result.shortfalls.append("enrolled in %s, whose attendance sheet has no topics yet" % ", ".join(
                    f"'{name}'" for name in result.trainings_without_topics))
            else:
                result.shortfalls.append(f"not enrolled in a {training_kind} of the programme")
            continue
        for completion in result.modules.values():
            if not completion.meets(minimum_percentage):
                result.shortfalls.append(
                    "module '%s': attended %s of %s topics (%.1f%%), below %s%%" % (
                        completion.module_name, completion.ticked_topics, completion.total_topics,
                        completion.percentage, _format_percentage(minimum_percentage),
                    )
                )
    return results


def _format_percentage(value):
    return f"{value.normalize():f}" if hasattr(value, "normalize") else str(value)
