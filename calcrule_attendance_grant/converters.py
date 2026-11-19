from calcrule_social_protection.converters.beneficiary import (  # noqa: F401
    BeneficiaryToBenefitConverter,
    BeneficiaryToBillConverter,
    BeneficiaryToBillItemConverter,
)


class GrantBenefitConverter(BeneficiaryToBenefitConverter):
    """A BenefitConsumption that names its grant types and records its tranche."""

    @classmethod
    def to_grant_benefit_obj(cls, entity, amount, payment_plan, payment_cycle, benefit_type, json_ext):
        benefit = cls.to_benefit_obj(entity, amount, payment_plan, payment_cycle)
        benefit["type"] = benefit_type
        benefit["json_ext"] = json_ext
        return benefit
