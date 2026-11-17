"""Grant amount A and the tranche paid from it."""
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from calcrule_attendance_grant.parameters import GRANT_SOURCE_PAYMENT_PLAN

CENT = Decimal("0.01")


@dataclass(frozen=True)
class GrantAmount:
    grant_amount: Decimal = None
    # Why there is no amount for this participant, when grant_amount is None.
    reason: str = None


def resolve_grant_amount(beneficiary, params):
    """A for this participant: the plan's grant_amount for UPG, or the beneficiary's
    approved amount (RMEP grant, UMEP programme contribution).

    A beneficiary amount must be a positive JSON number. Text is refused rather
    than guessed at, so a mistyped amount is reported instead of paid.
    """
    if params.grant_amount_source == GRANT_SOURCE_PAYMENT_PLAN:
        return GrantAmount(grant_amount=params.grant_amount)

    key = params.beneficiary_grant_amount_key
    json_ext = beneficiary.json_ext if isinstance(beneficiary.json_ext, dict) else {}
    if key not in json_ext or json_ext[key] is None:
        return GrantAmount(reason=f"{key} is missing")
    value = json_ext[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return GrantAmount(reason=f"{key} must be a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        return GrantAmount(reason=f"{key} must be a number, got {value!r}")
    amount = Decimal(str(value))
    if amount <= 0:
        return GrantAmount(reason=f"{key} must be positive, got {value!r}")
    return GrantAmount(grant_amount=amount)


def tranche_amount(grant_amount, tranche_percentage):
    """A x tranche_percentage, rounded half up to the cent."""
    return (grant_amount * tranche_percentage / 100).quantize(CENT, rounding=ROUND_HALF_UP)
