from project.settings.environment import ENV


SOCKET_BASE_URL = ENV.str("SOCKET_BASE_URL", None)
SOCKET_TOKEN = ENV.str("SOCKET_TOKEN", None)
