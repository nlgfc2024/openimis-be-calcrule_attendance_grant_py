from django.core.exceptions import ValidationError


class AttendanceGrantConfigurationError(ValidationError):
    """The PaymentPlan is configured in a way the rule cannot run with.

    Raised before any participant is paid, so the whole payroll run stops.
    Participant data problems never raise; they only exclude that participant.
    """
