"""Training completion per participant and required module.

For each required Activity (training module), a participant's completion is

    attended days / scheduled days * 100

for a training session of that module they are enrolled in, where an attended day
is a time entry whose percent_complete reaches the plan's attended-day threshold.
Modules are evaluated independently: attendance in one module never compensates
for missed days in another. If the participant is enrolled in several sessions of
the same module (for example after repeating it), the best session counts.

Only individual enrollments are read. Group-level time entries are never used as
evidence of a participant's attendance.
"""
from dataclasses import dataclass, field

from django.db.models import Count, F, Q

from project_social_protection.models import (
    Activity,
    BeneficiaryProjectEnrollment,
    ProjectStatus,
)

# Project.project_type of a training session.
TRAINING_PROJECT_TYPE = "TRAINING"


@dataclass(frozen=True)
class ModuleAttendance:
    activity_name: str
    scheduled_days: int
    attended_days: int

    @property
    def completion_percentage(self):
        return self.attended_days * 100 / self.scheduled_days

    def meets(self, minimum_percentage):
        # Compare whole numbers so a boundary value is never lost to rounding.
        return self.attended_days * 100 >= minimum_percentage * self.scheduled_days


@dataclass
class ParticipantAttendance:
    # Best session per required Activity ID the participant attended.
    modules: dict = field(default_factory=dict)
    shortfalls: list = field(default_factory=list)

    @property
    def eligible(self):
        return not self.shortfalls

    @property
    def reason(self):
        return "; ".join(self.shortfalls)


def evaluate_attendance(beneficiary_ids, benefit_plan, params):
    """Return {beneficiary_id: ParticipantAttendance} for a list of beneficiary IDs."""
    activity_names = dict(
        Activity.objects.filter(id__in=params.required_activity_ids).values_list("id", "name")
    )
    activity_names = {str(activity_id): name for activity_id, name in activity_names.items()}

    enrollments = BeneficiaryProjectEnrollment.objects.filter(
        beneficiary_id__in=beneficiary_ids,
        is_deleted=False,
        project__is_deleted=False,
        project__benefit_plan=benefit_plan,
        project__project_type=TRAINING_PROJECT_TYPE,
        project__activity_id__in=params.required_activity_ids,
        project__working_days__gt=0,
    )
    if params.only_completed_sessions:
        enrollments = enrollments.filter(project__status=ProjectStatus.COMPLETED)
    rows = enrollments.annotate(
        attended_days=Count(
            "time_entries",
            filter=Q(
                time_entries__is_deleted=False,
                time_entries__percent_complete__gte=params.attended_day_threshold,
                time_entries__day_number__gte=1,
                time_entries__day_number__lte=F("project__working_days"),
            ),
        )
    ).values_list("beneficiary_id", "project__activity_id", "project__working_days", "attended_days")

    best_sessions = {}
    for beneficiary_id, activity_id, working_days, attended_days in rows:
        activity_id = str(activity_id)
        session = ModuleAttendance(
            activity_name=activity_names.get(activity_id, activity_id),
            scheduled_days=working_days,
            attended_days=attended_days,
        )
        key = (beneficiary_id, activity_id)
        best = best_sessions.get(key)
        if best is None or session.attended_days * best.scheduled_days > best.attended_days * session.scheduled_days:
            best_sessions[key] = session

    session_kind = "completed training session" if params.only_completed_sessions else "training session"
    results = {}
    for beneficiary_id in beneficiary_ids:
        result = ParticipantAttendance()
        for activity_id in params.required_activity_ids:
            session = best_sessions.get((beneficiary_id, activity_id))
            name = activity_names.get(activity_id, activity_id)
            if session is None:
                result.shortfalls.append(f"module '{name}': not enrolled in a {session_kind}")
                continue
            result.modules[activity_id] = session
            if not session.meets(params.minimum_attendance_percentage):
                result.shortfalls.append(
                    "module '%s': attended %s of %s days (%.1f%%), below %s%%" % (
                        name, session.attended_days, session.scheduled_days,
                        session.completion_percentage,
                        _format_percentage(params.minimum_attendance_percentage),
                    )
                )
        results[beneficiary_id] = result
    return results


def _format_percentage(value):
    return f"{value.normalize():f}" if hasattr(value, "normalize") else str(value)
