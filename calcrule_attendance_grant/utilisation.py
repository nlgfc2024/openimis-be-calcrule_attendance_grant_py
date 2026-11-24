"""Recording a beneficiary's utilization assessment from the beneficiary page.

Only the assessment keys are sent; they are merged into the beneficiary's current
``json_ext`` under a row lock, so a change made elsewhere since the page loaded (a
KYC approval, say) is kept, and the status is the record's own. A new or changed
result or note is dated today and signed by the user. The save goes through the
beneficiary services, as an update task when the beneficiary mutations create tasks.
"""
import datetime
import math

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from calcrule_attendance_grant.plan_approval import approver_name
from calcrule_attendance_grant.protected_fields import as_json_ext, check_protected_changes

RESULT_KEYS = ("utilization_percentage", "utilization_confirmed")
INPUT_KEYS = RESULT_KEYS + ("utilization_notes",)
YES_NO = ("YES", "NO")


def record_utilisation(user, model, beneficiary_id, values):
    """Merge ``values`` into the beneficiary's assessment; returns True when it became a task."""
    from social_protection.apps import SocialProtectionConfig
    from social_protection.models import GroupBeneficiary
    from social_protection.services import BeneficiaryService, GroupBeneficiaryService

    values = _validated(values)
    is_group = model is GroupBeneficiary
    with transaction.atomic():
        beneficiary = model.objects.select_for_update().filter(id=beneficiary_id, is_deleted=False).first()
        if beneficiary is None:
            raise ValidationError(_("The beneficiary was not found."))
        current = as_json_ext(beneficiary.json_ext)
        json_ext = _assessed(current, values, user)
        check_protected_changes(user, current, json_ext)
        data = {"id": beneficiary.id, "benefit_plan_id": beneficiary.benefit_plan_id,
                "status": beneficiary.status, "json_ext": json_ext}
        if is_group:
            data["group_id"] = beneficiary.group_id
            service, as_task = GroupBeneficiaryService(user), SocialProtectionConfig.gql_check_group_beneficiary_crud
        else:
            data["individual_id"] = beneficiary.individual_id
            service, as_task = BeneficiaryService(user), SocialProtectionConfig.gql_check_beneficiary_crud
        result = service.create_update_task(data) if as_task else service.update(data)
        if not result or not result.get("success"):
            raise ValidationError((result or {}).get("detail") or (result or {}).get("message")
                                  or _("The assessment could not be saved."))
    return bool(as_task)


def _validated(values):
    if not isinstance(values, dict) or not values:
        raise ValidationError(_("Send at least one assessment value."))
    unknown = sorted(set(values) - set(INPUT_KEYS))
    if unknown:
        raise ValidationError(_("Only %s can be sent, not: %s.") % (", ".join(INPUT_KEYS), ", ".join(unknown)))
    cleaned = {}
    for key, value in values.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            cleaned[key] = None
        elif key == "utilization_percentage":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not 0 <= value <= 100:
                raise ValidationError(_("utilization_percentage must be a number from 0 to 100."))
            cleaned[key] = value
        elif key == "utilization_confirmed":
            if str(value).strip().upper() not in YES_NO:
                raise ValidationError(_("utilization_confirmed must be YES or NO."))
            cleaned[key] = str(value).strip().upper()
        else:
            text = str(value).strip()
            if len(text) > 1000:
                raise ValidationError(_("utilization_notes must be at most 1000 characters."))
            cleaned[key] = text
    return cleaned


def _assessed(current, values, user):
    json_ext = dict(current)
    for key, value in values.items():
        if value is None:
            json_ext.pop(key, None)
        else:
            json_ext[key] = value
    if not any(json_ext.get(key) is not None for key in RESULT_KEYS):
        json_ext.pop("utilization_assessed_on", None)
        json_ext.pop("utilization_assessed_by", None)
    elif any(json_ext.get(key) != current.get(key) for key in INPUT_KEYS):
        json_ext["utilization_assessed_on"] = datetime.date.today().isoformat()
        json_ext["utilization_assessed_by"] = approver_name(user)
    return json_ext
