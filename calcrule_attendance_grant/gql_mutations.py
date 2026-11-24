import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig
from calcrule_attendance_grant.plan_approval import PlanApprovalService
from calcrule_attendance_grant.protected_fields import as_json_ext


class SubmitBeneficiaryPlanInput(OpenIMISMutation.Input):
    # The Beneficiary (UMEP) or, with is_group, the GroupBeneficiary (UPG, RMEP).
    id = graphene.UUID(required=True)
    is_group = graphene.Boolean(required=False, default_value=False)
    # The grant keys to approve: grant types, amounts and business plan reference.
    json_ext = graphene.JSONString(required=True)


class SubmitBeneficiaryPlanMutation(BaseMutation):
    """Submit a beneficiary's grant and business plan details for approval."""
    _mutation_class = "SubmitBeneficiaryPlanMutation"
    _mutation_module = "calcrule_attendance_grant"

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                CalcruleAttendanceGrantConfig.gql_plans_and_grants_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        from social_protection.benefit_plan_access import require_benefit_plan_access
        from social_protection.models import Beneficiary, GroupBeneficiary

        model = GroupBeneficiary if data.get("is_group") else Beneficiary
        beneficiary = model.objects.filter(id=data["id"], is_deleted=False).select_related("benefit_plan").first()
        if beneficiary is None:
            raise ValidationError(_("The beneficiary was not found."))
        require_benefit_plan_access(user, beneficiary.benefit_plan_id)
        PlanApprovalService(user).submit(beneficiary, as_json_ext(data.get("json_ext")))
        return None

    class Input(SubmitBeneficiaryPlanInput):
        pass


class RecordUtilisationAssessmentInput(OpenIMISMutation.Input):
    # The Beneficiary (UMEP) or, with is_group, the GroupBeneficiary (UPG, RMEP).
    id = graphene.UUID(required=True)
    is_group = graphene.Boolean(required=False, default_value=False)
    # Only the assessment: utilization_percentage or utilization_confirmed, and utilization_notes.
    json_ext = graphene.JSONString(required=True)


class RecordUtilisationAssessmentMutation(BaseMutation):
    """Record a beneficiary's utilization assessment, merged into its current json_ext."""
    _mutation_class = "RecordUtilisationAssessmentMutation"
    _mutation_module = "calcrule_attendance_grant"

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                CalcruleAttendanceGrantConfig.gql_utilization_assessment_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        from social_protection.benefit_plan_access import require_benefit_plan_access
        from social_protection.models import Beneficiary, GroupBeneficiary

        from calcrule_attendance_grant.utilisation import record_utilisation

        model = GroupBeneficiary if data.get("is_group") else Beneficiary
        beneficiary = model.objects.filter(id=data["id"], is_deleted=False).first()
        if beneficiary is None:
            raise ValidationError(_("The beneficiary was not found."))
        require_benefit_plan_access(user, beneficiary.benefit_plan_id)
        record_utilisation(user, model, beneficiary.id, as_json_ext(data.get("json_ext")))
        return None

    class Input(RecordUtilisationAssessmentInput):
        pass
