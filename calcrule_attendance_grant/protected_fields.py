"""Beneficiary json_ext keys that size and release training grant payments.

Only users with the matching right may set or change them, because the household
and beneficiary forms render every json_ext key as an editable field and the
update mutations accept the whole json_ext. Unchanged values always pass, so a user
without the right can still save other fields of a record that has them.
"""
import json

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig

PLAN_AND_GRANT_KEYS = (
    "grant_types",
    "approved_grant_amount",
    "tranche_1_cash_amount",
    "tranche_1_equipment_amount",
    "tranche_1_service_amount",
    "tranche_2_cash_amount",
    "tranche_2_equipment_amount",
    "tranche_2_service_amount",
    "business_plan_reference",
    "business_plan_status",
    "plan_approved_on",
    "plan_approved_by",
    "plan_rejection_reason",
)
UTILIZATION_KEYS = (
    "utilization_percentage",
    "utilization_confirmed",
    "utilization_assessed_on",
    "utilization_assessed_by",
    "utilization_notes",
)
APPROVED = "APPROVED"
REJECTED = "REJECTED"


def as_json_ext(value):
    """A json_ext dict from a dict, JSON text or nothing."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def changed_keys(current, incoming, keys):
    """Keys whose value differs; a missing key and an empty value are the same."""
    return [key for key in keys if _value(current, key) != _value(incoming, key)]


def check_protected_changes(user, current_json_ext, incoming_json_ext):
    """Raise ValidationError when ``user`` may not make this json_ext change."""
    current, incoming = as_json_ext(current_json_ext), as_json_ext(incoming_json_ext)
    errors = []

    plan_changes = changed_keys(current, incoming, PLAN_AND_GRANT_KEYS)
    if plan_changes:
        if not user.has_perms(CalcruleAttendanceGrantConfig.gql_plans_and_grants_perms):
            errors.append(_(
                "Only users with the right to manage beneficiary plans and grants can change: %s."
            ) % ", ".join(plan_changes))
        if _value(incoming, "business_plan_status") == APPROVED:
            missing = [key for key in ("plan_approved_on", "plan_approved_by") if _value(incoming, key) is None]
            if missing:
                errors.append(_("An APPROVED plan needs %s.") % " and ".join(missing))
        if _value(incoming, "business_plan_status") == REJECTED and _value(incoming, "plan_rejection_reason") is None:
            errors.append(_("A REJECTED plan needs plan_rejection_reason."))

    utilization_changes = changed_keys(current, incoming, UTILIZATION_KEYS)
    if utilization_changes:
        if not user.has_perms(CalcruleAttendanceGrantConfig.gql_utilization_assessment_perms):
            errors.append(_(
                "Only users with the right to record utilization assessments can change: %s."
            ) % ", ".join(utilization_changes))
        has_result = any(_value(incoming, key) is not None
                         for key in ("utilization_percentage", "utilization_confirmed"))
        if has_result:
            missing = [key for key in ("utilization_assessed_on", "utilization_assessed_by")
                       if _value(incoming, key) is None]
            if missing:
                errors.append(_("A utilization result needs %s.") % " and ".join(missing))

    if errors:
        raise ValidationError(errors)


def _value(json_ext, key):
    value = json_ext.get(key)
    if value is None or (isinstance(value, str) and not value.strip()) or value == []:
        return None
    return value
