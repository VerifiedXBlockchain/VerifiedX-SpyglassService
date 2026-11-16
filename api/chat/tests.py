import json
import uuid

from django.test import Client, SimpleTestCase, TestCase, override_settings
from drf_yasg import openapi
from drf_yasg.generators import OpenAPISchemaGenerator

from connect.models import ChatMessage, ChatThread
from shop.models import Shop

SHOP_URL = "rbx://legacy-shop"
BUYER = "RBUYER"
METHODS = ("get", "head", "post", "put", "patch", "delete", "options")
# What the old views read: a new thread for another buyer, or a message
# claiming to be from the shop.
BODY = json.dumps({"shop_url": SHOP_URL, "buyer_address": "RNEW", "is_third_party": False,
                   "body": "spoofed", "is_from_buyer": False})


def snapshot():
    """Every chat row, so a test can see that nothing was added, changed or
    removed."""
    return (
        list(ChatThread.objects.order_by("pk").values()),
        list(ChatMessage.objects.order_by("pk").values()),
    )


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class RetiredChatTests(TestCase):
    def setUp(self):
        # A real client sends no CSRF token.
        self.client = Client(enforce_csrf_checks=True)
        shop = Shop.objects.create(
            shop_id=1, unique_id="legacy", name="Legacy", url=SHOP_URL, description="", owner_address="ROWNER"
        )
        self.thread = ChatThread.objects.create(shop=shop, is_third_party=False, buyer_address=BUYER)
        ChatMessage.objects.create(thread=self.thread, is_from_buyer=True, body="hello")

    def paths(self):
        thread = self.thread.uuid
        return [
            f"/api/chat/?address={BUYER}",
            f"/api/chat/?shop_url={SHOP_URL}",
            f"/api/chat/{thread}/",
            f"/api/chat/{uuid.uuid4()}/",
            f"/api/chat/lookup/?url={SHOP_URL}&buyer_address={BUYER}",
            f"/api/chat/{thread}/message/",
            f"/api/chat/new-messages/?address={BUYER}",
            f"/api/chat/{thread}",
            "/api/chat/anything/else/",
        ]

    def send(self, method, path):
        if method in ("get", "head"):
            return getattr(self.client, method)(path)
        return getattr(self.client, method)(path, data=BODY, content_type="application/json")

    def test_every_path_and_method_is_gone_and_leaves_the_rows(self):
        before = snapshot()
        for path in self.paths():
            for method in METHODS:
                with self.subTest(path=path, method=method), self.assertNumQueries(0):
                    response = self.send(method, path)
                    self.assertEqual(response.status_code, 410)
                    if method != "head":
                        self.assertEqual(response.json(), {"detail": "Shop chat has been retired."})
        self.assertEqual(snapshot(), before)

    def test_a_form_post_is_gone_too(self):
        before = snapshot()
        response = self.client.post(f"/api/chat/{self.thread.uuid}/message/", {"body": "x", "is_from_buyer": "true"})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(snapshot(), before)


class RetiredChatDocsTests(SimpleTestCase):
    def test_the_api_docs_list_no_chat_paths(self):
        endpoints = OpenAPISchemaGenerator(openapi.Info(title="VFX Web API", default_version="v1")).get_endpoints(None)
        self.assertTrue(endpoints)
        self.assertEqual([path for path in endpoints if path.startswith("/api/chat/")], [])
