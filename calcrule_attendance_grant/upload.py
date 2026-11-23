"""The "Python Beneficiary Grant and Utilization Update" upload workflow.

It records grant details or utilization assessments on each beneficiary's
``json_ext``: ``GroupBeneficiary`` for a GROUP programme (UPG, RMEP), ``Beneficiary``
for an INDIVIDUAL one (UMEP). A file holds one or the other:

- Grant details (grant types, amounts, business plan reference) are submitted for
  approval: the file's valid rows become one plan approval task (see plan_approval),
  and nothing is saved until it is decided.
- Utilization assessments are saved through the beneficiary services, so rights,
  history and, for INDIVIDUAL beneficiaries with
  ``enable_maker_checker_for_beneficiary_update``, update tasks apply. Household
  beneficiaries have no update task, so their rows are saved directly.

Unlike "Python Beneficiaries Update", it accepts only these keys, stores numbers as
numbers and merges only the submitted keys. A blank cell means "not submitted": an
upload never clears a recorded value.
"""
import datetime
import logging
import math
import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig
from calcrule_attendance_grant.grants import GRANT_TYPES, tranche_amount_key
from calcrule_attendance_grant.plan_approval import (
    FROZEN_ONCE_PAID_KEYS,
    PlanApprovalService,
    beneficiary_label,
    beneficiary_type_of,
    open_approval_beneficiary_ids,
)
from calcrule_attendance_grant.programme import ProgrammeSettings
from calcrule_attendance_grant.protected_fields import (
    GRANT_KEYS,
    PLAN_AND_GRANT_KEYS,
    UTILIZATION_KEYS,
    as_json_ext,
    changed_keys,
    check_protected_changes,
)
from calcrule_attendance_grant.strategy import tranche_one_benefits

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "Python Beneficiary Grant and Utilization Update"
WORKFLOW_GROUP = "socialProtection"
ID_COLUMN = "ID"

AMOUNT_KEYS = tuple(key for key in GRANT_KEYS if key.endswith("_amount"))
TEXT_KEYS = ("business_plan_reference", "utilization_assessed_by", "utilization_notes")
DATE_KEYS = ("utilization_assessed_on",)
YES_NO = ("YES", "NO")
CENT = Decimal("0.01")
# Spreadsheet day numbers count from 1899-12-30; epoch milliseconds are far larger.
SPREADSHEET_EPOCH = datetime.date(1899, 12, 30)
LAST_SPREADSHEET_DAY = 2958465


class GrantUploadError(Exception):
    """The upload as a whole cannot be processed; no row is applied."""


def register_workflow():
    from workflow.systems.python import PythonWorkflowAdaptor

    PythonWorkflowAdaptor.register_workflow(WORKFLOW_NAME, WORKFLOW_GROUP, process_grant_and_utilization_update)


def process_grant_and_utilization_update(user_uuid, benefit_plan_uuid, upload_uuid):
    from core.models import User
    from individual.models import IndividualDataSource, IndividualDataSourceUpload
    from social_protection.models import BenefitPlan
    from workflow.exceptions import PythonWorkflowHandlerException

    from django.core.exceptions import PermissionDenied
    from social_protection.benefit_plan_access import require_benefit_plan_access

    user = User.objects.get(id=user_uuid)
    benefit_plan = BenefitPlan.objects.get(id=benefit_plan_uuid, is_deleted=False)
    try:
        require_benefit_plan_access(user, benefit_plan.id)
    except PermissionDenied as error:
        raise PythonWorkflowHandlerException("The upload was refused: %s" % error)
    upload = IndividualDataSourceUpload.objects.get(id=upload_uuid)
    rows = list(IndividualDataSource.objects.filter(upload_id=upload_uuid, is_deleted=False).order_by("date_created", "id"))
    try:
        summary = GrantUtilizationUpload(user, benefit_plan).process(rows, upload)
    except GrantUploadError as error:
        # The import service marks the upload FAIL with this message.
        raise PythonWorkflowHandlerException(str(error))

    upload.status = summary.upload_status(IndividualDataSourceUpload.Status)
    upload.error = {"errors": {"error": "Invalid entries", "invalid_rows": summary.invalid}} if summary.invalid else {}
    upload.save(username=user.login_name)
    return summary.as_dict()


