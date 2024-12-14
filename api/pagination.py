from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    last_page_strings = ["last"]
    max_page_size = 500
    page_query_param = "page"
    page_size = 30
    page_size_query_param = "limit"

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "page": self.page.number,
                "num_pages": self.page.paginator.num_pages,
                "results": data,
            }
        )


class MasterNodePagination(StandardPagination):
    page_size = 15000
    max_page_size = 15000
