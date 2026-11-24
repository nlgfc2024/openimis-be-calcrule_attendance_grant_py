# The Payment Plan form's inputs for the rule. parameters.py reads, defaults and
# validates the values the form stores under PaymentPlan.json_ext["calculation_rule"].
PAYMENT_PLAN_RIGHTS = {"read": "157101", "write": "157102", "update": "157103", "replace": "157206"}
# "null" is the empty choice: a plan saved without choosing fails with a clear error.
NO_CHOICE = {"value": "null", "label": {"en": "", "fr": ""}}

CLASS_RULE_PARAM_VALIDATION = [
    {
        "class": "PaymentPlan",
        "parameters": [
            {
                "type": "select",
                "name": "tranche",
                "label": {"en": "Tranche", "fr": "Tranche"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "optionSet": [
                    {"value": "1", "label": {"en": "Tranche 1", "fr": "Tranche 1"}},
                    {"value": "2", "label": {"en": "Tranche 2 (cash)", "fr": "Tranche 2 (espèces)"}},
                ],
                "default": "1",
            },
            {
                "type": "select",
                "name": "grant_component",
                "label": {"en": "Grant part paid", "fr": "Partie de la subvention payée"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "optionSet": [
                    NO_CHOICE,
                    {"value": "cash", "label": {"en": "Cash", "fr": "Espèces"}},
                    {"value": "in_kind", "label": {"en": "Equipment and service", "fr": "Équipement et service"}},
                ],
                "default": "null",
            },
            {
                "type": "select",
                "name": "grant_amount_source",
                "label": {"en": "Grant amount from", "fr": "Montant de la subvention selon"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "optionSet": [
                    NO_CHOICE,
                    {"value": "payment_plan", "label": {"en": "This payment plan", "fr": "Ce plan de paiement"}},
                    {"value": "beneficiary_json", "label": {"en": "Each beneficiary's approved grant",
                                                            "fr": "La subvention approuvée de chaque bénéficiaire"}},
                ],
                "default": "null",
            },
            {
                "type": "number",
                "name": "grant_amount",
                "label": {"en": "Grant amount (when set on this payment plan)",
                          "fr": "Montant de la subvention (défini sur ce plan)"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "condition": "INPUT>=0",
                "default": "",
            },
            {
                "type": "number",
                "name": "tranche_percentage",
                "label": {"en": "Tranche percentage of a grant with cash", "fr": "Pourcentage de la tranche"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "condition": "INPUT>0",
                # Blank: 70 for tranche one; tranche two pays the rest of each grant.
                "default": "",
            },
            {
                "type": "number",
                "name": "minimum_attendance_percentage",
                "label": {"en": "Minimum attendance per module (%)", "fr": "Présence minimale par module (%)"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "condition": "INPUT>0",
                # No default: UMEP plans need 90, and a default would hide a missing value.
                "default": "",
            },
            {
                "type": "select",
                "name": "utilization_measure",
                "label": {"en": "Utilization measure (tranche 2)", "fr": "Mesure de l'utilisation (tranche 2)"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "optionSet": [
                    NO_CHOICE,
                    {"value": "percentage", "label": {"en": "Percentage used", "fr": "Pourcentage utilisé"}},
                    {"value": "yes_no", "label": {"en": "Used for its purpose: yes or no",
                                                  "fr": "Utilisé à bon escient : oui ou non"}},
                ],
                "default": "null",
            },
            {
                "type": "number",
                "name": "minimum_utilization_percentage",
                "label": {"en": "Minimum utilization (%, tranche 2)", "fr": "Utilisation minimale (%, tranche 2)"},
                "rights": PAYMENT_PLAN_RIGHTS,
                "relevance": "True",
                "condition": "INPUT>0",
                "default": "80",
            },
        ],
    },
]

FROM_TO = None

DESCRIPTION_ATTENDANCE_GRANT_CALCULATION = (
    "Calculation rule for training grants paid in tranches. A beneficiary is paid "
    "the cash or in-kind part of a tranche of their grant when every module of their "
    "training reaches the plan's attendance minimum and their plan is approved."
)
