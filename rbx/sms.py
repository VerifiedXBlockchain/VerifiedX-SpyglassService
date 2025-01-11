from django.conf import settings
from twilio.rest import Client

client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def send_sms(phone, body):
    return client.messages.create(to=phone, from_="+16473603707", body=body)


def send_verification_code(phone):

    verification = client.verify.v2.services(
        settings.TWILIO_VERIFY_SID
    ).verifications.create(to=phone, channel="sms")


def check_verification_code(phone, code):

    verification_check = client.verify.v2.services(
        settings.TWILIO_VERIFY_SID
    ).verification_checks.create(to=phone, code=code)

    return verification_check.status == "approved"
