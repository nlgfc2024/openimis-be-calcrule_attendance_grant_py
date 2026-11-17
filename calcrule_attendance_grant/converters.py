from calcrule_social_protection.converters.beneficiary import (  # noqa: F401
    BeneficiaryToBenefitConverter,
    BeneficiaryToBillConverter,
    BeneficiaryToBillItemConverter,
)


class GrantBenefitConverter(BeneficiaryToBenefitConverter):
    """A BenefitConsumption that records how its tranche was calculated."""

    @classmethod
    def to_grant_benefit_obj(cls, entity, amount, payment_plan, payment_cycle, extra_info):
        benefit = cls.to_benefit_obj(entity, amount, payment_plan, payment_cycle)
        benefit["json_ext"] = {"extra_info": extra_info}
        return benefit
