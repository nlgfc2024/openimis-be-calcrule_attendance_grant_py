import logging

from core.models import User
from invoice.models import Bill
from invoice.services import BillService
from payroll.models import BenefitConsumption, BenefitConsumptionStatus
from payroll.services import BenefitConsumptionService, PayrollService
from social_protection.models import Beneficiary, BeneficiaryStatus

from calcrule_attendance_grant.approval import plan_approval_shortfall
from calcrule_attendance_grant.attendance import evaluate_attendance
from calcrule_attendance_grant.converters import (
    BeneficiaryToBillConverter,
    BeneficiaryToBillItemConverter,
    GrantBenefitConverter,
)
from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError
from calcrule_attendance_grant.grants import GRANT_COMPONENT_IN_KIND, GRANT_TYPES, IN_KIND_TYPES, resolve_grant
from calcrule_attendance_grant.parameters import GRANT_SOURCE_PAYMENT_PLAN, GrantParameters

logger = logging.getLogger(__name__)


class AttendanceGrantError(Exception):
    """A Bill or BenefitConsumption could not be saved; the payroll run stops."""


class AttendanceGrantStrategy:

    @classmethod
    def calculate(cls, calculation, payment_plan, **kwargs):
        params = GrantParameters.from_payment_plan(payment_plan)
        payroll = kwargs.get("payroll")
        user_id, _start_date, end_date, payment_cycle = \
            calculation.get_payment_cycle_parameters(**kwargs)
        user = User.objects.filter(id=user_id).first()

        beneficiaries = list(cls._beneficiaries(payment_plan, kwargs.get("beneficiaries_queryset")))
        attendance = evaluate_attendance(
            [beneficiary.id for beneficiary in beneficiaries], payment_plan.benefit_plan,
            params.minimum_attendance_percentage,
            params.programme.counts_completed_trainings_only,
        )
        individuals_paid = cls._individuals_with_benefits(payment_plan)

        paid = 0
        for beneficiary in beneficiaries:
            if beneficiary.individual_id in individuals_paid:
                cls._log_excluded(
                    payment_plan, beneficiary,
                    "already has a benefit under this payment plan that is not rejected",
                )
                continue
            participant = attendance[str(beneficiary.id)]
            if not participant.eligible:
                cls._log_excluded(payment_plan, beneficiary, participant.reason)
                continue
            shortfall = plan_approval_shortfall(beneficiary.json_ext)
            if shortfall:
                cls._log_excluded(payment_plan, beneficiary, shortfall)
                continue
            grant = resolve_grant(
                beneficiary.json_ext,
                allowed_grant_types=params.programme.allowed_grant_types,
                grant_amount_source_is_plan=params.grant_amount_source == GRANT_SOURCE_PAYMENT_PLAN,
                plan_grant_amount=params.grant_amount,
                tranche_percentage=params.tranche_percentage,
            )
            if grant.reason:
                cls._log_excluded(payment_plan, beneficiary, grant.reason, level=logging.WARNING)
                continue
            amount = grant.part(params.grant_component)
            if not amount:
                cls._log_excluded(
                    payment_plan, beneficiary,
                    "its tranche has no %s part" % params.grant_component.replace("_", "-"),
                )
                continue

            calculation.run_convert(
                payment_plan,
                beneficiary=beneficiary,
                amount=amount,
                benefit_type="+".join(grant.part_types(params.grant_component)),
                json_ext=cls._benefit_json_ext(params, grant),
                user=user,
                end_date=end_date,
                payment_cycle=payment_cycle,
                payroll=payroll,
            )
            individuals_paid.add(beneficiary.individual_id)
            paid += 1

        logger.info(
            "Attendance grant, payment plan %s: %s of %s participant(s) paid.",
            payment_plan.code, paid, len(beneficiaries),
        )
        return "Calculation and transformation into bills completed successfully."

    @staticmethod
    def _benefit_json_ext(params, grant):
        """What the BenefitConsumption records about its tranche.

        ``attendance_grant`` holds the whole tranche, so tranche two and reports can
        read it; reconciliation replaces only ``extra_info``. ``extra_info`` holds what
        the payroll's recipient needs, as columns of the reconciliation file: the
        in-kind amounts for the PIU team.
        """
        amounts = {
            "%s_amount" % grant_type.lower(): float(grant.amounts.get(grant_type, 0))
            for grant_type in GRANT_TYPES
        }
        extra_info = {}
        if params.grant_component == GRANT_COMPONENT_IN_KIND:
            extra_info = {
                "%s_amount" % grant_type.lower(): float(grant.amounts.get(grant_type, 0))
                for grant_type in IN_KIND_TYPES
                if grant_type in params.programme.allowed_grant_types
            }
        return {
            "attendance_grant": {
                "tranche": 1,
                "grant_component": params.grant_component,
                "grant_types": list(grant.grant_types),
                "grant_amount": float(grant.grant_amount),
                "tranche_percentage": float(grant.percentage),
                "tranche_share": float(grant.share),
                **amounts,
            },
            "extra_info": extra_info,
        }

    @staticmethod
    def _beneficiaries(payment_plan, beneficiaries_queryset):
        """ACTIVE beneficiaries of the plan, narrowed to the payroll's selection."""
        beneficiaries = Beneficiary.objects.filter(
            benefit_plan=payment_plan.benefit_plan,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        )
        if beneficiaries_queryset is not None:
            if getattr(beneficiaries_queryset, "model", None) is not Beneficiary:
                raise AttendanceGrantConfigurationError(
                    "The attendance-grant rule expects a Beneficiary selection, got %s."
                    % getattr(beneficiaries_queryset, "model", type(beneficiaries_queryset)).__name__
                )
            beneficiaries = beneficiaries.filter(id__in=beneficiaries_queryset.values("id"))
        return beneficiaries.select_related("individual").order_by("date_created", "id")

    @staticmethod
    def _individuals_with_benefits(payment_plan):
        """Individuals with a benefit under this plan that was not rejected."""
        return set(
            BenefitConsumption.objects.filter(
                is_deleted=False,
                payrollbenefitconsumption__is_deleted=False,
                payrollbenefitconsumption__payroll__is_deleted=False,
                payrollbenefitconsumption__payroll__payment_plan=payment_plan,
            ).exclude(
                status=BenefitConsumptionStatus.REJECTED,
            ).values_list("individual_id", flat=True)
        )

    @staticmethod
    def _log_excluded(payment_plan, beneficiary, reason, level=logging.INFO):
        logger.log(
            level,
            "Attendance grant, payment plan %s: beneficiary %s (individual %s) not paid: %s.",
            payment_plan.code, beneficiary.id, beneficiary.individual_id, reason,
        )

    @classmethod
    def convert(cls, payment_plan, **kwargs):
        beneficiary = kwargs["beneficiary"]
        amount = kwargs["amount"]
        payment_cycle = kwargs.get("payment_cycle")
        user = kwargs.get("user")
        payroll = kwargs.get("payroll")
        convert_results = {
            "bill_data": BeneficiaryToBillConverter.to_bill_obj(
                payment_plan, beneficiary, amount, kwargs.get("end_date"), payment_cycle
            ),
            "bill_data_line": [
                BeneficiaryToBillItemConverter.to_bill_item_obj(payment_plan, beneficiary, amount)
            ],
            "type_conversion": "beneficiary - bill",
            "user": user,
        }
        benefit_data = GrantBenefitConverter.to_grant_benefit_obj(
            beneficiary, amount, payment_plan, payment_cycle,
            kwargs["benefit_type"], kwargs["json_ext"],
        )
        cls._save(convert_results, benefit_data, amount, payroll, user, beneficiary)

    @staticmethod
    def _save(convert_results, benefit_data, amount, payroll, user, beneficiary):
        bill_result = BillService.bill_create(convert_results=convert_results)
        if not bill_result or not bill_result.get("success"):
            raise AttendanceGrantError(
                "Could not create the bill for beneficiary %s: %s"
                % (beneficiary.id, (bill_result or {}).get("detail"))
            )
        bill = Bill.objects.get(id=bill_result["data"]["id"])
        if bill.amount_total != amount:
            # A bill line that failed to save leaves the bill at a different total.
            raise AttendanceGrantError(
                "The bill for beneficiary %s totals %s instead of %s."
                % (beneficiary.id, bill.amount_total, amount)
            )
        benefit_service = BenefitConsumptionService(user)
        benefit_result = benefit_service.create(benefit_data)
        if not benefit_result.get("success"):
            raise AttendanceGrantError(
                "Could not create the benefit for beneficiary %s: %s"
                % (beneficiary.id, benefit_result.get("detail"))
            )
        benefit_id = benefit_result["data"]["id"]
        benefit_service.create_or_update_benefit_attachment(Bill.objects.filter(id=bill.id), benefit_id)
        if payroll is not None:
            PayrollService(user=user).attach_benefit_to_payroll(payroll.id, benefit_id)
