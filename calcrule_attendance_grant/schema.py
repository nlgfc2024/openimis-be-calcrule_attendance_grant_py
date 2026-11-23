import graphene

from calcrule_attendance_grant.gql_mutations import SubmitBeneficiaryPlanMutation


class Mutation(graphene.ObjectType):
    submit_beneficiary_plan = SubmitBeneficiaryPlanMutation.Field()
