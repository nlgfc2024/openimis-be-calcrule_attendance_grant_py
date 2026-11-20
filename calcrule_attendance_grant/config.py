# The rule's PaymentPlan parameters are read and defaulted in parameters.py.
# Their Payment Plan UI declarations are added to this PaymentPlan entry.
CLASS_RULE_PARAM_VALIDATION = [
    {
        "class": "PaymentPlan",
        "parameters": [],
    },
]

FROM_TO = None

DESCRIPTION_ATTENDANCE_GRANT_CALCULATION = (
    "Calculation rule for training grants paid in tranches. A beneficiary is paid "
    "the cash or in-kind part of a tranche of their grant when every module of their "
    "training reaches the plan's attendance minimum and their plan is approved."
)
