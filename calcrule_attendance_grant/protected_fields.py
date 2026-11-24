"""Beneficiary json_ext keys that size and release training grant payments.

The household and beneficiary forms render every json_ext key as an editable field,
and the update mutations accept the whole json_ext, so these keys are protected:

- The grant and business plan keys change only through a plan approval decision
  (see plan_approval): a user with the plans-and-grants right submits them, and a
  user with the plan approval right approves or rejects the submission. A direct
  save, or a generic beneficiary task, cannot change them.
- The utilization keys are saved directly, by users with the utilization right, and
  a new or changed result only once the beneficiary's tranche one is paid in full, so
  tranche two counts only an assessment made after the money and the equipment or
  service were received.

Unchanged values always pass, so other fields of a record that has these keys can
still be saved. Values are compared by meaning: 700, 700.0 and "700" are the same.
"""
import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig

# What the maker submits for approval.
GRANT_KEYS = (
    "grant_types",
    "approved_grant_amount",
    "tranche_1_cash_amount",
    "tranche_1_equipment_amount",
    "tranche_1_service_amount",
    "tranche_2_cash_amount",
    "tranche_2_equipment_amount",
    "tranche_2_service_amount",
    "business_plan_reference",
)
# What the approval decision records.
APPROVAL_KEYS = (
    "business_plan_status",
    "plan_approved_on",
    "plan_approved_by",
    "plan_approved_by_id",
    "plan_rejection_reason",
)
PLAN_AND_GRANT_KEYS = GRANT_KEYS + APPROVAL_KEYS
UTILIZATION_KEYS = (
    "utilization_percentage",
    "utilization_confirmed",
    "utilization_assessed_on",
    "utilization_assessed_by",
    "utilization_notes",
)
APPROVED = "APPROVED"
REJECTED = "REJECTED"

_applying_plan_decision = ContextVar("applying_plan_decision", default=False)


@contextmanager
def applying_plan_decision():
    """Lets the plan approval decision, and only it, change the grant and plan keys."""
    token = _applying_plan_decision.set(True)
    try:
        yield
    finally:
        _applying_plan_decision.reset(token)


def as_json_ext(value):
    """A json_ext dict from a dict, JSON text or nothing."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def changed_keys(current, incoming, keys):
    """Keys whose value differs in meaning; a missing key and an empty value are the same."""
    return [key for key in keys if _value(current, key) != _value(incoming, key)]


def check_protected_changes(user, current_json_ext, incoming_json_ext, beneficiary=None):
    """Raise ValidationError when ``user`` may not make this json_ext change to
    ``beneficiary`` (with its ``id`` and ``benefit_plan_id``; None for a new one)."""
    current, incoming = as_json_ext(current_json_ext), as_json_ext(incoming_json_ext)
    errors = []

    plan_changes = changed_keys(current, incoming, PLAN_AND_GRANT_KEYS)
    if plan_changes and not _applying_plan_decision.get():
        errors.append(_(
            "Grant and business plan details change only through a plan approval: submit them for "
            "approval instead. Changed: %s."
        ) % ", ".join(plan_changes))

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
            reason = tranche_one_shortfall_of(beneficiary)
            if reason:
                errors.append(_(
                    "The utilization can be assessed only once tranche one is paid in full: %s."
                ) % reason)

    if errors:
        raise ValidationError(errors)


def tranche_one_shortfall_of(beneficiary):
    """Why ``beneficiary`` has not received its tranche one in full yet, or None."""
    from calcrule_attendance_grant.strategy import tranche_benefits
    from calcrule_attendance_grant.tranche_two import tranche_one_shortfall

    records = None
    if beneficiary is not None and beneficiary.id:
        records = tranche_benefits(beneficiary.benefit_plan_id, 1, [beneficiary.id]).get(str(beneficiary.id))
    return tranche_one_shortfall(records)


def _value(json_ext, key):
    return _normalised(json_ext.get(key))


def _normalised(value):
    """The value's meaning: numbers and numeric text as Decimal, text trimmed, blanks as None."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value)) if not isinstance(value, float) or math.isfinite(value) else value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return text
        return number if number.is_finite() else text
    if isinstance(value, list):
        return [_normalised(item) for item in value] or None
    return value
