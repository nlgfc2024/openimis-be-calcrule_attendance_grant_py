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
benefit payable to the member its payroll settings name.

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
2. **Approved plan.** `business_plan_status` is `APPROVED` and `plan_approved_on` is set
   on the beneficiary's `json_ext`.
3. **A valid grant and tranche make-up** (see below), with a non-zero part for this
   plan.
4. **No earlier payment.** The beneficiary has no BenefitConsumption under the same
   PaymentPlan, unless it was rejected or deleted.

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
  paid zero.

## Protected beneficiary fields

The keys that size and release the grant payments sit on the beneficiary's
`json_ext` (`GroupBeneficiary` for UPG and RMEP, `Beneficiary` for UMEP). The
beneficiary forms render every `json_ext` key as an editable field, so the module
restricts them to roles holding the matching right:

| Right | Code | Protected keys |
|---|---|---|
| Manage beneficiary plans and grants | `210001` | `grant_types`, `approved_grant_amount`, `tranche_1_cash_amount`, `tranche_1_equipment_amount`, `tranche_1_service_amount`, `tranche_2_cash_amount`, `tranche_2_equipment_amount`, `tranche_2_service_amount`, `business_plan_reference`, `business_plan_status`, `plan_approved_on`, `plan_approved_by`, `plan_rejection_reason` |
| Record utilization assessments | `210002` | `utilization_percentage`, `utilization_confirmed`, `utilization_assessed_on`, `utilization_assessed_by`, `utilization_notes` |

Before-hooks on `beneficiary_service.create` / `update` and
`group_beneficiary_service.create` / `update` reject setting, changing or removing
a protected key by a user without its right. Unchanged values pass, so other fields
can still be saved. An `APPROVED` `business_plan_status` needs `plan_approved_on`
and `plan_approved_by`, a `REJECTED` one needs `plan_rejection_reason`, and a utilization result needs `utilization_assessed_on`
and `utilization_assessed_by`. IMIS administrators hold every right.

With maker-checker, the maker needs the right to create the update task
(`task_service.create`), and each approver needs it to approve the task
(`task_service.resolve_task`), so an approval they may not give fails with a clear
error. The approved task then runs as the approver and is checked again. Rejecting
a task needs no right.

The generic "Python Beneficiaries Update" upload writes `json_ext` in SQL, so these
hooks do not see it; do not use it for protected columns.

**Deployment:** assign `210001` to the roles that enter grant amounts and approve
plans, and `210002` to the roles that record utilization assessments.

## Grant and utilization upload

The module registers the upload workflow "Python Beneficiary Grant and Utilization
Update" in the `socialProtection` group, so it appears in the programme's existing
beneficiary upload dialog for GROUP and INDIVIDUAL programmes alike.

- The file needs an `ID` column: the GroupBeneficiary ID for UPG and RMEP, the
  Beneficiary ID for UMEP. Every other column must be one of the protected keys
  above; any other column fails the whole upload.
- Plan and grant columns need right `210001`, and utilization columns need `210002`,
  or the whole upload is refused.
- Each row is validated: the beneficiary belongs to the programme;
  `approved_grant_amount` is positive and per-tranche amounts are 0 or more, with at
  most two decimals; statuses are `PENDING`, `APPROVED` or `REJECTED`, and
  `utilization_confirmed` is `YES` or `NO`; `utilization_percentage` is from 0 to
  100; dates are `YYYY-MM-DD` (spreadsheet dates are converted).
- `grant_types` is a semicolon-separated list such as `CASH;EQUIPMENT`, limited to
  the programme's `allowed_grant_types` and stored as a JSON list. A per-tranche
  amount can be non-zero only for a recorded grant type, and a grant without cash
  has no tranche-two amounts.
- When a tranche's amounts do not add up to the default 70% or 30% share of
  `approved_grant_amount` (100% for a grant without cash), or, without a cash
  amount, its equipment and service exceed that share, the row gets a warning but
  is applied; the payroll checks the exact share against the payment plan.
- Only the submitted keys are merged into the beneficiary's `json_ext`, as numbers
  where they are numbers. A blank cell is not submitted, so an upload never clears a
  value; correct one through the beneficiary form instead.
- Valid rows are saved through the beneficiary services, so the rights above,
  history and maker-checker apply. Invalid rows are listed with their errors in the
  existing invalid-items download, and the upload ends as `SUCCESS`,
  `PARTIAL_SUCCESS`, `FAIL` or, when rows became approval tasks,
  `WAITING_FOR_VERIFICATION`.
- For UMEP, with `enable_maker_checker_for_beneficiary_update` on, each row becomes
  a beneficiary update task. Household beneficiaries have no update task, so UPG
  and RMEP rows apply directly.

**Deployment:** declare the keys each programme uses in its BenefitPlan
`beneficiary_data_schema`: RMEP and UMEP all plan, grant, cash and equipment tranche
and utilization keys, UMEP also the service tranche keys, and UPG only the
plan-approval and utilization keys. Set `allowed_grant_types` to `["CASH"]` for UPG,
`["CASH", "EQUIPMENT"]` for RMEP and all three for UMEP. Do not use "Python
Beneficiaries Update" for these columns: it bypasses the rights.

## Settings

PaymentPlan parameters, stored under `PaymentPlan.json_ext.calculation_rule`. Values
may be numbers or text.

| Parameter | Default | Meaning |
|---|---|---|
| `grant_component` | required | `cash` or `in_kind`: the part of each tranche this plan pays. |
| `grant_amount_source` | required | `payment_plan` or `beneficiary_json`. |
| `grant_amount` | required for `payment_plan` | Programme-wide grant amount. |
| `tranche_percentage` | `70` | The tranche's share of A for grants that include cash. |
| `minimum_attendance_percentage` | `80` | Completion each module needs; UMEP plans set 90. |

Second-tranche plans (`requires_reconciled_payment_plan_ids`) are refused for now.

Programme settings, on `BenefitPlan.json_ext`:

| Key | Meaning |
|---|---|
| `allowed_grant_types` | Required. `["CASH"]` for UPG, `["CASH", "EQUIPMENT"]` for RMEP, all three for UMEP. |
| `training_disqualification_percentage` | Set for UMEP (90): only COMPLETED trainings count. |

Module settings (`ModuleConfiguration` for `calcrule_attendance_grant`):

| Key | Default | Meaning |
|---|---|---|
| `gql_plans_and_grants_perms` | `["210001"]` | Right to change the plan and grant keys. |
| `gql_utilization_assessment_perms` | `["210002"]` | Right to change the utilization keys. |
