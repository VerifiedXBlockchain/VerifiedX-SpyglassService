from project.settings.environment import ENV

# Debug
# https://docs.djangoproject.com/en/4.0/ref/settings/#debug

DEBUG = ENV.bool("DEBUG", default=False)
