import importlib
import inspect

from django.apps import AppConfig

MODULE_NAME = "calcrule_attendance_grant"

DEFAULT_CFG = {
    # Right to submit a beneficiary's grant types, amounts and business plan
    # reference for approval.
    "gql_plans_and_grants_perms": ["210001"],
    # Right to set or change a beneficiary's utilization assessment.
    "gql_utilization_assessment_perms": ["210002"],
    # Right to approve or reject a submitted business plan (plan status, approval
    # date, approver and rejection reason).
    "gql_plan_approval_perms": ["210003"],
}


def read_all_calculation_rules():
    """Register this module's calculation rules with the calculation module."""
    from calculation.apps import CALCULATION_RULES

    module = importlib.import_module("calcrule_attendance_grant.calculation_rule")
    for _name, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ == module.__name__ and cls not in CALCULATION_RULES:
            CALCULATION_RULES.append(cls)
            cls.ready()


class CalcruleAttendanceGrantConfig(AppConfig):
    name = MODULE_NAME
    verbose_name = "Calculation rule: attendance grant"

    gql_plans_and_grants_perms = DEFAULT_CFG["gql_plans_and_grants_perms"]
    gql_utilization_assessment_perms = DEFAULT_CFG["gql_utilization_assessment_perms"]
    gql_plan_approval_perms = DEFAULT_CFG["gql_plan_approval_perms"]

    def ready(self):
        from core.models import ModuleConfiguration

        from calcrule_attendance_grant.upload import register_workflow

        self._load_config(ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG))
        read_all_calculation_rules()
        register_workflow()

    @classmethod
    def _load_config(cls, cfg):
        for field in DEFAULT_CFG:
            if field in cfg:
                setattr(cls, field, cfg[field])
