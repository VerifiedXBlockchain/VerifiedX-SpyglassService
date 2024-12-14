from smtplib import SMTPException

from django.conf import settings
from django.core.mail import send_mail
from django.utils.html import strip_tags

from connect.email.exceptions import EmailException


def send_email(subject, recipients, body, from_email=settings.EMAIL_FROM):
    if isinstance(recipients, str):
        recipients = (recipients,)

    try:
        send_mail(subject, strip_tags(body), from_email, recipients, html_message=body)
    except SMTPException as e:
        raise EmailException(str(e))
