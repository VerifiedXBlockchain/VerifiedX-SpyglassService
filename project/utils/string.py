import random
import string

from distutils.util import strtobool


def get_random_string(characters, length):
    return "".join(random.choice(characters) for _ in range(length))


def get_random_slug(length):
    return get_random_string(string.ascii_lowercase + string.digits, length)


def truncate_text(text, length=100, ellipsis="..."):
    return (
        text
        if len(text) <= length
        else " ".join(text[: length + 1].split(" ")[0:-1]) + ellipsis
    )


def string_to_bool(value):
    try:
        return strtobool(value)
    except ValueError:
        return False
