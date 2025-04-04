from rest_framework.response import Response
from rest_framework.generics import GenericAPIView


class GalxeVerifyView(GenericAPIView):

    def post(self, request, *args, **kwargs):

        address = request.POST.get("address", None)

        if not address:
            return Response({"success": False, "message": "Address required"})

        return Response({"success": True, "message": f"Address: {address}"})
