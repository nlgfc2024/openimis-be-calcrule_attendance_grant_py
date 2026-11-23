"""Grant types, the grant amount A, and how a tranche splits into cash and in-kind.

A participant's grant is one or more of CASH, EQUIPMENT and SERVICE, limited to the
programme's ``allowed_grant_types``. A is the whole approved grant, including the
equivalent value of any equipment or service. A tranche pays the share

    S = A x tranche_percentage   when the grant includes cash
    S = A x 100%                 at tranche one when it does not

and the beneficiary records the amount of each in-kind type the tranche delivers
(``tranche_<n>_<type>_amount``). The cash part is the rest of S, so the tranche
percentage lives only on the PaymentPlan; a recorded cash amount is used as
given. Cash and in-kind parts are paid by separate PaymentPlans, so each plan pays
only its own part of S.

Amounts on the beneficiary must be JSON numbers. Text is refused rather than
guessed at, so a mistyped amount is reported instead of paid.
"""
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CASH = "CASH"
EQUIPMENT = "EQUIPMENT"
SERVICE = "SERVICE"
GRANT_TYPES = (CASH, EQUIPMENT, SERVICE)
IN_KIND_TYPES = (EQUIPMENT, SERVICE)

GRANT_COMPONENT_CASH = "cash"
GRANT_COMPONENT_IN_KIND = "in_kind"
GRANT_COMPONENTS = (GRANT_COMPONENT_CASH, GRANT_COMPONENT_IN_KIND)

GRANT_TYPES_KEY = "grant_types"
APPROVED_GRANT_AMOUNT_KEY = "approved_grant_amount"

CENT = Decimal("0.01")
FULL_SHARE = Decimal("100")


def tranche_amount_key(tranche, grant_type):
    return "tranche_%s_%s_amount" % (tranche, grant_type.lower())


def to_cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Grant:
    """The tranche a beneficiary is due, or why it cannot be paid."""
    reason: str = None
    grant_types: tuple = ()
    grant_amount: Decimal = None
    percentage: Decimal = None
    share: Decimal = None
    # Amount of S per grant type; every grant type has an entry.
    amounts: dict = field(default_factory=dict)
    # Likely data-entry slips that did not stop the payment, for the log.
    notes: tuple = ()

    def part(self, grant_component):
        """The part of S paid by a cash or an in-kind PaymentPlan."""
        types = (CASH,) if grant_component == GRANT_COMPONENT_CASH else IN_KIND_TYPES
        return sum((self.amounts.get(grant_type, Decimal(0)) for grant_type in types), Decimal(0))

    def part_types(self, grant_component):
        """Grant types with a non-zero amount in this plan's part, for example EQUIPMENT+SERVICE."""
        types = (CASH,) if grant_component == GRANT_COMPONENT_CASH else IN_KIND_TYPES
        return tuple(grant_type for grant_type in types if self.amounts.get(grant_type))


def resolve_grant(json_ext, *, allowed_grant_types, grant_amount_source_is_plan,
                  plan_grant_amount, tranche_percentage, tranche=1):
    """Resolve the beneficiary's tranche from its ``json_ext``."""
    json_ext = json_ext if isinstance(json_ext, dict) else {}
    grant_types, reason = _grant_types(json_ext, allowed_grant_types)
    if reason:
        return Grant(reason=reason)

    if grant_amount_source_is_plan:
        grant_amount = plan_grant_amount
    else:
        grant_amount, reason = _json_number(json_ext, APPROVED_GRANT_AMOUNT_KEY, positive=True)
        if reason:
            return Grant(reason=reason)

    percentage = tranche_percentage if CASH in grant_types else FULL_SHARE
    share = to_cents(grant_amount * percentage / 100)
    if share <= 0:
        return Grant(reason="the tranche of %s at %s%% rounds to 0" % (
            grant_amount, _format_number(percentage)))

    amounts, reason = _tranche_amounts(json_ext, grant_types, share, tranche)
    if reason:
        return Grant(reason=reason)
    return Grant(
        grant_types=grant_types, grant_amount=grant_amount, percentage=percentage,
        share=share, amounts=amounts, notes=_missing_in_kind_notes(json_ext, grant_types, tranche),
    )


