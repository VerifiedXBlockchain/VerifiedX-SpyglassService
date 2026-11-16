"""Who is asking: the address behind a sign-in token.

A client gets a one-hour token from POST /api/auth/sign-token/ (a signature
over a timed message, api.auth.serializers) and sends it as
`Authorization: Bearer <token>`; the old clients' `basic <token>` is still
read. The token's address is the actor of every P2P write: it is never taken
from the request body.
"""

from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission

from access.models import AuthToken
from p2p.errors import Refusal

SCHEMES = ("bearer", "basic")


class SignTokenUser:
    """The request.user of a token-authenticated request. Its pk keeps each
    address in its own throttle bucket."""

    is_authenticated = True
    is_anonymous = False
    is_active = True
    is_staff = False
    is_superuser = False

    def __init__(self, address):
        self.address = address
        self.pk = self.id = f"sign-token:{address}"

    def __str__(self):
        return self.address


class SignTokenAuthentication(BaseAuthentication):
    """Reads a sign-in token. A missing, unknown or expired token leaves the
    request anonymous and notes why; SignTokenRequired refuses it with that
    reason, so public reads still work with a stale header."""

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, value = header.strip().partition(" ")
        if scheme.lower() not in SCHEMES:
            return None
        value = value.strip()
        token = AuthToken.objects.filter(token=value).order_by("-expires_at").first() if value else None
        if token is None:
            request.p2p_auth_problem = ("token_invalid", "The sign-in token is not known. Sign in again.")
            return None
        if not token.is_valid:
            request.p2p_auth_problem = ("token_expired", "The sign-in token has expired. Sign in again.")
            return None
        return SignTokenUser(token.address), token

    def authenticate_header(self, request):
        return 'Bearer realm="api"'


class SignTokenRequired(BasePermission):
    def has_permission(self, request, view):
        if isinstance(request.auth, AuthToken):
            return True
        code, message = getattr(
            request, "p2p_auth_problem", ("not_authenticated", "Sign in: this needs a sign-in token.")
        )
        raise Refusal(401, code, message)


def actor_of(request) -> str:
    """The address the request acts for; only call behind SignTokenRequired."""
    return request.auth.address
