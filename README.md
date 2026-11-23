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
   result counts; completion is read for the whole payroll at once with the project
   module's `training_module_completion_for` when it is available. Training status
   does not matter, except for a programme that sets
   `training_disqualification_percentage` (UMEP): its trainings count only once
   COMPLETED.
2. **Approved plan.** `business_plan_status` is `APPROVED`, and `plan_approved_on` and
   `plan_approved_by_id` are set on the beneficiary's `json_ext`. A plan approval
   records `plan_approved_by_id`, so a plan marked approved any other way (for
   example by an upload that writes `json_ext` in SQL) is not paid.
3. **A valid grant and tranche make-up** (see below), with a non-zero part for this
   plan.
4. **No earlier payment.** The beneficiary has no BenefitConsumption, other than a
   rejected or deleted one, that records the same part (`cash` or `in_kind`) of the
   same tranche under any PaymentPlan of the programme, including a deleted or
   changed one, so a plan re-created to fix a mistake does not pay again. Once one
   part of a tranche is paid, the other part is paid from the tranche recorded on
   that payment (grant amount, types and split), not from the beneficiary's current
   values, so the two parts always add up to one share; a difference is logged. Runs for one programme take turns: the rule locks the
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
A recorded `tranche_1_cash_amount` is used as given, and then the amounts must add
up to S. A cash-only grant records nothing. A grant without cash has no cash part,
so its in-kind amounts must add up to A. Tranche two is always cash (see Tranche two).
A `cash` plan pays the cash amount, and an `in_kind` plan pays the equipment and
service amounts, so a beneficiary with both is in both payrolls and the two add up
to S. Amounts must be JSON numbers; text, missing or inconsistent amounts exclude
the beneficiary.

### Tranche two

A plan with `tranche` 2 pays the rest of a grant with cash, in cash only. It finds the
beneficiary's tranche-one payments from what each benefit recorded, under any payment
plan of the programme, and pays the beneficiary when all of the following hold:

1. **Tranche one is reconciled.** The beneficiary has at least one tranche-one
   benefit, and every one that was not rejected or deleted is RECONCILED: the cash was
   paid and the equipment or service delivered.
2. **The grant includes cash.** A grant without cash was given in full at tranche
   one, so the beneficiary is skipped.
3. **The utilization assessment meets the plan.** `utilization_assessed_on` is set,
   and `utilization_percentage` is at least `minimum_utilization_percentage` (a value
   exactly at the minimum is eligible) for the `percentage` measure, or
   `utilization_confirmed` is `YES` for `yes_no`.
4. **No earlier tranche-two benefit**, other than a rejected or deleted one.

Attendance, plan approval and grant types are not checked again. The share is
`A x tranche_percentage`, with A and the grant types recorded on the tranche-one
benefit, not the beneficiary's current values; a difference is logged. The benefit
records `tranche` 2 in its `attendance_grant` block.

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

## Protected beneficiary fields

The keys that size and release the grant payments sit on the beneficiary's
`json_ext` (`GroupBeneficiary` for UPG and RMEP, `Beneficiary` for UMEP). The
beneficiary forms render every `json_ext` key as an editable field, and the update
mutations accept the whole `json_ext`, so the module protects them:

| Keys | How they change |
|---|---|
| Grant: `grant_types`, `approved_grant_amount`, `tranche_1_cash_amount`, `tranche_1_equipment_amount`, `tranche_1_service_amount`, `tranche_2_cash_amount`, `tranche_2_equipment_amount`, `tranche_2_service_amount`, `business_plan_reference` | Only through a plan approval (below): submitted with right `210001`. |
| Decision: `business_plan_status`, `plan_approved_on`, `plan_approved_by`, `plan_approved_by_id`, `plan_rejection_reason` | Only through a plan approval: recorded when a user with right `210003` decides. |
| Utilization: `utilization_percentage`, `utilization_confirmed`, `utilization_assessed_on`, `utilization_assessed_by`, `utilization_notes` | Saved directly by users with right `210002`, from the beneficiary page with the `recordUtilisationAssessment` mutation (below) or the upload. A result needs `utilization_assessed_on` and `utilization_assessed_by`. |

Before-hooks on `beneficiary_service.create` / `update` and
`group_beneficiary_service.create` / `update` reject any other change to these keys,
including by administrators, and generic beneficiary update tasks cannot carry
grant or decision keys. Unchanged values pass, so other fields can still be saved;
values are compared by meaning, so `700`, `700.0` and `"700"` are the same. With
maker-checker, a utilization change is checked when the update task is created and
again when each approver approves it; rejecting a generic task needs no right.

