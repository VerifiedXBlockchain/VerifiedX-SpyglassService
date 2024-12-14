from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _


@deconstructible
class TypeValidator:
    def __init__(
        self,
        expected_type,
        message=_("Expected type of `%(expected_type)s`, given `%(given_type)s`."),
        code="invalid_type",
    ):
        self.expected_type = expected_type
        self.message = message
        self.code = code

    def __call__(self, value):
        if type(value) != self.expected_type:
            raise ValidationError(
                self.message,
                code=self.code,
                params={
                    "given_type": type(value).__name__,
                    "expected_type": self.expected_type.__name__,
                },
            )

    def __eq__(self, other):
        return (
            isinstance(other, self.__class__)
            and self.expected_type == other.expected_type
            and self.message == other.message
            and self.code == other.code
        )
