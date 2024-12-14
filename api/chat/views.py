from rest_framework.generics import GenericAPIView
from rest_framework.mixins import (
    RetrieveModelMixin,
    ListModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
)

from .serializers import (
    ChatThreadSerializer,
    ChatThreadCreateSerializer,
    ChatThreadDetailSerializer,
    ChatMessageSerializer,
    ChatMessageCreateSerializer,
    LatestChatMessageSerializer,
)
from connect.models import ChatThread, ChatMessage
from api import exceptions
from rest_framework import status
from rest_framework.response import Response

from shop.models import Shop
from django.utils.decorators import method_decorator
from api.decorators import cache_request
from django.conf import settings


class ChatThreadListCreateView(ListModelMixin, CreateModelMixin, GenericAPIView):
    serializer_class = ChatThreadSerializer

    def get_queryset(self):
        address = self.request.query_params.get("address", None)
        shop_url = self.request.query_params.get("shop_url", None)

        if not address and not shop_url:
            raise exceptions.BadRequest("Param `address` or `shop_url` is required.")

        if address:
            return ChatThread.objects.filter(buyer_address=address)
        else:
            try:
                shop = Shop.objects.get(url=shop_url)
            except Shop.DoesNotExist:
                raise exceptions.BadRequest(f"shop with url `{shop_url}` not found")
            return ChatThread.objects.filter(shop=shop)

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.serializer_class = ChatThreadCreateSerializer
        return super().create(request, *args, **kwargs)


class ChatThreadLookupView(RetrieveModelMixin, GenericAPIView):
    serializer_class = ChatThreadDetailSerializer

    def get_object(self):
        url = self.request.query_params.get("url", None)
        buyer_address = self.request.query_params.get("buyer_address", None)

        if not url:
            raise exceptions.BadRequest("Param `url` is required.")
        if not "rbx://" in url:
            url = f"rbx://{url}"

        url = url.strip()

        if not buyer_address:
            raise exceptions.BadRequest("Param `buyer_address` is required.")

        try:
            return ChatThread.objects.get(shop__url=url, buyer_address=buyer_address)
        except ChatThread.DoesNotExist:
            raise exceptions.BadRequest(
                f"ChatThread with url '{url}' and buyer_address '{buyer_address}' does not exist."
            )

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class ChatThreadRetrieveUpdateDestroyView(
    RetrieveModelMixin, DestroyModelMixin, GenericAPIView
):
    serializer_class = ChatThreadDetailSerializer
    queryset = ChatThread.objects.all()
    lookup_field = "uuid"

    def get(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        thread = ChatThread.objects.get(uuid=kwargs["uuid"])
        thread.delete()
        return Response({"success": True}, status=status.HTTP_204_NO_CONTENT)


class ChatMessageListCreateView(ListModelMixin, CreateModelMixin, GenericAPIView):
    serializer_class = ChatMessageSerializer

    def get_queryset(self):
        return ChatMessage.objects.filter(thread_uuid=self.kwargs["thread_uuid"])

    def get(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.serializer_class = ChatMessageCreateSerializer

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        body = serializer.validated_data.get("body")
        is_from_buyer = serializer.validated_data.get("is_from_buyer")
        thread_uuid = kwargs["thread_uuid"]

        try:
            thread = ChatThread.objects.get(uuid=thread_uuid)
        except ChatThread.DoesNotExist:
            raise exceptions.BadRequest(f"thread with uuid `{thread_uuid}` not found")

        message = ChatMessage.objects.create(
            thread=thread,
            body=body,
            is_from_buyer=is_from_buyer,
            # is_delivered=bool(thread.shop.is_third_party),
            is_delivered=True,
        )

        return Response(
            ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED
        )


@method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class LatestChatMessagesView(GenericAPIView):
    serializer_class = LatestChatMessageSerializer

    queryset = ChatMessage.objects.all()

    def get(self, request, *args, **kwargs):
        return Response(
            {"results": []},
            status=status.HTTP_200_OK,
        )

        from django.db.models import Q
        from django.utils import timezone

        now = timezone.now()

        threshold = now - timezone.timedelta(seconds=90)
        address = self.request.query_params.get("address", None)
        if not address:
            raise exceptions.BadRequest("Param `address` is required.")

        messages = ChatMessage.objects.filter(
            Q(
                Q(created_at__gte=threshold) & Q(Q(thread__buyer_address=address))
                | Q(thread__shop__owner_address=address)
            )
        )

        data = []
        for message in messages:
            shop_address = message.thread.shop.url
            buyer_address = message.thread.buyer_address

            from_address = buyer_address if message.is_from_buyer else shop_address
            to_address = shop_address if message.is_from_buyer else buyer_address

            if to_address != address:
                continue

            data.append(
                {
                    "uuid": message.uuid,
                    "from_address": from_address,
                    "to_address": to_address,
                    "body": message.body,
                    "created_at": message.created_at,
                    "thread_uuid": str(message.thread.uuid),
                }
            )

        return Response(
            {"results": LatestChatMessageSerializer(data, many=True).data},
            status=status.HTTP_200_OK,
        )


# class ChatMessageDestroyView(DestroyModelMixin, GenericAPIView):

#     serializer_class = ChatThreadDetailSerializer
#     queryset = ChatThread.objects.all()

#     def delete(self, request, *args, **kwargs):
#         return super().destroy(request, *args, **kwargs)
