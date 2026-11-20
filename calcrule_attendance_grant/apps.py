import importlib
import inspect

from django.apps import AppConfig

MODULE_NAME = "calcrule_attendance_grant"


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

    def ready(self):
        read_all_calculation_rules()