### Plan approval

Plan approval uses the CoreMIS maker-checker tasks:

1. A user with `210001` submits a beneficiary's grant keys with the
   `submitBeneficiaryPlan` mutation (`id`, `isGroup`, `jsonExt`). The proposal is
   checked (grant types the programme allows; amounts as numbers, at most two
   decimals, the grant above 0) and becomes a task with source and business event
   `calcrule_attendance_grant.plan_approval`, holding the proposed values. The
   beneficiary does not change yet. Only one approval can be open per beneficiary:
   the beneficiary is locked while a submission is checked and created, so two
   requests cannot both create one. Once any part of the first tranche is paid, the
   grant types, amount and tranche amounts can no longer change; the business plan
   reference can.
2. The task goes to the task group whose `task_sources` include
   `calcrule_attendance_grant.plan_approval`. Its executors approve or reject it with
   the standard task resolution, under the group's completion policy. Deciding needs
   right `210003`, access to every programme the task touches, and a place among the
   group's executors; IMIS administrators may decide any approval. A rejection needs a reason, given as the resolution's
   `additional_data` (`{"reason": "..."}`).
3. When the task completes, the proposed values are saved with
   `business_plan_status = APPROVED`, and the user who completed it is recorded as
   `plan_approved_by` (name), `plan_approved_by_id` and `plan_approved_on` (today).
   When it fails, the proposal is dropped; a plan that was never approved gets
   `business_plan_status = REJECTED` and the reason as `plan_rejection_reason`, while
   a rejected change to an approved plan leaves the approval as it was, with the
   reason on the task. A decision is saved on all of a task's beneficiaries or none;
   an upload whose decision cannot be saved ends as `FAIL` with the error.

Who may submit and who may decide is set by roles and task groups, as elsewhere in
CoreMIS: an administrator can let one user do both, or give the decision to others.

The task data names each beneficiary (`label`: the person's name or the household
code) for the reviewer. The `pendingBeneficiaryPlanApproval(beneficiaryId, isGroup)`
query returns a beneficiary's waiting approval (task, form or upload, submitter,
date and proposed values) to users holding any of the three rights, since makers
cannot see tasks outside their task groups.

### Utilization assessment from the beneficiary page

The `recordUtilisationAssessment` mutation (`id`, `isGroup`, `jsonExt` with
`utilization_percentage` or `utilization_confirmed`, and `utilization_notes`) needs
right `210002` and access to the programme. It merges only these keys into the
beneficiary's current `json_ext` under a row lock, so a change made elsewhere since
the page loaded is kept, and keeps the record's own status. A new or changed result
or note is dated today and signed by the user; clearing the result removes the date
and author. It saves through the beneficiary services, as an update task when the
beneficiary update mutations create tasks (`gql_check_beneficiary_crud`,
`gql_check_group_beneficiary_crud`).

The generic "Python Beneficiaries Update" upload writes `json_ext` in SQL, so these
hooks do not see it. It only writes keys declared in the programme's
`beneficiary_data_schema`, so do not declare the grant and decision keys there.

**Deployment:** assign `210001` to the roles that enter grant details, `210003` to
the roles that approve business plans, and `210002` to the roles that record
utilization assessments. Create a task group with
`{"task_sources": ["calcrule_attendance_grant.plan_approval"]}` and add the approvers
as its executors.

## Grant and utilization upload

The module registers the upload workflow "Python Beneficiary Grant and Utilization
Update" in the `socialProtection` group, so it appears in the programme's existing
beneficiary upload dialog for GROUP and INDIVIDUAL programmes alike. A file holds
either grant details or utilization assessments:

