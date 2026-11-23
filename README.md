# openIMIS Backend calcrule_attendance_grant module

Calculation rule that pays training grants in tranches. A beneficiary is paid when
every module of their training reaches the plan's attendance minimum and their plan
is approved. Cash and in-kind (equipment, service) parts of a tranche are paid by
separate PaymentPlans. Install it only in the training instances (UPG and RMEP;
UMEP); other deployments do not need it.

## Django app

```
calcrule_attendance_grant
```

## Depends on

- `openimis-be-core`, `openimis-be-calculation`, `openimis-be-contribution_plan`
- `openimis-be-invoice`, `openimis-be-payroll`
- `openimis-be-individual`, with `individual.primary_recipients` (the shared
  resolver of a verified household's primary worker), and `openimis-be-social_protection`
- `openimis-be-project_social_protection`, with training support: `Project.project_type`
  (trainings are projects of type `TRAINING`), training topic sheets (`ProjectTopic`)
  and the per-module completion helper `training_topics.training_module_completion`
- `openimis-be-calcrule_social_protection` (its Bill and benefit converters are reused)

## Programmes and who is paid

| Programme | Benefit plan | Beneficiary | Participant paid |
|---|---|---|---|
| UPG, RMEP | GROUP | The household (`GroupBeneficiary`) | Its primary worker |
| UMEP | INDIVIDUAL | The `Beneficiary` | Its individual |

A household's primary worker comes from `individual.primary_recipients`, the
resolver enrolment, KYC and payments share: a member marked as primary worker in a
VERIFIED household. A household without exactly one such member is excluded and
logged. The household is the Bill subject; the benefit is made out to
the primary worker. For GROUP plans, PayrollService then makes each household
benefit payable to the member its payroll settings name, so the payroll module's
`group_benefit_payee` must be `PRIMARY_WORKER` (openimis-be-payroll_py, "support
training payrolls and primary-worker payments"): otherwise a GROUP plan's run stops
with a configuration error, as the person checked and the person paid would differ.

## How a payroll run is calculated

Select "Calculation rule: attendance grant" on a PaymentPlan. Each PaymentPlan pays
one part of one tranche. When a payroll is created, the rule takes the ACTIVE
beneficiaries in the payroll's selection (the household enrollments PayrollService
passes for a GROUP plan, the beneficiaries for an INDIVIDUAL one) and pays a
beneficiary when all of the following hold:

1. **Attendance.** A training covers all of its programme's modules and ticks topics
   on one sheet. For every module of the beneficiary's training:

   completion = ticked topics of the module / topics of the module x 100

   must reach `minimum_attendance_percentage`. Each module is evaluated on its own.
   If the beneficiary has several trainings of the programme, each module's best
   result counts. Training status does not matter, except for a programme that sets
   `training_disqualification_percentage` (UMEP): its trainings count only once
   COMPLETED.
2. **Approved plan.** `business_plan_status` is `APPROVED`, and `plan_approved_on` and
   `plan_approved_by_id` are set on the beneficiary's `json_ext`. A plan approval
   records `plan_approved_by_id`, so a plan marked approved any other way (for
   example by an upload that writes `json_ext` in SQL) is not paid.
3. **A valid grant and tranche make-up** (see below), with a non-zero part for this
   plan.
4. **No earlier payment.** The beneficiary has no BenefitConsumption, other than a
   rejected or deleted one, under any attendance-grant PaymentPlan of the programme
   that pays the same part (`cash` or `in_kind`), so a plan re-created to fix a
   mistake does not pay again. Runs for one programme take turns: the rule locks the
   programme until the payroll is saved, so two runs never both pay a person.

### Grant amount, share and parts

The grant types are the beneficiary's `json_ext.grant_types`, limited to the
programme's `allowed_grant_types`. A programme that allows one type (UPG: `CASH`)
records none. The grant amount A is:

| Programme | `grant_amount_source` | A |
|---|---|---|
| UPG | `payment_plan` | The plan's `grant_amount`, the same for everyone. |
| RMEP | `beneficiary_json` | The beneficiary's approved grant, `json_ext.approved_grant_amount`. |
| UMEP | `beneficiary_json` | The beneficiary's programme contribution, `json_ext.approved_grant_amount`. |

The tranche share S is `A x tranche_percentage` when the grant includes `CASH`, and
`A x 100%` otherwise. The beneficiary records the in-kind amounts the tranche
delivers, `tranche_1_equipment_amount` and `tranche_1_service_amount`, for its grant
types. The cash part is the rest of S, so a different `tranche_percentage` on the
PaymentPlan changes only the cash; in-kind amounts above S exclude the beneficiary.
A recorded `tranche_<n>_cash_amount` is used as given, and then the amounts must add
up to S. A cash-only grant records nothing, and tranche two is S in cash unless it
records in-kind amounts. A grant without cash has no cash part, so its in-kind
amounts must add up to A.
A `cash` plan pays the cash amount, and an `in_kind` plan pays the equipment and
service amounts, so a beneficiary with both is in both payrolls and the two add up
to S. Amounts must be JSON numbers; text, missing or inconsistent amounts exclude
the beneficiary.

### What is recorded

The BenefitConsumption amount is the plan's part, and its `type` names the paid
types (`CASH`, `EQUIPMENT`, `SERVICE` or `EQUIPMENT+SERVICE`). Its `json_ext` is:

```json
{
  "attendance_grant": {
    "tranche": 1, "grant_component": "in_kind", "grant_types": ["CASH", "EQUIPMENT"],
    "grant_amount": 1000.0, "tranche_percentage": 70.0, "tranche_share": 700.0,
    "cash_amount": 200.0, "equipment_amount": 500.0, "service_amount": 0.0
  },
  "extra_info": {"equipment_amount": 500.0}
}
```

`attendance_grant` keeps the whole tranche for tranche two and reporting;
reconciliation replaces only `extra_info`. `extra_info` becomes columns of the
reconciliation file: the in-kind amounts for the PIU team on an `in_kind` plan.

### Errors

- **Plan configuration errors stop the run.** The payroll is not created and the
  error lists every problem, for example a missing `grant_component`, a missing
  `grant_amount`, or a programme without `allowed_grant_types`.
- **Beneficiary problems exclude only that beneficiary.** Each is logged with the
  beneficiary and participant IDs and the reason, for example
  `module 'Business skills': attended 3 of 5 topics (60.0%), below 80%`. Nobody is
  paid zero. Likely slips that do not stop a payment are logged as warnings, for
  example an in-kind grant type without its tranche-one amount next to a recorded
  cash amount.

## Settings

PaymentPlan parameters, entered on the Payment Plan form and stored under
`PaymentPlan.json_ext.calculation_rule`. Values may be numbers or text; the form's
empty choice (`null`) counts as not set.

| Parameter | Default | Meaning |
|---|---|---|
| `grant_component` | required | `cash` or `in_kind`: the part of each tranche this plan pays. |
| `grant_amount_source` | required | `payment_plan` or `beneficiary_json`. |
| `grant_amount` | required for `payment_plan` | Programme-wide grant amount. |
| `tranche_percentage` | `70` | The tranche's share of A for grants that include cash. A tranche's cash and in-kind plans must use the same value: a plan whose value differs from another attendance-grant plan of the programme stops with a configuration error. |
| `minimum_attendance_percentage` | `80`; required, and at least `training_disqualification_percentage`, for a programme that sets it (UMEP: 90) | Completion each module needs. |

Second-tranche plans (`requires_reconciled_payment_plan_ids`) are refused for now.

Programme settings, on `BenefitPlan.json_ext`:

| Key | Meaning |
|---|---|
| `allowed_grant_types` | Required. `["CASH"]` for UPG, `["CASH", "EQUIPMENT"]` for RMEP, all three for UMEP. |
| `training_disqualification_percentage` | Set for UMEP (90): only COMPLETED trainings count. |