class UploadSummary:
    def __init__(self):
        self.applied = 0
        self.tasks = 0
        self.invalid = 0

    def upload_status(self, statuses):
        if self.invalid and not (self.applied or self.tasks):
            return statuses.FAIL
        if self.tasks:
            return statuses.WAITING_FOR_VERIFICATION
        if self.invalid:
            return statuses.PARTIAL_SUCCESS
        return statuses.SUCCESS

    def as_dict(self):
        return {"applied": self.applied, "tasks": self.tasks, "invalid": self.invalid}


class GrantUtilizationUpload:
    def __init__(self, user, benefit_plan):
        self.user = user
        self.benefit_plan = benefit_plan
        self.is_group = benefit_plan.type == "GROUP"

    def process(self, rows, upload=None):
        columns = self._columns(rows)
        self._check_columns(columns)
        self._check_rights(columns)
        if columns & set(GRANT_KEYS):
            # The file's beneficiaries stay locked until its approval task is created, so a
            # submission from the form cannot slip in between.
            with transaction.atomic():
                return self._process(rows, upload, columns, lock=True)
        return self._process(rows, upload, columns, lock=False)

    def _process(self, rows, upload, columns, lock):
        programme = self._programme(columns)
        beneficiaries = self._beneficiaries(rows, lock)
        repeated = self._repeated_ids(rows)
        is_grant_file = bool(columns & set(GRANT_KEYS))
        waiting = open_approval_beneficiary_ids(beneficiaries) if is_grant_file else set()
        paid = set(tranche_one_benefits(self.benefit_plan.id, beneficiaries)) if is_grant_file else set()

        summary, proposals = UploadSummary(), []
        for row in rows:
            errors, patch, beneficiary = self._validate_row(row, programme, beneficiaries, is_grant_file)
            beneficiary_id = str(beneficiary.id) if beneficiary is not None else None
            if beneficiary_id in repeated:
                errors.append({"field_name": ID_COLUMN, "note": "appears in more than one row of the file"})
            if beneficiary_id in waiting:
                errors.append({"field_name": ID_COLUMN, "note": "already has a plan approval waiting for a decision"})
            if not errors and is_grant_file and not patch:
                errors.append({"field_name": None, "note": "has no grant details to submit"})
            if not errors and is_grant_file and beneficiary_id in paid:
                frozen = changed_keys(as_json_ext(beneficiary.json_ext), {**as_json_ext(beneficiary.json_ext), **patch},
                                      FROZEN_ONCE_PAID_KEYS)
                if frozen:
                    errors.append({"field_name": frozen[0], "note": "the first tranche is already being paid, so these "
                                                                    "can no longer change: %s" % ", ".join(frozen)})
            if not errors and is_grant_file:
                current = as_json_ext(beneficiary.json_ext)
                proposals.append({
                    "id": beneficiary_id, "label": beneficiary_label(beneficiary), "incoming_data": patch,
                    "current_data": {key: current.get(key) for key in PLAN_AND_GRANT_KEYS if key in current},
                })
            elif not errors:
                try:
                    created_task = self._apply(beneficiary, patch)
                except ValidationError as error:
                    errors = [{"field_name": None, "note": message} for message in error.messages]
                else:
                    if created_task:
                        summary.tasks += 1
                    else:
                        summary.applied += 1
            if errors:
                summary.invalid += 1
            row.validations = {"validation_errors": errors, "validation_warnings": []}
            row.save(username=self.user.login_name)

        if proposals:
            beneficiary_type = beneficiary_type_of(next(iter(beneficiaries.values())))
            PlanApprovalService(self.user).submit_upload(
                upload, self.benefit_plan, beneficiary_type, proposals, invalid_rows=summary.invalid)
            summary.tasks += 1
        return summary

    # Upload-level checks

    @staticmethod
    def _columns(rows):
        from individual.data_source import DATA_SOURCE_FIELD

        ignored = {DATA_SOURCE_FIELD, "Unnamed: 0"}
        return {key for row in rows for key in (row.json_ext or {}) if key not in ignored}

    @staticmethod
    def _check_columns(columns):
        if ID_COLUMN not in columns:
            raise GrantUploadError("The upload needs an %s column with the beneficiary ID." % ID_COLUMN)
        decisions = sorted(columns & (set(PLAN_AND_GRANT_KEYS) - set(GRANT_KEYS)))
        if decisions:
            raise GrantUploadError(
                "The plan status and approval are recorded when a plan approval is decided, not uploaded. "
                "Remove the columns: %s." % ", ".join(decisions)
            )
        tranche_two = sorted(column for column in columns if column.startswith("tranche_2_"))
        if tranche_two:
            raise GrantUploadError(
                "Tranche two is paid in cash, as its payment plan's share of the grant, so its amounts are not "
                "uploaded. Remove the columns: %s." % ", ".join(tranche_two)
            )
        unknown = sorted(columns - {ID_COLUMN} - set(GRANT_KEYS) - set(UTILIZATION_KEYS))
        if unknown:
            raise GrantUploadError(
                "The upload accepts only %s and the grant and utilization keys. Unknown columns: %s."
                % (ID_COLUMN, ", ".join(unknown))
            )
        if columns & set(GRANT_KEYS) and columns & set(UTILIZATION_KEYS):
            raise GrantUploadError(
                "Upload grant details and utilization assessments in separate files: grant details go "
                "for approval, utilization assessments are saved directly."
            )

    def _check_rights(self, columns):
        problems = []
        if columns & set(GRANT_KEYS) and not self.user.has_perms(
                CalcruleAttendanceGrantConfig.gql_plans_and_grants_perms):
            problems.append("grant columns need the right to manage beneficiary plans and grants")
        if columns & set(UTILIZATION_KEYS) and not self.user.has_perms(
                CalcruleAttendanceGrantConfig.gql_utilization_assessment_perms):
            problems.append("utilization columns need the right to record utilization assessments")
        if problems:
            raise GrantUploadError("The upload was refused: %s." % "; ".join(problems))

    def _programme(self, columns):
        errors = []
        programme = ProgrammeSettings.from_benefit_plan(self.benefit_plan, errors)
        grant_columns = {"grant_types", *AMOUNT_KEYS} - {"approved_grant_amount"}
        if errors and columns & grant_columns:
            raise GrantUploadError("The programme is not set up for grants: %s." % "; ".join(errors))
        return programme

    @staticmethod
    def _repeated_ids(rows):
        seen, repeated = set(), set()
        for row in rows:
            beneficiary_id = _uuid((row.json_ext or {}).get(ID_COLUMN))
            if beneficiary_id is not None:
                (repeated if str(beneficiary_id) in seen else seen).add(str(beneficiary_id))
        return repeated

    def _beneficiaries(self, rows, lock=False):
        from social_protection.models import Beneficiary, GroupBeneficiary

        model = GroupBeneficiary if self.is_group else Beneficiary
        ids = {_uuid((row.json_ext or {}).get(ID_COLUMN)) for row in rows} - {None}
        queryset = model.objects.filter(id__in=ids, benefit_plan=self.benefit_plan, is_deleted=False).order_by("id")
        if lock:
            queryset = queryset.select_for_update()
        return {str(beneficiary.id): beneficiary for beneficiary in queryset}

    # Row validation

    def _validate_row(self, row, programme, beneficiaries, is_grant_file):
        data = row.json_ext or {}
        errors, patch = [], {}

        def error(field, note):
            errors.append({"field_name": field, "note": note})

        beneficiary_id = _uuid(data.get(ID_COLUMN))
        beneficiary = beneficiaries.get(str(beneficiary_id)) if beneficiary_id else None
        if beneficiary is None:
            kind = "household beneficiary" if self.is_group else "beneficiary"
            error(ID_COLUMN, "%r is not a %s of this programme" % (data.get(ID_COLUMN), kind))

        for key, value in data.items():
            if key == ID_COLUMN or key not in GRANT_KEYS + UTILIZATION_KEYS or _blank(value):
                continue
            parsed, note = self._parse(key, value, programme)
            if note:
                error(key, note)
            else:
                patch[key] = parsed

        if beneficiary is not None and not errors:
            merged = {**as_json_ext(beneficiary.json_ext), **patch}
            if is_grant_file:
                self._check_tranches(merged, patch, programme, error)
            else:
                try:
                    check_protected_changes(self.user, beneficiary.json_ext, merged)
                except ValidationError as exc:
                    for message in exc.messages:
                        error(None, message)
        return errors, patch, beneficiary

    def _parse(self, key, value, programme):
        if key == "grant_types":
            return _grant_types(value, programme.allowed_grant_types)
        if key in AMOUNT_KEYS:
            return _amount(value, positive=key == "approved_grant_amount")
        if key == "utilization_confirmed":
            return _choice(value, YES_NO)
        if key == "utilization_percentage":
            number = _number(value)
            if number is None or not 0 <= number <= 100:
                return None, "must be a number from 0 to 100, got %r" % (value,)
            return _json_number(number), None
        if key in DATE_KEYS:
            return _date(value)
        return _text(value)

    @staticmethod
    def _check_tranches(merged, patch, programme, error):
        """Per-tranche amounts must fit the grant types. Whether a tranche adds up to its
        share is checked by the payroll, against the payment plan's percentage."""
        types = merged.get("grant_types")
        if not isinstance(types, list) or not types:
            types = list(programme.allowed_grant_types) if len(programme.allowed_grant_types) == 1 else []
        for grant_type in GRANT_TYPES:
            key = tranche_amount_key(1, grant_type)
            if key in patch and patch[key] and grant_type not in types:
                error(key, "is not zero but %s is not one of the grant types" % grant_type)

    # Saving

    def _apply(self, beneficiary, patch):
        """Merge the row into the beneficiary. Returns True when it became a task."""
        from social_protection.apps import SocialProtectionConfig
        from social_protection.services import BeneficiaryService, GroupBeneficiaryService

        data = {
            "id": beneficiary.id,
            "benefit_plan_id": beneficiary.benefit_plan_id,
            "status": beneficiary.status,
            "json_ext": {**as_json_ext(beneficiary.json_ext), **patch},
        }
        if self.is_group:
            data["group_id"] = beneficiary.group_id
            service, as_task = GroupBeneficiaryService(self.user), False
        else:
            data["individual_id"] = beneficiary.individual_id
            service = BeneficiaryService(self.user)
            as_task = bool(SocialProtectionConfig.enable_maker_checker_for_beneficiary_update)

        with transaction.atomic():
            result = service.create_update_task(data) if as_task else service.update(data)
            if not result or not result.get("success"):
                raise ValidationError((result or {}).get("detail") or (result or {}).get("message")
                                      or "The beneficiary could not be saved")
        return as_task