- **Grant details** (`grant_types`, `approved_grant_amount`, the per-tranche amounts,
  `business_plan_reference`) go for approval. The file's valid rows become one plan
  approval task (business event `calcrule_attendance_grant.plan_approval_upload`,
  task source `calcrule_attendance_grant.plan_approval`), and the upload waits as
  `WAITING_FOR_VERIFICATION`. Nothing is saved until it is decided, as in
  [Plan approval](#plan-approval): approval saves every row with `APPROVED` and the
  approver, and the upload ends as `SUCCESS`, or `PARTIAL_SUCCESS` when some rows
  were invalid; rejection records `REJECTED` and the reason on every row, and the
  upload ends as `FAIL`.
- **Utilization assessments** (`utilization_percentage` or `utilization_confirmed`,
  `utilization_assessed_on`, `utilization_assessed_by`, `utilization_notes`) are saved
  through the beneficiary services. For UMEP, with
  `enable_maker_checker_for_beneficiary_update` on, each row becomes a beneficiary
  update task. Household beneficiaries have no update task, so UPG and RMEP rows are
  saved directly.

Every file:

- needs an `ID` column: the GroupBeneficiary ID for UPG and RMEP, the Beneficiary ID
  for UMEP. Any other column must be a grant or utilization key. Plan status and
  approval columns are refused (the approval records them), as are tranche-two amount
  columns (tranche two is paid in cash as its plan's share), as are unknown columns
  and files that mix grant and utilization columns; a refused file changes nothing.
- needs access to the programme and the right for its columns: `210001` for grant
  details, `210002` for utilization, or the whole upload is refused.
- is checked row by row: the beneficiary belongs to the programme and appears in one
  row only; a grant row has at least one value, its beneficiary has no other
  approval waiting, and it does not change the grant of a beneficiary whose first
  tranche is being paid (the file's beneficiaries are locked until its task is
  created); `approved_grant_amount` is positive and per-tranche amounts are 0
  or more, with at most two decimals; `utilization_confirmed` is `YES` or `NO`;
  `utilization_percentage` is from 0 to 100; dates are `YYYY-MM-DD` or spreadsheet
  dates; a utilization result has its date and author.
- reads `grant_types` as a semicolon-separated list such as `CASH;EQUIPMENT`, limited
  to the programme's `allowed_grant_types` and stored as a JSON list. A tranche-one
  amount can be non-zero only for a recorded grant type. Whether a tranche adds up to
  its share is checked by the payroll, against the payment plan's percentage.
- merges only the submitted keys, as numbers where they are numbers. A blank cell is
  not submitted, so an upload never clears a value.

Invalid rows are listed with their errors in the existing invalid-items download.

**Deployment:** do not declare the grant, decision or utilization keys in the
programme's `beneficiary_data_schema`: this upload does not need them there, and the
generic "Python Beneficiaries Update" upload would then write them without the
rights or the approval. Set `allowed_grant_types` to `["CASH"]` for UPG,
`["CASH", "EQUIPMENT"]` for RMEP and all three for UMEP.

## Settings

PaymentPlan parameters, entered on the Payment Plan form and stored under
`PaymentPlan.json_ext.calculation_rule`. Values may be numbers or text; the form's
empty choice (`null`) counts as not set.

| Parameter | Default | Meaning |
|---|---|---|
| `tranche` | `1` | `1` or `2`: the tranche this plan pays. |
| `grant_component` | required | `cash` or `in_kind`: the part of each tranche this plan pays. Tranche two is `cash` only. |
| `grant_amount_source` | required | `payment_plan` or `beneficiary_json`. |
| `grant_amount` | required for `payment_plan` | Programme-wide grant amount. |
| `tranche_percentage` | `70` for tranche one, `30` for tranche two | The tranche's share of A for grants that include cash. A tranche's cash and in-kind plans must use the same value: a plan whose value differs from another attendance-grant plan of the programme for the same tranche stops with a configuration error. |
| `minimum_attendance_percentage` | `80`; required, and at least `training_disqualification_percentage`, for a programme that sets it (UMEP: 90) | Completion each module needs. |

| `utilization_measure` | required for tranche two | `percentage` (UPG, RMEP) or `yes_no` (UMEP): how the beneficiary's utilization assessment releases tranche two. |
| `minimum_utilization_percentage` | `80` | The share of tranche one that must have been used for its purpose, for the `percentage` measure. |

`grant_amount_source` and `grant_amount` apply to tranche one only: tranche two pays
against the grant recorded with tranche one.

Programme settings, on `BenefitPlan.json_ext`:

| Key | Meaning |
|---|---|
| `allowed_grant_types` | Required. `["CASH"]` for UPG, `["CASH", "EQUIPMENT"]` for RMEP, all three for UMEP. |
| `training_disqualification_percentage` | Set for UMEP (90): only COMPLETED trainings count. |

Module settings (`ModuleConfiguration` for `calcrule_attendance_grant`):

| Key | Default | Meaning |
|---|---|---|
| `gql_plans_and_grants_perms` | `["210001"]` | Right to submit grant details for approval. |
| `gql_utilization_assessment_perms` | `["210002"]` | Right to change the utilization keys. |
| `gql_plan_approval_perms` | `["210003"]` | Right to approve or reject submitted business plans. |
