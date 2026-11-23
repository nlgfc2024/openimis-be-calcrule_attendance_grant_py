"""Business plan approval through a maker-checker task.

A user with the plans-and-grants right submits a beneficiary's grant and business
plan details (grant types, amounts, business plan reference), one at a time or as
an upload file. The submission is a task holding the proposed values, one task per
beneficiary or per file; the beneficiaries are not changed yet.

The task goes to the task group whose ``task_sources`` include ``TASK_SOURCE``. An
executor of that group with the plan approval right approves or rejects it through
the standard task resolution, under the group's completion policy:

- approved: the proposed values are saved with ``business_plan_status = APPROVED``,
  and the approval is recorded from the user who completed the task
  (``plan_approved_on``, ``plan_approved_by``, ``plan_approved_by_id``);
- rejected: the proposed values are dropped, ``business_plan_status = REJECTED`` and
  the rejecting user's reason is recorded as ``plan_rejection_reason``.

Who may submit and who may decide is set by roles and task groups: the same user may
do both if an administrator allows it. IMIS administrators may decide any task.
"""
import datetime
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.translation import gettext as _

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig
from calcrule_attendance_grant.grants import GRANT_TYPES
from calcrule_attendance_grant.protected_fields import (
    APPROVED,
    GRANT_KEYS,
    PLAN_AND_GRANT_KEYS,
    REJECTED,
    applying_plan_decision,
    as_json_ext,
    changed_keys,
)

logger = logging.getLogger(__name__)

TASK_SOURCE = "calcrule_attendance_grant.plan_approval"
BUSINESS_EVENT = "calcrule_attendance_grant.plan_approval"
UPLOAD_BUSINESS_EVENT = "calcrule_attendance_grant.plan_approval_upload"
BUSINESS_EVENTS = (BUSINESS_EVENT, UPLOAD_BUSINESS_EVENT)
OPEN_STATUSES = ("RECEIVED", "ACCEPTED")
# Task data key telling which beneficiary model the task is for.
BENEFICIARY_TYPE_KEY = "beneficiary_type"
INDIVIDUAL_BENEFICIARY = "Beneficiary"
GROUP_BENEFICIARY = "GroupBeneficiary"
REASON_KEY = "reason"

AMOUNT_KEYS = tuple(key for key in GRANT_KEYS if key.endswith("_amount"))


def beneficiary_model(beneficiary_type):
    from social_protection.models import Beneficiary, GroupBeneficiary
    return {INDIVIDUAL_BENEFICIARY: Beneficiary, GROUP_BENEFICIARY: GroupBeneficiary}[beneficiary_type]


def beneficiary_type_of(beneficiary):
    from social_protection.models import GroupBeneficiary
    return GROUP_BENEFICIARY if isinstance(beneficiary, GroupBeneficiary) else INDIVIDUAL_BENEFICIARY


def beneficiary_label(beneficiary):
    """How a reviewer recognises the beneficiary: the person's name or the household code."""
    group = getattr(beneficiary, "group", None)
    if group is not None:
        return group.code
    individual = beneficiary.individual
    return " ".join(part for part in (individual.first_name, individual.last_name) if part)


def approver_name(user):
    i_user = getattr(user, "i_user", None)
    name = " ".join(part for part in (getattr(i_user, "other_names", None), getattr(i_user, "last_name", None))
                    if part).strip()
    return name or user.username


