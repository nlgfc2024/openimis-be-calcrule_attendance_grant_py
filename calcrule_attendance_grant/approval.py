"""The tranche-one plan-approval gate.

Every programme approves the participant's plan before the first tranche: a
Household Plan (UPG), a Business Plan (RMEP) or a Business Investment Plan (UMEP).
The approval is recorded on the beneficiary's ``json_ext``.

A plan approval decision records who approved it as ``plan_approved_by_id``. A plan
marked approved without it did not come through the approval (for example a
spreadsheet written with SQL), so it does not count.
"""
BUSINESS_PLAN_STATUS_KEY = "business_plan_status"
PLAN_APPROVED_ON_KEY = "plan_approved_on"
PLAN_APPROVED_BY_ID_KEY = "plan_approved_by_id"
APPROVED = "APPROVED"


def plan_approval_shortfall(json_ext):
    """Why the beneficiary's plan does not count as approved, or None when it does."""
    json_ext = json_ext if isinstance(json_ext, dict) else {}
    status = json_ext.get(BUSINESS_PLAN_STATUS_KEY)
    if status != APPROVED:
        if status in (None, ""):
            return "business plan not approved (no %s)" % BUSINESS_PLAN_STATUS_KEY
        return "business plan not approved (%s is %r)" % (BUSINESS_PLAN_STATUS_KEY, status)
    for key in (PLAN_APPROVED_ON_KEY, PLAN_APPROVED_BY_ID_KEY):
        value = json_ext.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return "business plan approved without %s" % key
    return None
