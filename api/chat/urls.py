from django.urls import re_path
from api.chat.views import chat_retired

urlpatterns = [
    # Every path under /api/chat/, the old ones and any other.
    re_path(r"^.*", chat_retired),
]
