from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from access.models import AuthToken
from rbx.models import Nft

BUCKET = "test-nft-assets"
ORIGIN = f"https://{BUCKET}.s3.amazonaws.com/"


def make_nft(identifier="sc:media", owner="xOWNER", asset_urls=None):
    return Nft.objects.create(
        identifier=identifier,
        name="",
        minter_address=owner,
        owner_address=owner,
        minter_name="",
        primary_asset_name="",
        primary_asset_size=0,
        data="",
        smart_contract_data="",
        minted_at=timezone.now(),
        asset_urls=asset_urls,
    )


def make_auth_token(address, value="tok", expires_in=timedelta(hours=1)):
    return AuthToken.objects.create(
        address=address,
        token=value,
        message="m",
        signature="s",
        expires_at=timezone.now() + expires_in,
    )


@override_settings(AWS_BUCKET_NFT_ASSETS=BUCKET)
class AssociateMediaViewTests(TestCase):
    def post(self, media_map, token=None, sc_id="sc:media"):
        headers = {"HTTP_AUTHORIZATION": f"basic {token}"} if token else {}
        return self.client.post(
            f"/api/media/associate-media/{sc_id}/",
            {"media_map": media_map},
            content_type="application/json",
            **headers,
        )

    def test_first_association_without_a_token_succeeds_and_is_recorded(self):
        make_nft(asset_urls={})

        response = self.post({"a.png": f"{ORIGIN}abc/a.png"})

        self.assertEqual(response.status_code, 200)
        nft = Nft.objects.get(identifier="sc:media")
        self.assertEqual(nft.asset_urls, {"a.png": f"{ORIGIN}abc/a.png"})
        self.assertIsNotNone(nft.asset_urls_associated_at)
        self.assertEqual(nft.asset_urls_associated_by, "")

    def test_first_association_when_asset_urls_is_null_succeeds(self):
        make_nft(asset_urls=None)

        response = self.post({"a.png": f"{ORIGIN}abc/a.png"})

        self.assertEqual(response.status_code, 200)

    def test_overwrite_without_a_token_is_401_and_keeps_the_urls(self):
        make_nft(asset_urls={"a.png": f"{ORIGIN}old/a.png"})

        response = self.post({"a.png": f"{ORIGIN}new/a.png"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"message": "Not authenticated"})
        nft = Nft.objects.get(identifier="sc:media")
        self.assertEqual(nft.asset_urls, {"a.png": f"{ORIGIN}old/a.png"})

    def test_overwrite_with_another_addresss_token_is_401(self):
        make_nft(asset_urls={"a.png": f"{ORIGIN}old/a.png"})
        make_auth_token("xSOMEONE_ELSE", value="other")

        response = self.post({"a.png": f"{ORIGIN}new/a.png"}, token="other")

        self.assertEqual(response.status_code, 401)

    def test_overwrite_with_an_expired_owner_token_is_401(self):
        make_nft(asset_urls={"a.png": f"{ORIGIN}old/a.png"})
        make_auth_token("xOWNER", value="old", expires_in=timedelta(hours=-1))

        response = self.post({"a.png": f"{ORIGIN}new/a.png"}, token="old")

        self.assertEqual(response.status_code, 401)

    def test_overwrite_with_the_owners_token_succeeds(self):
        make_nft(asset_urls={"a.png": f"{ORIGIN}old/a.png"})
        make_auth_token("xOWNER", value="owner")

        response = self.post({"a.png": f"{ORIGIN}new/a.png"}, token="owner")

        self.assertEqual(response.status_code, 200)
        nft = Nft.objects.get(identifier="sc:media")
        self.assertEqual(nft.asset_urls, {"a.png": f"{ORIGIN}new/a.png"})
        self.assertEqual(nft.asset_urls_associated_by, "xOWNER")

    def test_a_url_outside_the_asset_bucket_is_400(self):
        make_nft(asset_urls={})

        response = self.post(
            {
                "a.png": f"{ORIGIN}abc/a.png",
                "b.png": "https://example.com/b.png",
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["assets"], ["b.png"])
        self.assertEqual(Nft.objects.get(identifier="sc:media").asset_urls, {})

    def test_a_lookalike_host_is_400(self):
        make_nft(asset_urls={})

        response = self.post(
            {"a.png": f"https://{BUCKET}.s3.amazonaws.com.example.com/a.png"}
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_nft_is_400(self):
        response = self.post({"a.png": f"{ORIGIN}abc/a.png"}, sc_id="sc:missing")

        self.assertEqual(response.status_code, 400)

    @override_settings(AWS_BUCKET_NFT_ASSETS=None)
    def test_no_bucket_configured_is_503(self):
        make_nft(asset_urls={})

        response = self.post({"a.png": f"{ORIGIN}abc/a.png"})

        self.assertEqual(response.status_code, 503)


class UploadAssetViewTests(TestCase):
    @override_settings(AWS_ACCESS_KEY=None, AWS_SECRET_KEY=None, AWS_BUCKET_NFT_ASSETS=None)
    def test_missing_aws_credentials_is_503(self):
        response = self.client.post("/api/media/", {})

        self.assertEqual(response.status_code, 503)
        self.assertIn("message", response.json())
