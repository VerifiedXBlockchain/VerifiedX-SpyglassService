from django.db.models import CharField


class CICharField(CharField):
    def to_python(self, value):
        return super().to_python(value).lower()
