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

# Each tranche's share of a grant that includes cash, unless the plan sets its own.
DEFAULT_TRANCHE_PERCENTAGES = {1: Decimal("70"), 2: Decimal("30")}
DEFAULT_TRANCHE_PERCENTAGE = DEFAULT_TRANCHE_PERCENTAGES[1]
DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE = Decimal("80")
DEFAULT_MINIMUM_UTILIZATION_PERCENTAGE = Decimal("80")
TRANCHES = (1, 2)
UTILIZATION_PERCENTAGE = "percentage"
UTILIZATION_YES_NO = "yes_no"
UTILIZATION_MEASURES = (UTILIZATION_PERCENTAGE, UTILIZATION_YES_NO)


def _is_blank(value):
    # "null" is the Payment Plan form's empty choice.
    return value is None or (isinstance(value, str) and value.strip() in ("", "null"))


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
    # 1 or 2. Tranche two is paid in cash, once the first tranche is reconciled and
    # the beneficiary's utilization assessment meets the plan.
    tranche: int = 1
    utilization_measure: str = None
    minimum_utilization_percentage: Decimal = DEFAULT_MINIMUM_UTILIZATION_PERCENTAGE

    @classmethod
    def from_payment_plan(cls, payment_plan):
        """Read and validate the plan's parameters.

        Raises AttendanceGrantConfigurationError listing every problem found.
        """
        errors = []
        json_ext = payment_plan.json_ext if isinstance(payment_plan.json_ext, dict) else {}
        rule = json_ext.get("calculation_rule")
        rule = rule if isinstance(rule, dict) else {}

        tranche = cls.tranche_of(rule, errors)

        component = None if _is_blank(rule.get("grant_component")) else rule.get("grant_component")
        if component not in GRANT_COMPONENTS:
            errors.append("grant_component must be one of %s" % ", ".join(GRANT_COMPONENTS))
        elif tranche == 2 and component == GRANT_COMPONENT_IN_KIND:
            errors.append("tranche two is paid in cash only, so its plans need grant_component cash")

        utilization_measure = None if _is_blank(rule.get("utilization_measure")) else rule.get("utilization_measure")
        if tranche == 2 and utilization_measure not in UTILIZATION_MEASURES:
            errors.append("a tranche-two plan needs utilization_measure, one of %s" % ", ".join(UTILIZATION_MEASURES))
        minimum_utilization = cls._percentage(rule, "minimum_utilization_percentage",
                                              DEFAULT_MINIMUM_UTILIZATION_PERCENTAGE, errors)

        source = None if _is_blank(rule.get("grant_amount_source")) else rule.get("grant_amount_source")
        # Tranche two pays against the grant recorded with tranche one, so it needs no source.
        if tranche == 1 and source not in GRANT_SOURCES:
            errors.append(
                "grant_amount_source must be one of %s" % ", ".join(GRANT_SOURCES)
            )

        grant_amount = None
        if tranche == 1 and source == GRANT_SOURCE_PAYMENT_PLAN:
            grant_amount = _parse_decimal(rule.get("grant_amount"))
            if grant_amount is None or grant_amount <= 0:
                errors.append(
                    "grant_amount must be a positive number when grant_amount_source "
                    "is %s" % GRANT_SOURCE_PAYMENT_PLAN
                )

        tranche_percentage = cls._percentage(
            rule, "tranche_percentage", DEFAULT_TRANCHE_PERCENTAGES[tranche], errors
        )
        # The modules come from each beneficiary's training, a tick always means
        # attended, and the programme decides which training status counts.
        programme = ProgrammeSettings.from_benefit_plan(payment_plan.benefit_plan, errors)
        # Tranche two checks no attendance: tranche one already required it.
        minimum_attendance = cls._minimum_attendance(rule, programme, errors) if tranche == 1 \
            else DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE
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
            tranche=tranche,
            utilization_measure=utilization_measure,
            minimum_utilization_percentage=minimum_utilization,
        )

    @classmethod
    def tranche_of(cls, rule, errors=None):
        """The tranche a plan pays, 1 when unset (the form stores choices as text or numbers)."""
        value = rule.get("tranche")
        if _is_blank(value):
            return 1
        number = _parse_decimal(value)
        if number is None or number not in TRANCHES:
            if errors is not None:
                errors.append("tranche must be 1 or 2")
            return 1
        return int(number)

    @classmethod
    def tranche_percentage_of(cls, rule):
        """A plan's tranche percentage, its tranche's default when unset or invalid."""
        return cls._percentage(rule, "tranche_percentage", DEFAULT_TRANCHE_PERCENTAGES[cls.tranche_of(rule)], [])

    @classmethod
    def _minimum_attendance(cls, rule, programme, errors):
        """The plan's attendance minimum. A programme that disqualifies participants
        below a percentage (UMEP) needs it set, at least that high, so a plan saved
        without it never pays at the general default."""
        disqualification = programme.training_disqualification_percentage
        if disqualification is not None and _is_blank(rule.get("minimum_attendance_percentage")):
            errors.append("minimum_attendance_percentage is required for this programme (at least %s)"
                          % f"{disqualification.normalize():f}")
            return DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE
        minimum = cls._percentage(rule, "minimum_attendance_percentage",
                                  DEFAULT_MINIMUM_ATTENDANCE_PERCENTAGE, errors)
        if disqualification is not None and minimum < disqualification:
            errors.append("minimum_attendance_percentage must be at least the programme's "
                          "training_disqualification_percentage (%s)" % f"{disqualification.normalize():f}")
        return minimum

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
