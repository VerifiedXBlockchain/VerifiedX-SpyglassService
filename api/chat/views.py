from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

RETIRED_BODY = {"detail": "Shop chat has been retired."}


@csrf_exempt
def chat_retired(request, *args, **kwargs):
    """The old shop chat API had no authentication: anyone could read,
    post to or delete any thread. Every path and method under /api/chat/
    now answers 410 without touching the database. The ChatThread and
    ChatMessage rows are kept."""
    return JsonResponse(RETIRED_BODY, status=410)
