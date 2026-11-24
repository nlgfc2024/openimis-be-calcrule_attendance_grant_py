"""Hooks that protect the grant, plan and utilization keys (see protected_fields)
and run the plan approval task (see plan_approval).

A change can reach a beneficiary in three ways, and each is checked:

- directly, through the beneficiary or household beneficiary service;
- as a generic maker-checker task: the maker is checked when the task is created,
  and each approver when they approve it, so a change that is not allowed fails with
  a clear error instead of being dropped when the task runs;
- when the approved task runs, through the service again, as the approver.

Grant and plan keys only change through a plan approval task. Its decisions are
checked when they are given, and applied when the task completes or fails.
"""
import logging

from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

from calcrule_attendance_grant.plan_approval import BUSINESS_EVENTS, apply_decision, check_decision
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

logger = logging.getLogger(__name__)


def bind_service_signals():
    for signal_name in SERVICE_SIGNALS:
        bind_service_signal(signal_name, protect_on_save, bind_type=ServiceSignalBindType.BEFORE)
    bind_service_signal("task_service.create", protect_on_task_create, bind_type=ServiceSignalBindType.BEFORE)
    bind_service_signal("task_service.resolve_task", protect_on_task_approval,
                        bind_type=ServiceSignalBindType.BEFORE)
    bind_service_signal("task_service.complete_task", apply_plan_decision,
                        bind_type=ServiceSignalBindType.AFTER)


def protect_on_save(**kwargs):
    service = kwargs["cls_"]
    obj_data = _service_data(kwargs)
    if "json_ext" not in obj_data:
        return
    beneficiary = _current(service.OBJECT_TYPE, obj_data.get("id"))
    check_protected_changes(service.user, _json_ext(beneficiary), obj_data["json_ext"], beneficiary)


def protect_on_task_create(**kwargs):
    task_data = _service_data(kwargs)
    _check_task(kwargs["cls_"].user, task_data.get("business_event"), task_data.get("data"),
                task_data.get("entity_id"))


def protect_on_task_approval(**kwargs):
    from tasks_management.apps import TasksManagementConfig
    from tasks_management.models import Task

    obj_data = _service_data(kwargs)
    statuses = obj_data.get("business_status")
    if not isinstance(statuses, dict):
        return
    task = Task.objects.select_related("task_group").filter(id=obj_data.get("id")).first()
    if task is None:
        return
    user = kwargs["cls_"].user
    if task.business_event in BUSINESS_EVENTS:
        check_decision(user, task, statuses, obj_data.get("additional_data"))
    elif TasksManagementConfig.task_user_approved in statuses.values():
        _check_task(user, task.business_event, task.data, task.entity_id)


def apply_plan_decision(**kwargs):
    """Save a completed or failed plan approval task's decision on its beneficiary."""
    from core.models import User
    from tasks_management.models import Task

    result = kwargs.get("result") or {}
    data = result.get("data") or {}
    task_data = data.get("task") or {}
    if not result.get("success") or task_data.get("business_event") not in BUSINESS_EVENTS:
        return
    try:
        task = Task.objects.get(id=task_data["id"])
        apply_decision(task, User.objects.get(id=data["user"]["id"]))
    except Exception as exc:
        logger.error("Could not apply the plan approval decision of task %s", task_data.get("id"), exc_info=exc)
        raise


def _check_task(user, business_event, task_data, entity_id):
    service_name, _, operation = str(business_event or "").partition(".")
    if service_name not in TASK_SERVICES or operation not in TASK_OPERATIONS:
        return
    incoming = (task_data or {}).get("incoming_data") or {}
    if "json_ext" not in incoming:
        return
    beneficiary = None
    if operation == "update":
        beneficiary = _current(_beneficiary_model(service_name), entity_id or incoming.get("id"))
    check_protected_changes(user, _json_ext(beneficiary), incoming["json_ext"], beneficiary)


def _service_data(kwargs):
    args, call_kwargs = kwargs.get("data") or ((), {})
    obj_data = args[0] if args else call_kwargs.get("obj_data")
    return obj_data if isinstance(obj_data, dict) else {}


def _beneficiary_model(service_name):
    from social_protection.models import Beneficiary, GroupBeneficiary

    return GroupBeneficiary if service_name == "GroupBeneficiaryService" else Beneficiary


def _current(model, obj_id):
    """The saved beneficiary being changed, or None for a new one."""
    if not obj_id:
        return None
    return model.objects.filter(id=obj_id).only("id", "benefit_plan_id", "json_ext").first()


def _json_ext(beneficiary):
    return (beneficiary.json_ext if beneficiary is not None else None) or {}
