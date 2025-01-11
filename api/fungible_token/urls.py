from django.urls import path
from api.fungible_token.views import (
    FungibleTokenListView,
    FungibleTokenRetrieveView,
    TokenVotingTopicListView,
    TokenVotingTopicDetailView,
)

urlpatterns = [
    path("", FungibleTokenListView.as_view()),
    path(
        "voting-topics/<str:topic_id>/",
        TokenVotingTopicDetailView.as_view(),
    ),
    path("<str:sc_identifier>/", FungibleTokenRetrieveView.as_view()),
    path("<str:sc_identifier>/voting-topics/", TokenVotingTopicListView.as_view()),
]
