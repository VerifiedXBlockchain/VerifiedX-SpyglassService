from distutils.util import strtobool

from django.db.models import Q
from django_filters import *
from rest_framework.filters import OrderingFilter, SearchFilter


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


class PrefixSearchFilter(SearchFilter):
    """`?search=` as an indexed prefix match instead of a substring scan.

    Each term matches rows where one of the view's `search_fields` starts
    with it (case-sensitive: addresses and hashes are). Terms combine with AND.
    A term shorter than the view's `search_min_length` (default 4) matches
    nothing. A view can define `search_q(term)` to return its own Q, or None
    to match nothing.
    """

    default_min_length = 4

    def filter_queryset(self, request, queryset, view):
        terms = self.get_search_terms(request)
        if not terms:
            return queryset

        min_length = getattr(view, "search_min_length", self.default_min_length)
        build = getattr(view, "search_q", None)
        fields = getattr(view, "search_fields", None) or []

        for term in terms:
            if build:
                condition = build(term)
            elif len(term) < min_length:
                condition = None
            else:
                condition = Q()
                for field in fields:
                    condition |= Q(**{f"{field}__startswith": term})
            if condition is None:
                return queryset.none()
            queryset = queryset.filter(condition)

        return queryset
