from django.urls import path
from api.chat.views import (
    ChatThreadListCreateView,
    ChatThreadRetrieveUpdateDestroyView,
    ChatMessageListCreateView,
    # ChatMessageDestroyView,
    ChatThreadLookupView,
    LatestChatMessagesView,
)

urlpatterns = [
    path("", ChatThreadListCreateView.as_view()),
    path("<uuid:uuid>/", ChatThreadRetrieveUpdateDestroyView.as_view()),
    path("lookup/", ChatThreadLookupView.as_view()),
    path("<uuid:thread_uuid>/message/", ChatMessageListCreateView.as_view()),
    path("new-messages/", LatestChatMessagesView.as_view()),
    # path("<int:thread_pk>/message/<int:pk>/", ChatMessageDestroyView.as_view()),
]
