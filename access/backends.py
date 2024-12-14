from django.contrib.auth import backends


class ModelBackend(backends.ModelBackend):
    def user_can_authenticate(self, user):
        return user.can_authenticate