class PlanApprovalService:
    """Submits beneficiaries' grant and business plan details for approval."""

    def __init__(self, user):
        self.user = user

    def submit(self, beneficiary, proposed):
        """Create the approval task for ``beneficiary`` with the ``proposed`` grant keys.

        ``proposed`` holds the grant keys the beneficiary should have once approved; a key
        left out keeps its current value and a None or blank value removes it.
        """
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        if not self.user.has_perms(CalcruleAttendanceGrantConfig.gql_plans_and_grants_perms):
            raise ValidationError(_("Only users with the right to manage beneficiary plans and grants can "
                                    "submit them for approval."))
        proposed = validate_proposal(proposed, beneficiary.benefit_plan)
        if open_approval_beneficiary_ids([beneficiary.id]):
            raise ValidationError(_("This beneficiary already has a plan approval waiting for a decision."))
        current = as_json_ext(beneficiary.json_ext)
        if not changed_keys(current, {**current, **proposed}, GRANT_KEYS) \
                and current.get("business_plan_status") == APPROVED:
            raise ValidationError(_("These details are already approved."))

        with transaction.atomic():
            result = TaskService(self.user).create({
                "source": TASK_SOURCE,
                "entity_type": ContentType.objects.get_for_model(beneficiary),
                "entity_id": str(beneficiary.id),
                "status": Task.Status.RECEIVED,
                "executor_action_event": TasksManagementConfig.default_executor_event,
                "business_event": BUSINESS_EVENT,
                "data": {
                    BENEFICIARY_TYPE_KEY: beneficiary_type_of(beneficiary),
                    "label": beneficiary_label(beneficiary),
                    "incoming_data": proposed,
                    "current_data": {key: current.get(key) for key in PLAN_AND_GRANT_KEYS if key in current},
                },
                "json_ext": {"benefit_plan_code": beneficiary.benefit_plan.code},
            })
        if not result.get("success"):
            raise ValidationError(result.get("detail") or result.get("message") or _("The task was not created."))
        return result

    def submit_upload(self, upload, benefit_plan, beneficiary_type, rows, invalid_rows=0):
        """Create one approval task for an upload file's valid ``rows``.

        Each row is ``{"id": <beneficiary ID>, "incoming_data": {...}, "current_data": {...}}``.
        """
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        result = TaskService(self.user).create({
            "source": TASK_SOURCE,
            "entity_type": ContentType.objects.get_for_model(upload),
            "entity_id": str(upload.id),
            "status": Task.Status.RECEIVED,
            "executor_action_event": TasksManagementConfig.default_executor_event,
            "business_event": UPLOAD_BUSINESS_EVENT,
            "data": {BENEFICIARY_TYPE_KEY: beneficiary_type, "rows": rows},
            "json_ext": {
                "benefit_plan_code": benefit_plan.code, "source_name": upload.source_name,
                "data_upload_id": str(upload.id), "rows": len(rows), "invalid_rows": invalid_rows,
            },
        })
        if not result.get("success"):
            raise ValidationError(result.get("detail") or result.get("message") or _("The task was not created."))
        return result


def pending_approval(beneficiary):
    """The beneficiary's waiting approval as ``(task, proposed values)``, or None."""
    from tasks_management.models import Task

    beneficiary_id = str(beneficiary.id)
    open_tasks = Task.objects.filter(is_deleted=False, status__in=OPEN_STATUSES).order_by("-date_created")
    task = open_tasks.filter(business_event=BUSINESS_EVENT, entity_id=beneficiary_id).first()
    if task is not None:
        return task, task.data.get("incoming_data") or {}
    task = open_tasks.filter(business_event=UPLOAD_BUSINESS_EVENT,
                             data__rows__contains=[{"id": beneficiary_id}]).first()
    if task is not None:
        row = next(row for row in task.data["rows"] if row.get("id") == beneficiary_id)
        return task, row.get("incoming_data") or {}
    return None


def open_approval_beneficiary_ids(beneficiary_ids):
    """The IDs, among ``beneficiary_ids``, of beneficiaries with a plan approval waiting."""
    from django.db.models import Q
    from tasks_management.models import Task

    ids = {str(beneficiary_id) for beneficiary_id in beneficiary_ids}
    if not ids:
        return set()
    open_tasks = Task.objects.filter(is_deleted=False, status__in=OPEN_STATUSES)
    waiting = set(open_tasks.filter(business_event=BUSINESS_EVENT, entity_id__in=ids)
                  .values_list("entity_id", flat=True))
    upload_rows = Q()
    for beneficiary_id in ids:
        upload_rows |= Q(data__rows__contains=[{"id": beneficiary_id}])
    for rows in open_tasks.filter(upload_rows, business_event=UPLOAD_BUSINESS_EVENT).values_list("data__rows", flat=True):
        waiting |= {row.get("id") for row in rows or []} & ids
    return waiting


def validate_proposal(proposed, benefit_plan):
    """The proposed grant keys, checked; raises ValidationError listing every problem."""
    from calcrule_attendance_grant.programme import ProgrammeSettings

    if not isinstance(proposed, dict) or not proposed:
        raise ValidationError(_("Submit at least one grant or business plan detail."))
    unknown = sorted(set(proposed) - set(GRANT_KEYS))
    if unknown:
        raise ValidationError(_("Only grant and business plan details can be submitted for approval, not: %s.")
                              % ", ".join(unknown))

    errors, cleaned = [], {}
    settings_errors = []
    allowed = ProgrammeSettings.from_benefit_plan(benefit_plan, settings_errors).allowed_grant_types
    if "grant_types" in proposed and settings_errors:
        raise ValidationError(settings_errors)
    for key, value in proposed.items():
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            cleaned[key] = None
        elif key == "grant_types":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(_("grant_types must be a list of grant types."))
                continue
            not_allowed = [item for item in value if item not in allowed]
            if not_allowed:
                errors.append(_("grant_types has types the programme does not allow: %s.") % ", ".join(not_allowed))
            cleaned[key] = [grant_type for grant_type in GRANT_TYPES if grant_type in value]
        elif key in AMOUNT_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(_("%s must be a number.") % key)
                continue
            number = Decimal(str(value))
            positive = key == "approved_grant_amount"
            if (positive and number <= 0) or number < 0 or number != number.quantize(Decimal("0.01")):
                errors.append(_("%s must be %s, with at most two decimals.")
                              % (key, _("more than 0") if positive else _("0 or more")))
                continue
            cleaned[key] = value
        else:
            text = str(value).strip()
            if len(text) > 255:
                errors.append(_("%s must be at most 255 characters.") % key)
                continue
            cleaned[key] = text
    if errors:
        raise ValidationError(errors)
    return cleaned


