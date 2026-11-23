from contribution_plan.models import PaymentPlan
from core import datetime
from core.abs_calculation_rule import AbsStrategy

from calcrule_attendance_grant.config import (
    CLASS_RULE_PARAM_VALIDATION,
    DESCRIPTION_ATTENDANCE_GRANT_CALCULATION,
    FROM_TO,
)
from calcrule_attendance_grant.strategy import AttendanceGrantStrategy


class AttendanceGrantCalculationRule(AbsStrategy):
    version = 1
    uuid = "00697d55-e501-48f1-ae25-9fd6594543bd"
    calculation_rule_name = "Calculation rule: attendance grant"
    description = DESCRIPTION_ATTENDANCE_GRANT_CALCULATION
    impacted_class_parameter = CLASS_RULE_PARAM_VALIDATION
    date_valid_from = datetime.datetime(2000, 1, 1)
    date_valid_to = None
    status = "active"
    from_to = FROM_TO
    type = "social_protection"
    sub_type = "benefit_plan"
    supports_advanced_criteria = False
    CLASS_NAME_CHECK = ['PaymentPlan']

    @classmethod
    def run_calculation_rules(cls, sender, instance, user, context, **kwargs):
        if isinstance(instance, PaymentPlan):
            return cls.calculate_if_active_for_object(instance, **kwargs)
        return False

    @classmethod
    def calculate_if_active_for_object(cls, payment_plan, **kwargs):
        if cls.active_for_object(payment_plan):
            return cls.calculate(payment_plan, **kwargs)

    @classmethod
    def active_for_object(cls, payment_plan, **kwargs):
        return cls.check_calculation(payment_plan)

    @classmethod
    def check_calculation(cls, payment_plan, **kwargs):
        return cls.uuid == str(payment_plan.calculation)

    @classmethod
    def get_linked_class(cls, sender, class_name, **kwargs):
        list_class = super().get_linked_class(sender, class_name, **kwargs)
        if class_name == "PaymentPlan":
            list_class.append("Calculation")
        if class_name == "BenefitPlan":
            list_class.append("PaymentPlan")
        if class_name == "Beneficiary":
            list_class.append("BenefitPlan")
        return list_class

    @classmethod
    def get_parameters(cls, sender, class_name, instance, **kwargs):
        rule_details = cls.get_rule_details(sender=sender, class_name=class_name)
        if rule_details:
            if instance.__class__.__name__ in cls.CLASS_NAME_CHECK:
                if cls.check_calculation(payment_plan=instance):
                    return rule_details.get("parameters", [])
            elif instance.__class__.__name__ == 'ABCMeta' and cls.uuid == str(instance.uuid):
                return rule_details.get("parameters", [])

    @classmethod
    def calculate(cls, payment_plan, **kwargs):
        return AttendanceGrantStrategy.calculate(cls, payment_plan, **kwargs)

    @classmethod
    def run_convert(cls, payment_plan, **kwargs):
        return cls.convert(payment_plan=payment_plan, **kwargs)

    @classmethod
    def convert(cls, payment_plan, **kwargs):
        AttendanceGrantStrategy.convert(payment_plan, **kwargs)

    @classmethod
    def get_payment_cycle_parameters(cls, **kwargs):
        return (
            kwargs.get('user_id'),
            kwargs.get('start_date'),
            kwargs.get('end_date'),
            kwargs.get('payment_cycle'),
        )
