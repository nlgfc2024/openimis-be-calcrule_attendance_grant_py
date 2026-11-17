# openIMIS Backend calcrule_attendance_grant module

Calculation rule that pays training grants in tranches, based on each participant's
attendance of the required training modules. Install it only in hubs that run
training programmes (Jobs-Now); other deployments do not need it.

## Django app

```
calcrule_attendance_grant
```

## Depends on

- `openimis-be-core`, `openimis-be-calculation`, `openimis-be-contribution_plan`
- `openimis-be-invoice`, `openimis-be-payroll`
- `openimis-be-individual`, `openimis-be-social_protection`
- `openimis-be-project_social_protection`, with `Project.project_type` (training
  sessions are projects of type `TRAINING`)
- `openimis-be-calcrule_social_protection` (its beneficiary converters are reused)

## How a payroll run is calculated

Select "Calculation rule: attendance grant" on a PaymentPlan of an INDIVIDUAL
benefit plan. Each PaymentPlan is one programme tranche. When a payroll is created,
the rule looks at the ACTIVE beneficiaries in the payroll's selection and pays a
participant when all of the following hold:

1. **Attendance.** For every required training module (Activity), the participant
   attended at least `minimum_attendance_percentage` of a training session of that
   module in the same benefit plan:

   completion = attended days / scheduled days x 100

   An attended day is a time entry whose `percent_complete` reaches
   `attended_day_threshold`. The scheduled days are the session's `working_days`.
   Each module is evaluated on its own; full attendance of one module never makes up
   for another. If the participant attended several sessions of the same module, the
   best one counts. Only individual enrollments are read: group time entries are
   never evidence of a participant's attendance.
2. **No earlier payment.** The participant has no BenefitConsumption under the same
   PaymentPlan, unless it was rejected or deleted.
3. **A valid grant amount A** (see below), giving a tranche above zero.

The tranche paid is `A x tranche_percentage`, rounded half up to the cent. The rule
creates a Bill and a BenefitConsumption for the participant, attaches them to the
payroll, and records how the amount was calculated in the BenefitConsumption:

```json
{"extra_info": {"grant_amount": 1500.0, "tranche_percentage": 70.0, "tranche_amount": 1050.0}}
```

### Grant amount A

| Programme | `grant_amount_source` | A |
|---|---|---|
| UPG | `payment_plan` | The plan's `grant_amount`, the same for everyone. |
| RMEP | `beneficiary_json` | The beneficiary's approved grant, `json_ext.approved_grant_amount`. |
| UMEP | `beneficiary_json` | The beneficiary's programme contribution, `json_ext.approved_grant_amount`. |

A beneficiary amount must be a positive JSON number. A missing amount, text such as
`"1500"`, zero or a negative number excludes that participant.

### Errors

- **Plan configuration errors stop the run.** The payroll is not created and the
  error lists every problem, for example a missing `grant_amount` or unknown Activity
  IDs.
- **Participant problems exclude only that participant.** Each excluded participant
  is logged with the beneficiary and individual IDs and the reason, for example
  `module 'Business skills': attended 3 of 5 days (60.0%), below 80%`. Nobody is paid
  zero.

## PaymentPlan parameters

Stored under `PaymentPlan.json_ext.calculation_rule`. Values may be numbers or text.

| Parameter | Default | Meaning |
|---|---|---|
| `grant_amount_source` | required | `payment_plan` or `beneficiary_json`. |
| `grant_amount` | required for `payment_plan` | Programme-wide grant amount. |
| `beneficiary_grant_amount_key` | `approved_grant_amount` | Beneficiary `json_ext` key holding A for `beneficiary_json`. |
| `tranche_percentage` | `70` | Share of A this plan pays, above 0 and at most 100. |
| `minimum_attendance_percentage` | `80` | Completion each required module needs. |
| `attended_day_threshold` | `100` | `percent_complete` that counts as an attended day. |
| `required_activity_ids` | required | Activity IDs (list or comma-separated text) this plan requires. |
| `only_completed_sessions` | `true` | Count only sessions whose status is COMPLETED. |

Second-tranche plans (`requires_reconciled_payment_plan_id`) are refused for now.
