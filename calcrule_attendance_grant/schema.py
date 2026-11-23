import graphene
from django.core.exceptions import PermissionDenied

from calcrule_attendance_grant.apps import CalcruleAttendanceGrantConfig
from calcrule_attendance_grant.gql_mutations import SubmitBeneficiaryPlanMutation
from calcrule_attendance_grant.gql_queries import PendingPlanApprovalGQLType

VIEW_PERMS = ("gql_plans_and_grants_perms", "gql_plan_approval_perms", "gql_utilization_assessment_perms")


class Query(graphene.ObjectType):
    pending_beneficiary_plan_approval = graphene.Field(
        PendingPlanApprovalGQLType,
        beneficiary_id=graphene.UUID(required=True),
        is_group=graphene.Boolean(default_value=False),
    )

    def resolve_pending_beneficiary_plan_approval(self, info, beneficiary_id, is_group=False):
        from social_protection.benefit_plan_access import require_benefit_plan_access
        from social_protection.models import Beneficiary, GroupBeneficiary

        from calcrule_attendance_grant.plan_approval import UPLOAD_BUSINESS_EVENT, approver_name, pending_approval

        user = info.context.user
        if not any(user.has_perms(getattr(CalcruleAttendanceGrantConfig, perms)) for perms in VIEW_PERMS):
            raise PermissionDenied("Unauthorized")
        model = GroupBeneficiary if is_group else Beneficiary
        beneficiary = model.objects.filter(id=beneficiary_id, is_deleted=False).first()
        if beneficiary is None:
            return None
        require_benefit_plan_access(user, beneficiary.benefit_plan_id)
        pending = pending_approval(beneficiary)
        if pending is None:
            return None
        task, incoming_data = pending
        return PendingPlanApprovalGQLType(
            task_id=task.id,
            origin="upload" if task.business_event == UPLOAD_BUSINESS_EVENT else "form",
            submitted_on=task.date_created,
            submitted_by=approver_name(task.user_created) if task.user_created else None,
            incoming_data=incoming_data,
        )


class Mutation(graphene.ObjectType):
    submit_beneficiary_plan = SubmitBeneficiaryPlanMutation.Field()
