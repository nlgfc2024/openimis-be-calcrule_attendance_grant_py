import importlib
import inspect

from django.apps import AppConfig

MODULE_NAME = "calcrule_attendance_grant"

DEFAULT_CFG = {
    # GroupIndividual.json_ext key that marks a household's primary worker, the
    # participant of a GROUP programme. Enrolment reads the same key.
    "primary_worker_json_ext_key": "primary_worker",
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

    primary_worker_json_ext_key = DEFAULT_CFG["primary_worker_json_ext_key"]

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self._load_config(cfg)
        read_all_calculation_rules()

    @classmethod
    def _load_config(cls, cfg):
        for field in DEFAULT_CFG:
            if field in cfg:
                setattr(cls, field, cfg[field])
