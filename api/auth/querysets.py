from django.contrib.auth import get_user_model

User = get_user_model()

ALL_USERS_QUERYSET = User.objects.all()
ACTIVE_USERS_QUERYSET = User.objects.filter(is_active=True)
