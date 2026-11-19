"""The tranche-one plan-approval gate.

Every programme approves the participant's plan before the first tranche: a
Household Plan (UPG), a Business Plan (RMEP) or a Business Investment Plan (UMEP).
The approval is recorded on the beneficiary's ``json_ext``.
"""
BUSINESS_PLAN_STATUS_KEY = "business_plan_status"
PLAN_APPROVED_ON_KEY = "plan_approved_on"
APPROVED = "APPROVED"


def plan_approval_shortfall(json_ext):
    """Why the beneficiary's plan does not count as approved, or None when it does."""
    json_ext = json_ext if isinstance(json_ext, dict) else {}
    status = json_ext.get(BUSINESS_PLAN_STATUS_KEY)
    if status != APPROVED:
        if status in (None, ""):
            return "business plan not approved (no %s)" % BUSINESS_PLAN_STATUS_KEY
        return "business plan not approved (%s is %r)" % (BUSINESS_PLAN_STATUS_KEY, status)
    approved_on = json_ext.get(PLAN_APPROVED_ON_KEY)
    if approved_on is None or (isinstance(approved_on, str) and not approved_on.strip()):
        return "business plan approved without %s" % PLAN_APPROVED_ON_KEY
    return None
