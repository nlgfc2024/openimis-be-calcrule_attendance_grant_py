from calcrule_social_protection.converters.beneficiary import (  # noqa: F401
    BeneficiaryToBenefitConverter,
    BeneficiaryToBillConverter,
    BeneficiaryToBillItemConverter,
)
from calcrule_social_protection.converters.group_beneficiary import (  # noqa: F401
    GroupToBillConverter,
    GroupToBillItemConverter,
)


class GrantBenefitConverter(BeneficiaryToBenefitConverter):
    """A BenefitConsumption that names its grant types and records its tranche.

    ``entity`` is the paid participant: a Beneficiary, or a household's primary
    worker as a GroupIndividual. Both expose ``.individual``.
    """

    @classmethod
    def to_grant_benefit_obj(cls, entity, amount, payment_plan, payment_cycle, benefit_type, json_ext):
        benefit = cls.to_benefit_obj(entity, amount, payment_plan, payment_cycle)
        benefit["type"] = benefit_type
        benefit["json_ext"] = json_ext
        return benefit
