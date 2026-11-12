from datetime import timedelta

from django.utils import timezone

from access.models import AuthToken
from p2p.models import Collection
from p2p.tests.helpers import BUYER, SELLER, P2PTestCase, make_nft, make_shop, sign_in

WRITES = [
    ("POST", "/shops/coolstuff/collections/"),
    ("PATCH", "/collections/{collection}/"),
    ("DELETE", "/collections/{collection}/"),
    ("POST", "/collections/{collection}/listings/"),
    ("PATCH", "/listings/1/"),
    ("DELETE", "/listings/1/"),
    ("POST", "/listings/1/bids/"),
]


class TokenTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.collection = self.shop.collections.get()

    def paths(self):
        return [(method, path.format(collection=self.collection.pk)) for method, path in WRITES]

    def test_every_write_needs_a_token(self):
        for method, path in self.paths():
            with self.subTest(method=method, path=path):
                self.assertError(self.call(method, path, {}), 401, "not_authenticated")

    def test_an_unknown_token_is_refused(self):
        for method, path in self.paths():
            with self.subTest(method=method, path=path):
                self.assertError(self.call(method, path, {}, token="nope"), 401, "token_invalid")

    def test_an_expired_token_is_refused(self):
        token = sign_in(str(SELLER))
        AuthToken.objects.filter(token=token).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertError(
            self.call("POST", "/shops/coolstuff/collections/", {"name": "New"}, token=token), 401, "token_expired"
        )

    def test_bearer_and_basic_are_both_read(self):
        token = sign_in(str(SELLER))
        for scheme in ("Bearer", "bearer", "basic", "Basic"):
            with self.subTest(scheme=scheme):
                response = self.call("POST", "/shops/coolstuff/collections/", {"name": f"By {scheme}"},
                                     token=token, scheme=scheme)
                self.assertEqual(response.status_code, 201, response.content)

    def test_another_scheme_is_not_a_sign_in_token(self):
        # "Token" is DRF's own scheme (rest_framework.authtoken), not a sign-in token.
        token = sign_in(str(SELLER))
        self.assertError(self.call("POST", "/shops/coolstuff/collections/", {"name": "x"}, token=token,
                                   scheme="Token"), 401, "token_invalid")
        self.assertFalse(Collection.objects.filter(name="x").exists())

    def test_the_actor_is_the_token_never_the_body(self):
        token = sign_in(str(BUYER))
        response = self.call(
            "POST", "/shops/coolstuff/collections/",
            {"name": "Hijack", "owner_address": str(SELLER), "address": str(SELLER)}, token=token,
        )
        self.assertError(response, 403, "not_shop_owner")
        self.assertFalse(Collection.objects.filter(name="Hijack").exists())

        make_nft("nft:1", SELLER)
        response = self.call("POST", f"/collections/{self.collection.pk}/listings/", {
            "smart_contract_uid": "nft:1", "buy_now_price": "5", "seller_address": str(SELLER),
            "starts_at": timezone.now().isoformat(), "ends_at": (timezone.now() + timedelta(days=1)).isoformat(),
        }, token=token)
        self.assertError(response, 403, "not_shop_owner")

    def test_reads_are_public_even_with_a_stale_token(self):
        self.assertEqual(self.call("GET", "/shops/").status_code, 200)
        self.assertEqual(self.call("GET", "/shops/coolstuff/", token="stale").status_code, 200)

    def test_me_needs_a_token(self):
        self.assertError(self.call("GET", "/me/"), 401, "not_authenticated")
        self.assertEqual(self.call("GET", "/me/", token=sign_in(str(SELLER))).status_code, 200)
