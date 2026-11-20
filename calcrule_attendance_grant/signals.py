"""Before-hooks that keep the protected grant, plan and utilization keys to the
roles holding the matching right (see protected_fields).

A change can reach a beneficiary in three ways, and each is checked:

- directly, through the beneficiary or household beneficiary service;
- as a maker-checker task: the maker is checked when the task is created, and
  each approver when they approve it, so an approval the approver may not give
  fails with a clear error instead of being dropped when the task runs;
- when the approved task runs, through the service again, as the approver.
"""
from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

from calcrule_attendance_grant.protected_fields import check_protected_changes

SERVICE_SIGNALS = (
    "beneficiary_service.create",
    "beneficiary_service.update",
    "group_beneficiary_service.create",
    "group_beneficiary_service.update",
)
# Services whose create and update tasks can carry protected keys.
TASK_SERVICES = ("BeneficiaryService", "GroupBeneficiaryService")
TASK_OPERATIONS = ("create", "update")


def bind_service_signals():
    for signal_name in SERVICE_SIGNALS:
        bind_service_signal(signal_name, protect_on_save, bind_type=ServiceSignalBindType.BEFORE)
    bind_service_signal("task_service.create", protect_on_task_create, bind_type=ServiceSignalBindType.BEFORE)
    bind_service_signal("task_service.resolve_task", protect_on_task_approval,
                        bind_type=ServiceSignalBindType.BEFORE)


def protect_on_save(**kwargs):
    service = kwargs["cls_"]
    obj_data = _service_data(kwargs)
    if "json_ext" not in obj_data:
        return
    current = _current_json_ext(service.OBJECT_TYPE, obj_data.get("id"))
    check_protected_changes(service.user, current, obj_data["json_ext"])


def protect_on_task_create(**kwargs):
    task_data = _service_data(kwargs)
    _check_task(kwargs["cls_"].user, task_data.get("business_event"), task_data.get("data"),
                task_data.get("entity_id"))


def protect_on_task_approval(**kwargs):
    from tasks_management.apps import TasksManagementConfig
    from tasks_management.models import Task

    obj_data = _service_data(kwargs)
    statuses = obj_data.get("business_status")
    if not isinstance(statuses, dict) or TasksManagementConfig.task_user_approved not in statuses.values():
        return
    task = Task.objects.filter(id=obj_data.get("id")).first()
    if task is not None:
        _check_task(kwargs["cls_"].user, task.business_event, task.data, task.entity_id)


def _check_task(user, business_event, task_data, entity_id):
    service_name, _, operation = str(business_event or "").partition(".")
    if service_name not in TASK_SERVICES or operation not in TASK_OPERATIONS:
        return
    incoming = (task_data or {}).get("incoming_data") or {}
    if "json_ext" not in incoming:
        return
    current = {}
    if operation == "update":
        current = _current_json_ext(_beneficiary_model(service_name), entity_id or incoming.get("id"))
    check_protected_changes(user, current, incoming["json_ext"])


def _service_data(kwargs):
    args, call_kwargs = kwargs.get("data") or ((), {})
    obj_data = args[0] if args else call_kwargs.get("obj_data")
    return obj_data if isinstance(obj_data, dict) else {}


def _beneficiary_model(service_name):
    from social_protection.models import Beneficiary, GroupBeneficiary

    return GroupBeneficiary if service_name == "GroupBeneficiaryService" else Beneficiary


def _current_json_ext(model, obj_id):
    if not obj_id:
        return {}
    return model.objects.filter(id=obj_id).values_list("json_ext", flat=True).first() or {}
