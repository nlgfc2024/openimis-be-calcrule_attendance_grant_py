"""Who the rule evaluates and who is paid.

UPG and RMEP are GROUP programmes. Their beneficiary is the household
(GroupBeneficiary), and its participant is the primary worker: the one member whose
``GroupIndividual.json_ext`` marks them as such. PayrollService passes the
households' project enrollments, so a household with several enrollments is taken
once. UMEP is an INDIVIDUAL programme: its beneficiary is a Beneficiary and the
participant is that Individual.
"""
from dataclasses import dataclass

from individual.models import GroupIndividual
from social_protection.models import Beneficiary, BeneficiaryStatus, GroupBeneficiary

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig
from calcrule_attendance_grant.attendance import GROUP_BENEFIT_PLAN
from calcrule_attendance_grant.exceptions import AttendanceGrantConfigurationError


@dataclass(frozen=True)
class Payee:
    """A beneficiary of the plan and the participant who would be paid."""
    beneficiary: object
    # The Beneficiary itself, or the primary worker's GroupIndividual. Both expose
    # ``.individual``, which the benefit converter reads.
    participant: object = None
    # Why nobody in the household can be paid.
    reason: str = None

    @property
    def is_household(self):
        return isinstance(self.beneficiary, GroupBeneficiary)

    @property
    def individual_id(self):
        return self.participant.individual_id if self.participant is not None else None

    @property
    def label(self):
        if self.is_household:
            return "household beneficiary %s (primary worker %s)" % (self.beneficiary.id, self.individual_id)
        return "beneficiary %s (individual %s)" % (self.beneficiary.id, self.individual_id)


def select_payees(payment_plan, beneficiaries_queryset=None, enrollments_queryset=None):
    """ACTIVE beneficiaries of the plan, narrowed to the payroll's selection, as Payees."""
    benefit_plan = payment_plan.benefit_plan
    if benefit_plan.type == GROUP_BENEFIT_PLAN:
        households = _select(GroupBeneficiary, benefit_plan, beneficiaries_queryset, enrollments_queryset,
                             "group_beneficiary_id").select_related("group")
        return _household_payees(households)
    beneficiaries = _select(Beneficiary, benefit_plan, beneficiaries_queryset, enrollments_queryset,
                            "beneficiary_id").select_related("individual")
    return [Payee(beneficiary=beneficiary, participant=beneficiary) for beneficiary in beneficiaries]


def _select(model, benefit_plan, beneficiaries_queryset, enrollments_queryset, enrollment_field):
    selected = model.objects.filter(
        benefit_plan=benefit_plan, status=BeneficiaryStatus.ACTIVE, is_deleted=False,
    )
    if beneficiaries_queryset is not None:
        _check_model(beneficiaries_queryset, model)
        selected = selected.filter(id__in=beneficiaries_queryset.values("id"))
    if enrollments_queryset is not None:
        enrollment_model = getattr(enrollments_queryset, "model", None)
        if enrollment_model is None or enrollment_field not in {
                field.attname for field in enrollment_model._meta.concrete_fields}:
            _raise_wrong_model(enrollments_queryset, "%s enrollment" % model.__name__)
        selected = selected.filter(id__in=enrollments_queryset.values(enrollment_field))
    return selected.order_by("date_created", "id")


def _household_payees(households):
    key = "json_ext__%s" % CalcruleAttendanceGrantConfig.primary_worker_json_ext_key
    workers = {}
    for member in GroupIndividual.objects.filter(
            group_id__in=[household.group_id for household in households],
            is_deleted=False, individual__is_deleted=False, **{key: True},
    ).select_related("individual").order_by("date_created", "id"):
        workers.setdefault(member.group_id, []).append(member)

    payees = []
    for household in households:
        members = workers.get(household.group_id, [])
        if len(members) == 1:
            payees.append(Payee(beneficiary=household, participant=members[0]))
        else:
            payees.append(Payee(beneficiary=household, reason=(
                "the household has no primary worker" if not members
                else "the household has %s primary workers" % len(members)
            )))
    return payees


def _check_model(queryset, model):
    if getattr(queryset, "model", None) is not model:
        _raise_wrong_model(queryset, model.__name__)


def _raise_wrong_model(queryset, expected):
    raise AttendanceGrantConfigurationError(
        "The attendance-grant rule expects a %s selection, got %s."
        % (expected, getattr(queryset, "model", type(queryset)).__name__)
    )