def _blank(value):
    return value is None or (isinstance(value, float) and math.isnan(value)) or (
        isinstance(value, str) and not value.strip())


def _uuid(value):
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _json_number(number):
    return int(number) if number == number.to_integral_value() else float(number)


def _amount(value, positive):
    number = _number(value)
    if number is None:
        return None, "must be a number, got %r" % (value,)
    if positive and number <= 0:
        return None, "must be positive, got %r" % (value,)
    if number < 0:
        return None, "must not be negative, got %r" % (value,)
    if number != number.quantize(CENT):
        return None, "must have at most two decimals, got %r" % (value,)
    return _json_number(number), None


def _choice(value, choices):
    text = str(value).strip().upper()
    if text not in choices:
        return None, "must be one of %s, got %r" % (", ".join(choices), value)
    return text, None


def _date(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 10 ** 11:
            # A spreadsheet date the upload stored as epoch milliseconds.
            return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc).date().isoformat(), None
        if 1 <= value <= LAST_SPREADSHEET_DAY and value == int(value):
            # A spreadsheet day number, such as 45980.
            return (SPREADSHEET_EPOCH + datetime.timedelta(days=int(value))).isoformat(), None
    try:
        return datetime.date.fromisoformat(str(value).strip()[:10]).isoformat(), None
    except ValueError:
        return None, "must be a date as YYYY-MM-DD or a spreadsheet date, got %r" % (value,)


def _text(value):
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return str(value).strip(), None


def _grant_types(value, allowed):
    types = [item.strip().upper() for item in str(value).split(";") if item.strip()]
    if not types:
        return None, "needs at least one grant type, such as CASH;EQUIPMENT"
    if len(set(types)) != len(types):
        return None, "repeats a grant type: %r" % (value,)
    not_allowed = [item for item in types if item not in allowed]
    if not_allowed:
        return None, "has types the programme does not allow: %s" % ", ".join(not_allowed)
    return [grant_type for grant_type in GRANT_TYPES if grant_type in types], None
