"""The tranche-two gates: tranche one paid in full, and its utilization.

Tranche two waits until every part of tranche one was paid or delivered. Then, the programme assesses how the first was used and records
it on the beneficiary's ``json_ext``: UPG and RMEP as ``utilization_percentage`` (the
share of the first tranche used for its intended purpose), UMEP as
``utilization_confirmed`` (``YES`` or ``NO``), both with ``utilization_assessed_on``.
"""
import math
from decimal import Decimal

from payroll.models import BenefitConsumptionStatus

from calcrule_attendance_grant.grants import GRANT_COMPONENTS, grant_from_record
from calcrule_attendance_grant.parameters import UTILIZATION_PERCENTAGE


def tranche_one_shortfall(records):
    """Why the beneficiary's tranche one, its recorded benefits (strategy.tranche_benefits),
    is not paid in full, or None.

    Every part of the recorded split with a non-zero amount, cash and in-kind, needs its
    own benefit: a part never paid, or whose benefit was rejected or deleted, leaves the
    tranche incomplete. Every benefit must then be RECONCILED.
    """
    if not records:
        return "no tranche-one payment of the programme"
    recorded = grant_from_record(records[0])
    paid_components = {record.get("grant_component") for record in records}
    missing = ["%s (%s)" % (component, recorded.part(component)) for component in GRANT_COMPONENTS
               if recorded.part(component) > 0 and component not in paid_components]
    if missing:
        return "tranche one has no benefit for its %s part" % " and ".join(missing)
    unreconciled = [record for record in records if record.get("_status") != BenefitConsumptionStatus.RECONCILED]
    if unreconciled:
        return "tranche one is not reconciled yet (%s of %s part(s) not RECONCILED)" % (
            len(unreconciled), len(records))
    return None


def utilization_shortfall(json_ext, measure, minimum_percentage):
    """Why the beneficiary's utilization does not release tranche two, or None."""
    json_ext = json_ext if isinstance(json_ext, dict) else {}
    assessed_on = json_ext.get("utilization_assessed_on")
    if assessed_on is None or (isinstance(assessed_on, str) and not assessed_on.strip()):
        return "utilization not assessed (no utilization_assessed_on)"
    if measure == UTILIZATION_PERCENTAGE:
        value = json_ext.get("utilization_percentage")
        if value is None:
            return "utilization_percentage is missing"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "utilization_percentage must be a number, got %r" % (value,)
        if Decimal(str(value)) < minimum_percentage:
            return "utilization %s%% below %s%%" % (_format(Decimal(str(value))), _format(minimum_percentage))
        return None
    confirmed = json_ext.get("utilization_confirmed")
    if confirmed != "YES":
        return "utilization not confirmed (utilization_confirmed is %r)" % (confirmed,)
    return None


def _format(value):
    return f"{value.normalize():f}"
