import graphene


class PendingPlanApprovalGQLType(graphene.ObjectType):
    """A beneficiary's plan approval waiting for a decision."""
    task_id = graphene.UUID()
    # "form" for a single submission, "upload" for a file.
    origin = graphene.String()
    submitted_on = graphene.DateTime()
    submitted_by = graphene.String()
    # The proposed grant keys, as JSON.
    incoming_data = graphene.JSONString()
