"""Programme-wide settings the rule reads from ``BenefitPlan.json_ext``.

They describe the programme, not one payment, so every PaymentPlan of the programme
shares them. The programme form edits them like ``data_source``.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from calcrule_attendance_grant.grants import GRANT_TYPES

TRAINING_DISQUALIFICATION_PERCENTAGE_KEY = "training_disqualification_percentage"
ALLOWED_GRANT_TYPES_KEY = "allowed_grant_types"


@dataclass(frozen=True)
class ProgrammeSettings:
    # Grant types the programme allows: CASH (UPG); CASH, EQUIPMENT (RMEP);
    # CASH, EQUIPMENT, SERVICE (UMEP).
    allowed_grant_types: tuple = ("CASH",)
    # Set for a programme (UMEP) that disqualifies participants when a training is
    # completed. Its trainings count only once COMPLETED.
    training_disqualification_percentage: Decimal = None

    @property
    def counts_completed_trainings_only(self):
        return self.training_disqualification_percentage is not None

    @classmethod
    def from_benefit_plan(cls, benefit_plan, errors):
        """Read the settings, adding a message to ``errors`` for each invalid one."""
        json_ext = benefit_plan.json_ext if isinstance(benefit_plan.json_ext, dict) else {}
        disqualification = json_ext.get(TRAINING_DISQUALIFICATION_PERCENTAGE_KEY)
        if disqualification in (None, ""):
            disqualification = None
        else:
            disqualification = _percentage(disqualification)
            if disqualification is None:
                errors.append(
                    "the programme's %s must be a number above 0 and at most 100"
                    % TRAINING_DISQUALIFICATION_PERCENTAGE_KEY
                )

        allowed = json_ext.get(ALLOWED_GRANT_TYPES_KEY)
        if not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed) \
                or any(grant_type not in GRANT_TYPES for grant_type in allowed):
            errors.append(
                "the programme's %s must list one or more of %s without repeats"
                % (ALLOWED_GRANT_TYPES_KEY, ", ".join(GRANT_TYPES))
            )
            allowed = ()
        allowed = tuple(grant_type for grant_type in GRANT_TYPES if grant_type in allowed)
        return cls(
            allowed_grant_types=allowed,
            training_disqualification_percentage=disqualification,
        )


def _percentage(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and 0 < number <= 100 else None
