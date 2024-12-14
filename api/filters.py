from distutils.util import strtobool

from django_filters import *
from rest_framework.filters import OrderingFilter


class CharInFilter(BaseInFilter, CharFilter):
    pass


class ChoiceInFilter(BaseInFilter, ChoiceFilter):
    pass


class NumberInFilter(BaseInFilter, NumberFilter):
    pass


class M2MInFilter(Filter):
    def filter(self, queryset, value):
        if not value:
            return queryset
        return queryset.filter(
            **{f"{self.field_name}__in": value.split(",")}
        ).distinct()


class ReversibleOrderingFilter(OrderingFilter):
    def filter_queryset(self, request, queryset, view):
        try:
            reverse = "reverse" in request.query_params and strtobool(
                request.query_params["reverse"]
            )
        except ValueError:
            reverse = False

        if reverse:
            queryset = queryset.reverse()
        return super().filter_queryset(request, queryset, view)
