from django.utils import timezone
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.contrib.contenttypes.models import ContentType
from django.core.validators import EmailValidator, MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from project.fields import CICharField
from project.utils.time import now
from project.validators import TypeValidator


class UserManager(BaseUserManager):
    def _create_user(self, email, password, **kwargs):
        if not email:
            raise ValueError("Users must have a email address")

        user = self.model(email=email, **kwargs)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **kwargs):
        kwargs["is_admin"] = False
        return self._create_user(email, password, **kwargs)

    def create_superuser(self, email, password, **kwargs):
        kwargs["is_admin"] = True
        return self._create_user(email, password, **kwargs)


class User(AbstractBaseUser):
    EMAIL_MAX_LENGTH = 128
    EMAIL_VALIDATORS = [
        TypeValidator(str),
        MaxLengthValidator(EMAIL_MAX_LENGTH),
        EmailValidator(),
    ]

    email = CICharField(
        _("Email"),
        unique=True,
        db_index=True,
        max_length=EMAIL_MAX_LENGTH,
        validators=EMAIL_VALIDATORS,
    )
    password = models.CharField(_("Password"), max_length=128)
    name = models.CharField(_("Name"), max_length=128)

    is_active = models.BooleanField(_("Active"), default=True)
    is_admin = models.BooleanField(_("Admin"), default=False)

    last_login = models.DateTimeField(_("Last Login"), blank=True, null=True)
    date_joined = models.DateTimeField(_("Date Joined"), default=now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    @property
    def is_staff(self):
        return self.is_admin

    @property
    def is_superuser(self):
        return self.is_admin

    @property
    def can_authenticate(self):
        return self.is_active

    def __str__(self):
        return str(self.email)

    def has_perm(self, perm, obj=None):
        return self.is_active and self.is_admin

    def has_module_perms(self, app_label):
        return self.is_active and self.is_admin

    def get_all_permissions(self, obj=None):
        return []

    class Meta(AbstractBaseUser.Meta):
        verbose_name = _("User")
        verbose_name_plural = _("Users")
        ordering = ["-date_joined"]


class Contact(models.Model):

    address = models.CharField(max_length=128)
    email = models.EmailField()

    def __str__(self):
        return f"{self.email} ({self.address})"

    class Meta:
        unique_together = [("address", "email")]


class AuthToken(models.Model):

    address = models.CharField(max_length=64)
    token = models.TextField()
    message = models.CharField(max_length=128)
    signature = models.TextField()

    expires_at = models.DateTimeField()

    @property
    def is_valid(self) -> bool:
        now = timezone.now()
        return self.expires_at >= now

    def __str__(self):
        return f"{self.address} {self.expires_at}"

    @property
    def email(self):
        contact = Contact.objects.filter(address=self.address).first()
        if contact:
            return contact.email
        return None
