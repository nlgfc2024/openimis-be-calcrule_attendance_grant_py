"""PaymentPlan parameters of the attendance-grant rule.

The Payment Plan UI stores the rule's parameters under
``PaymentPlan.json_ext["calculation_rule"]``, usually as text. This module is the
single place that names them, applies their defaults and validates them.
"""
import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError

GRANT_SOURCE_PAYMENT_PLAN = "payment_plan"
GRANT_SOURCE_BENEFICIARY_JSON = "beneficiary_json"
GRANT_SOURCES = (GRANT_SOURCE_PAYMENT_PLAN, GRANT_SOURCE_BENEFICIARY_JSON)

DEFAULT_BENEFICIARY_GRANT_AMOUNT_KEY = "approved_grant_amount"
DEFAULT_TRANCHE_PERCENTAGE = Decimal("70")
DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE = Decimal("80")
DEFAULT_ATTENDED_DAY_THRESHOLD = 100
DEFAULT_ONLY_COMPLETED_SESSIONS = True

INDIVIDUAL_BENEFIT_PLAN = "INDIVIDUAL"


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_decimal(value):
    """A finite Decimal from a number or numeric text, else None."""
    if isinstance(value, bool) or _is_blank(value):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _parse_ids(value):
    """Activity IDs from a list or from comma-separated text."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return None
    return [str(item).strip() for item in value if not _is_blank(str(item))]


@dataclass(frozen=True)
class GrantParameters:
    grant_amount_source: str
    grant_amount: Decimal
    beneficiary_grant_amount_key: str
    tranche_percentage: Decimal
    minimum_attendance_percentage: Decimal
    attended_day_threshold: int
    required_activity_ids: tuple
    only_completed_sessions: bool

    @classmethod
    def from_payment_plan(cls, payment_plan):
        """Read and validate the plan's parameters.

        Raises AttendanceGrantConfigurationError listing every problem found.
        """
        errors = []
        if payment_plan.benefit_plan.type != INDIVIDUAL_BENEFIT_PLAN:
            errors.append(
                "the benefit plan must be an INDIVIDUAL benefit plan, because "
                "attendance and payment are per participant"
            )
        json_ext = payment_plan.json_ext if isinstance(payment_plan.json_ext, dict) else {}
        rule = json_ext.get("calculation_rule")
        rule = rule if isinstance(rule, dict) else {}

        if not _is_blank(rule.get("requires_reconciled_payment_plan_id")):
            errors.append(
                "second-tranche plans (requires_reconciled_payment_plan_id) are not "
                "supported yet"
            )

        source = rule.get("grant_amount_source")
        if source not in GRANT_SOURCES:
            errors.append(
                "grant_amount_source must be one of %s" % ", ".join(GRANT_SOURCES)
            )

        grant_amount = None
        if source == GRANT_SOURCE_PAYMENT_PLAN:
            grant_amount = _parse_decimal(rule.get("grant_amount"))
            if grant_amount is None or grant_amount <= 0:
                errors.append(
                    "grant_amount must be a positive number when grant_amount_source "
                    "is %s" % GRANT_SOURCE_PAYMENT_PLAN
                )

        amount_key = rule.get("beneficiary_grant_amount_key")
        if _is_blank(amount_key):
            amount_key = DEFAULT_BENEFICIARY_GRANT_AMOUNT_KEY
        elif not isinstance(amount_key, str):
            errors.append("beneficiary_grant_amount_key must be text")
        else:
            amount_key = amount_key.strip()

        tranche_percentage = cls._percentage(
            rule, "tranche_percentage", DEFAULT_TRANCHE_PERCENTAGE, errors
        )
        minimum_attendance = cls._percentage(
            rule, "minimum_attendance_percentage",
            DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE, errors,
        )
        attended_day_threshold = cls._percentage(
            rule, "attended_day_threshold", Decimal(DEFAULT_ATTENDED_DAY_THRESHOLD), errors
        )

        only_completed = rule.get("only_completed_sessions")
        if _is_blank(only_completed):
            only_completed = DEFAULT_ONLY_COMPLETED_SESSIONS
        else:
            only_completed = _parse_bool(only_completed)
            if only_completed is None:
                errors.append("only_completed_sessions must be true or false")

        activity_ids = cls._activity_ids(rule.get("required_activity_ids"), errors)

        if errors:
            raise AttendanceGrantConfigurationError(
                "Payment plan %s cannot be calculated with the attendance-grant rule: %s."
                % (payment_plan.code, "; ".join(errors))
            )
        return cls(
            grant_amount_source=source,
            grant_amount=grant_amount,
            beneficiary_grant_amount_key=amount_key,
            tranche_percentage=tranche_percentage,
            minimum_attendance_percentage=minimum_attendance,
            # percent_complete is a whole number, so a fractional threshold
            # behaves like the next whole number.
            attended_day_threshold=int(attended_day_threshold.to_integral_value(rounding=ROUND_CEILING)),
            required_activity_ids=activity_ids,
            only_completed_sessions=only_completed,
        )

    @staticmethod
    def _percentage(rule, name, default, errors):
        """A percentage above 0 and at most 100, or the default when unset."""
        value = rule.get(name)
        if _is_blank(value):
            return default
        number = _parse_decimal(value)
        if number is None or not 0 < number <= 100:
            errors.append(f"{name} must be a number above 0 and at most 100")
            return default
        return number

    @staticmethod
    def _activity_ids(value, errors):
        from project_social_protection.models import Activity

        ids = _parse_ids(value) if not _is_blank(value) else []
        if not ids:
            errors.append("required_activity_ids must list at least one Activity ID")
            return ()
        invalid = []
        for activity_id in ids:
            try:
                uuid.UUID(activity_id)
            except ValueError:
                invalid.append(activity_id)
        if invalid:
            errors.append("required_activity_ids has invalid IDs: %s" % ", ".join(invalid))
            return ()
        found = {
            str(pk) for pk in Activity.objects.filter(
                id__in=ids, is_deleted=False
            ).values_list("id", flat=True)
        }
        missing = [activity_id for activity_id in ids if str(uuid.UUID(activity_id)) not in found]
        if missing:
            errors.append(
                "required_activity_ids refers to Activities that do not exist: %s"
                % ", ".join(missing)
            )
            return ()
        # Keep the plan's order and drop repeats.
        return tuple(dict.fromkeys(str(uuid.UUID(activity_id)) for activity_id in ids))
