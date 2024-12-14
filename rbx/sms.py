from django.conf import settings
from twilio.rest import Client

client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def send_sms(phone, body):
    return client.messages.create(to=phone, from_="+19896932837", body=body)
