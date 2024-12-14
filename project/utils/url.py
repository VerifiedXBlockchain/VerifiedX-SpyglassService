import re
from urllib.parse import parse_qs, urlparse

URL_RE = r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)"


def join_url(*parts, append_trailing_slash=False):
    url = "/".join([str(part).strip("/") for part in parts])
    if append_trailing_slash:
        url += "/"
    return url


def validate_url(url):
    return re.match(URL_RE, url)


def get_url_query_string(url):
    return parse_qs(urlparse(url).query)
