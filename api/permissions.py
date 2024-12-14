from rest_framework import permissions
from access.models import AuthToken


def address_permission(request, address):

    if request.method in (
        "HEAD",
        "OPTIONS",
    ):
        return True

    authorization = (
        request.headers["Authorization"] if "Authorization" in request.headers else None
    )

    if not authorization:
        return False

    token_value = authorization.replace("basic ", "")

    try:
        token = AuthToken.objects.get(token=token_value)
    except AuthToken.DoesNotExist:
        print("Token not found")
        return False

    if not token.is_valid:
        print("Token invalid (expired)")
        return False

    if token.address != address:
        print("Incorrect address")
        return False

    return True


def shop_permission(request, shop):
    return address_permission(request, shop.owner_address)


class IsShopOwner(permissions.DjangoObjectPermissions):
    def has_object_permission(self, request, view, shop):
        return shop_permission(request, shop)


class IsCollectionOwner(permissions.DjangoObjectPermissions):
    def has_object_permission(self, request, view, collection):
        shop = collection.shop
        return shop_permission(request, shop)


class IsListingOwner(permissions.DjangoObjectPermissions):
    def has_object_permission(self, request, view, listing):
        shop = listing.collection.shop
        return shop_permission(request, shop)


def is_authenticated_with_address(request, address):
    return address_permission(request, address)