def grant_from_record(record):
    """The tranche a BenefitConsumption recorded (its ``attendance_grant`` block), so the
    other part of the tranche is paid from exactly the same share and split."""
    grant_types = tuple(grant_type for grant_type in GRANT_TYPES if grant_type in (record.get("grant_types") or []))
    return Grant(
        grant_types=grant_types,
        grant_amount=to_cents(Decimal(str(record.get("grant_amount", 0)))),
        percentage=Decimal(str(record.get("tranche_percentage", 0))),
        share=to_cents(Decimal(str(record.get("tranche_share", 0)))),
        amounts={grant_type: to_cents(Decimal(str(record.get("%s_amount" % grant_type.lower(), 0))))
                 for grant_type in grant_types},
    )


def _missing_in_kind_notes(json_ext, grant_types, tranche):
    """Tranche one delivers the in-kind part, so an in-kind grant type without an amount
    there, counted as 0 because other amounts are recorded, is more likely a slip."""
    if tranche != 1:
        return ()
    return tuple(
        "%s is missing and counts as 0 although %s is one of the grant types"
        % (tranche_amount_key(tranche, grant_type), grant_type)
        for grant_type in grant_types
        if grant_type != CASH and json_ext.get(tranche_amount_key(tranche, grant_type)) is None
    )


def _grant_types(json_ext, allowed_grant_types):
    value = json_ext.get(GRANT_TYPES_KEY)
    if value is None:
        if len(allowed_grant_types) == 1:
            # A programme that allows a single type (UPG: cash) records none.
            return tuple(allowed_grant_types), None
        return (), "%s is missing" % GRANT_TYPES_KEY
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        return (), "%s must be a non-empty list, got %r" % (GRANT_TYPES_KEY, value)
    if len(set(value)) != len(value):
        return (), "%s repeats a type: %r" % (GRANT_TYPES_KEY, value)
    not_allowed = [item for item in value if item not in allowed_grant_types]
    if not_allowed:
        return (), "%s has types the programme does not allow: %s" % (
            GRANT_TYPES_KEY, ", ".join(not_allowed))
    # Keep a stable order, so the BenefitConsumption type reads the same everywhere.
    return tuple(grant_type for grant_type in GRANT_TYPES if grant_type in value), None


def _tranche_amounts(json_ext, grant_types, share, tranche):
    keys = {grant_type: tranche_amount_key(tranche, grant_type) for grant_type in GRANT_TYPES}
    recorded = {grant_type: key for grant_type, key in keys.items() if json_ext.get(key) is not None}
    label = "tranche-%s amounts" % ("one" if tranche == 1 else "two")
    in_kind_types = [grant_type for grant_type in grant_types if grant_type != CASH]

    # Tranche one delivers the in-kind part of the grant, so it needs those amounts;
    # tranche two is normally cash only.
    if tranche == 1 and in_kind_types and not recorded:
        return {}, "%s are missing (%s)" % (label, ", ".join(keys[grant_type] for grant_type in in_kind_types))

    amounts = {}
    for grant_type, key in recorded.items():
        amount, reason = _json_number(json_ext, key, positive=False)
        if reason:
            return {}, reason
        if amount and grant_type not in grant_types:
            return {}, "%s is %s but %s is not one of the grant types" % (key, amount, grant_type)
        amounts[grant_type] = to_cents(amount)

    derive_cash = CASH in grant_types and CASH not in recorded
    if derive_cash:
        in_kind = sum((amounts.get(grant_type, Decimal("0.00")) for grant_type in in_kind_types), Decimal("0.00"))
        if in_kind > share:
            return {}, "%s: equipment and service add up to %s, more than the share %s" % (label, in_kind, share)
        amounts[CASH] = share - in_kind
    amounts = {grant_type: amounts.get(grant_type, Decimal("0.00")) for grant_type in grant_types}

    total = sum(amounts.values(), Decimal("0.00"))
    if total != share:
        return {}, "%s add up to %s, expected %s" % (label, total, share)
    return amounts, None


def _json_number(json_ext, key, positive):
    if json_ext.get(key) is None:
        return None, "%s is missing" % key
    value = json_ext[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or (isinstance(value, float) and not math.isfinite(value)):
        return None, "%s must be a number, got %r" % (key, value)
    number = Decimal(str(value))
    if positive and number <= 0:
        return None, "%s must be positive, got %r" % (key, value)
    if number < 0:
        return None, "%s must not be negative, got %r" % (key, value)
    return number, None


def _format_number(value):
    return f"{value.normalize():f}" if isinstance(value, Decimal) else str(value)
