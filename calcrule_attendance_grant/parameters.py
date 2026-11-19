"""PaymentPlan parameters of the attendance-grant rule.

The Payment Plan UI stores the rule's parameters under
``PaymentPlan.json_ext["calculation_rule"]``, usually as text. This module is the
single place that names them, applies their defaults and validates them.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.grants import GRANT_COMPONENT_IN_KIND, GRANT_COMPONENTS, IN_KIND_TYPES
from calcrule_attendance_grant.programme import ProgrammeSettings

GRANT_SOURCE_PAYMENT_PLAN = "payment_plan"
GRANT_SOURCE_BENEFICIARY_JSON = "beneficiary_json"
GRANT_SOURCES = (GRANT_SOURCE_PAYMENT_PLAN, GRANT_SOURCE_BENEFICIARY_JSON)

DEFAULT_TRANCHE_PERCENTAGE = Decimal("70")
DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE = Decimal("80")


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


@dataclass(frozen=True)
class GrantParameters:
    # "cash" or "in_kind": the part of each tranche this plan pays.
    grant_component: str
    grant_amount_source: str
    grant_amount: Decimal
    tranche_percentage: Decimal
    minimum_attendance_percentage: Decimal
    # Settings of the programme (BenefitPlan) the plan pays.
    programme: ProgrammeSettings = ProgrammeSettings()

    @classmethod
    def from_payment_plan(cls, payment_plan):
        """Read and validate the plan's parameters.

        Raises AttendanceGrantConfigurationError listing every problem found.
        """
        errors = []
        json_ext = payment_plan.json_ext if isinstance(payment_plan.json_ext, dict) else {}
        rule = json_ext.get("calculation_rule")
        rule = rule if isinstance(rule, dict) else {}

        if not _is_blank(rule.get("requires_reconciled_payment_plan_id")):
            errors.append(
                "second-tranche plans (requires_reconciled_payment_plan_id) are not "
                "supported yet"
            )

        component = rule.get("grant_component")
        if component not in GRANT_COMPONENTS:
            errors.append("grant_component must be one of %s" % ", ".join(GRANT_COMPONENTS))

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

        tranche_percentage = cls._percentage(
            rule, "tranche_percentage", DEFAULT_TRANCHE_PERCENTAGE, errors
        )
        minimum_attendance = cls._percentage(
            rule, "minimum_attendance_percentage",
            DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE, errors,
        )
        # The modules come from each beneficiary's training, a tick always means
        # attended, and the programme decides which training status counts.
        programme = ProgrammeSettings.from_benefit_plan(payment_plan.benefit_plan, errors)
        if component == GRANT_COMPONENT_IN_KIND and programme.allowed_grant_types and not any(
                grant_type in programme.allowed_grant_types for grant_type in IN_KIND_TYPES):
            errors.append("an in_kind plan needs a programme that allows EQUIPMENT or SERVICE")

        if errors:
            raise AttendanceGrantConfigurationError(
                "Payment plan %s cannot be calculated with the attendance-grant rule: %s."
                % (payment_plan.code, "; ".join(errors))
            )
        return cls(
            grant_component=component,
            grant_amount_source=source,
            grant_amount=grant_amount,
            tranche_percentage=tranche_percentage,
            minimum_attendance_percentage=minimum_attendance,
            programme=programme,
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
