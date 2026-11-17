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
    "Calculation rule for training grants paid in tranches. A participant is paid "
    "a tranche of their grant amount when they attended every required training "
    "module of the payment plan."
)
