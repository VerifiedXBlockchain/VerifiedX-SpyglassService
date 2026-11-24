import io

from django.conf import settings
from rest_framework.parsers import JSONParser

# The key in request.parser_context that holds the request's JSON text.
JSON_TEXT = "json_text"


class TextKeepingJSONParser(JSONParser):
    """DRF's JSONParser, which also keeps the JSON text it parsed.

    The parsed data (and every parse error) is exactly JSONParser's. The text
    goes into request.parser_context[JSON_TEXT] so a view can forward part of
    the request as the client wrote it.
    """

    def parse(self, stream, media_type=None, parser_context=None):
        parser_context = parser_context if parser_context is not None else {}
        raw = stream.read() if stream is not None else b""
        data = super().parse(io.BytesIO(raw), media_type, parser_context)
        # JSONParser decoded the same bytes with this encoding, so this cannot fail.
        parser_context[JSON_TEXT] = raw.decode(parser_context.get("encoding", settings.DEFAULT_CHARSET))
        return data