def check_decision(user, task, business_status, additional_data):
    """Raise ValidationError when ``user`` may not approve or reject this approval task."""
    from tasks_management.apps import TasksManagementConfig

    decision = (business_status or {}).get(str(user.id))
    if decision not in (TasksManagementConfig.task_user_approved, "FAILED"):
        return
    if not user.is_imis_admin:
        if not user.has_perms(CalcruleAttendanceGrantConfig.gql_plan_approval_perms):
            raise ValidationError(_("Only users with the right to approve beneficiary plans can approve or "
                                    "reject this plan."))
        if not _is_executor(user, task):
            raise ValidationError(_("Only executors of the task group this approval is assigned to can approve "
                                    "or reject it."))
    if decision == "FAILED" and not rejection_reason(additional_data):
        raise ValidationError(_("Give a reason for rejecting the plan."))


def rejection_reason(additional_data):
    if isinstance(additional_data, dict):
        additional_data = additional_data.get(REASON_KEY)
    return additional_data.strip() if isinstance(additional_data, str) and additional_data.strip() else None


def _is_executor(user, task):
    if task.task_group is None or task.task_group.is_deleted:
        return False
    return task.task_group.taskexecutor_set.filter(user=user, is_deleted=False).exists()


def apply_decision(task, user):
    """Save the decision of a completed or failed approval task on its beneficiaries."""
    from tasks_management.models import Task

    if task.status not in (Task.Status.COMPLETED, Task.Status.FAILED):
        return
    approved = task.status == Task.Status.COMPLETED
    reason = None if approved else rejection_reason(
        ((task.json_ext or {}).get("additional_resolve_data") or {}).get(str(user.id)))
    model = beneficiary_model(task.data.get(BENEFICIARY_TYPE_KEY, INDIVIDUAL_BENEFICIARY))

    if task.business_event == UPLOAD_BUSINESS_EVENT:
        proposals = [(row["id"], row.get("incoming_data") or {}) for row in task.data.get("rows") or []]
    else:
        proposals = [(task.entity_id, task.data.get("incoming_data") or {})]

    with applying_plan_decision():
        for beneficiary_id, proposal in proposals:
            beneficiary = model.objects.filter(id=beneficiary_id, is_deleted=False).first()
            if beneficiary is None:
                logger.warning("Plan approval task %s: beneficiary %s no longer exists", task.id, beneficiary_id)
                continue
            beneficiary.json_ext = decided_json_ext(beneficiary.json_ext, proposal, approved, user, reason)
            beneficiary.save(user=user)

    if task.business_event == UPLOAD_BUSINESS_EVENT:
        _close_upload(task, approved, reason, user)


def decided_json_ext(json_ext, proposal, approved, user, reason):
    """``json_ext`` with an approval's proposal and decision recorded."""
    json_ext = dict(as_json_ext(json_ext))
    if approved:
        for key, value in proposal.items():
            if value is None:
                json_ext.pop(key, None)
            else:
                json_ext[key] = value
        json_ext.update({
            "business_plan_status": APPROVED,
            "plan_approved_on": datetime.date.today().isoformat(),
            "plan_approved_by": approver_name(user),
            "plan_approved_by_id": str(user.id),
        })
        json_ext.pop("plan_rejection_reason", None)
    else:
        for key in ("plan_approved_on", "plan_approved_by", "plan_approved_by_id"):
            json_ext.pop(key, None)
        json_ext.update({"business_plan_status": REJECTED, "plan_rejection_reason": reason})
    return json_ext


def _close_upload(task, approved, reason, user):
    from individual.models import IndividualDataSourceUpload

    upload = IndividualDataSourceUpload.objects.filter(id=task.entity_id).first()
    if upload is None:
        return
    if approved:
        partial = (task.json_ext or {}).get("invalid_rows")
        upload.status = IndividualDataSourceUpload.Status.PARTIAL_SUCCESS if partial \
            else IndividualDataSourceUpload.Status.SUCCESS
    else:
        upload.status = IndividualDataSourceUpload.Status.FAIL
        upload.error = {"Task Resolve": _("The plan approval was rejected: %s") % reason}
    upload.save(user=user)
