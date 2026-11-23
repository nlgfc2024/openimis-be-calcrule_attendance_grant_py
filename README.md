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
`A x 100%` otherwise. The beneficiary records how S splits into
`tranche_1_cash_amount`, `tranche_1_equipment_amount` and `tranche_1_service_amount`;
they must use only the grant types and add up to S. A cash-only grant needs no split.
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
beneficiary forms render every `json_ext` key as an editable field, and the update
mutations accept the whole `json_ext`, so the module protects them:

| Keys | How they change |
|---|---|
| Grant: `grant_types`, `approved_grant_amount`, `tranche_1_cash_amount`, `tranche_1_equipment_amount`, `tranche_1_service_amount`, `tranche_2_cash_amount`, `tranche_2_equipment_amount`, `tranche_2_service_amount`, `business_plan_reference` | Only through a plan approval (below): submitted with right `210001`. |
| Decision: `business_plan_status`, `plan_approved_on`, `plan_approved_by`, `plan_approved_by_id`, `plan_rejection_reason` | Only through a plan approval: recorded when a user with right `210003` decides. |
| Utilization: `utilization_percentage`, `utilization_confirmed`, `utilization_assessed_on`, `utilization_assessed_by`, `utilization_notes` | Saved directly by users with right `210002`. A result needs `utilization_assessed_on` and `utilization_assessed_by`. |

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
   beneficiary does not change yet, and only one approval can be open per beneficiary.
2. The task goes to the task group whose `task_sources` include
   `calcrule_attendance_grant.plan_approval`. Its executors approve or reject it with
   the standard task resolution, under the group's completion policy. Deciding needs
   right `210003` and a place among the group's executors; IMIS administrators may
   decide any approval. A rejection needs a reason, given as the resolution's
   `additional_data` (`{"reason": "..."}`).
3. When the task completes, the proposed values are saved with
   `business_plan_status = APPROVED`, and the user who completed it is recorded as
   `plan_approved_by` (name), `plan_approved_by_id` and `plan_approved_on` (today).
   When it fails, the proposal is dropped, `business_plan_status = REJECTED` and the
   reason is recorded as `plan_rejection_reason`.

Who may submit and who may decide is set by roles and task groups, as elsewhere in
CoreMIS: an administrator can let one user do both, or give the decision to others.

The generic "Python Beneficiaries Update" upload writes `json_ext` in SQL, so these
hooks do not see it. It only writes keys declared in the programme's
`beneficiary_data_schema`, so do not declare the grant and decision keys there.

**Deployment:** assign `210001` to the roles that enter grant details, `210003` to
the roles that approve business plans, and `210002` to the roles that record
utilization assessments. Create a task group with
`{"task_sources": ["calcrule_attendance_grant.plan_approval"]}` and add the approvers
as its executors.

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
| `gql_plans_and_grants_perms` | `["210001"]` | Right to submit grant details for approval. |
| `gql_utilization_assessment_perms` | `["210002"]` | Right to change the utilization keys. |
| `gql_plan_approval_perms` | `["210003"]` | Right to approve or reject submitted business plans. |
