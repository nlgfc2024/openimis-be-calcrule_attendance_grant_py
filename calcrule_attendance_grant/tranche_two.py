"""The tranche-two utilization gate.

Before the second tranche, the programme assesses how the first was used and records
it on the beneficiary's ``json_ext``: UPG and RMEP as ``utilization_percentage`` (the
share of the first tranche used for its intended purpose), UMEP as
``utilization_confirmed`` (``YES`` or ``NO``), both with ``utilization_assessed_on``.
"""
import math
from decimal import Decimal

from calcrule_attendance_grant.parameters import UTILIZATION_PERCENTAGE


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
