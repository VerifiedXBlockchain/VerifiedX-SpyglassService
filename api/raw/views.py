from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework import status
from typing import Tuple

import rbx.client as client
from api.raw.serializers import RawTransactionSerializer
from shop.tasks import remote_nft_media_to_urls


class TransactionView(GenericAPIView):
    serializer_class = RawTransactionSerializer

    def process(self, transaction) -> Tuple[dict, int]:
        raise NotImplemented()

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        transaction = serializer.validated_data.get("transaction")
        data = self.process(transaction)
        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(data, status=status.HTTP_200_OK)

    class Meta:
        abstract = True


class RetrieveTransactionFeeView(TransactionView):
    def process(self, transaction) -> Tuple[dict, int]:
        return client.tx_get_fee(transaction)


class RetrieveTransactionHashView(TransactionView):
    def process(self, transaction) -> Tuple[dict, int]:
        return client.tx_get_hash(transaction)


class VerifyTransactionView(TransactionView):
    def process(self, transaction) -> Tuple[dict, int]:
        return client.tx_verify(transaction)


class SendTransactionView(TransactionView):
    def process(self, transaction) -> Tuple[dict, int]:
        return client.tx_send(transaction)


class RetrieveTimestampView(GenericAPIView):
    def post(self, request, *args, **kwargs):
        data = client.get_timestamp()
        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(data, status=status.HTTP_200_OK)


class RetrieveAddressNonceView(GenericAPIView):
    def post(self, request, *args, **kwargs):
        data = client.get_address_nonce(kwargs["address"])
        if data is None:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(data, status=status.HTTP_200_OK)


class ValidateSignatureView(GenericAPIView):
    def post(self, request, *args, **kwargs):
        is_valid = client.validate_signature(
            kwargs["message"], kwargs["address"], kwargs["signature"]
        )
        if not is_valid:
            return Response(False, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(True, status=status.HTTP_200_OK)


# class MintSmartContractView(GenericAPIView):
#     def post(self, request, *args, **kwargs):
#         data = client.mint_smart_contract(id=kwargs["id"])
#         if not data:
#             return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
#         return Response(data, status=status.HTTP_200_OK)


class RetrieveSmartContractView(GenericAPIView):
    def get(self, request, *args, **kwargs):
        data = client.retrieve_smart_contract(id=kwargs["id"])
        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(data, status=status.HTTP_200_OK)


class SmartContractDataView(GenericAPIView):
    def post(self, request, *args, **kwargs):

        data = client.nft_data(payload=request.data)

        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(data, status=status.HTTP_200_OK)


class NftTransferDataView(GenericAPIView):
    def post(self, request, *args, **kwargs):

        data = client.nft_transfer_data(
            id=kwargs["id"], address=kwargs["address"], locator=kwargs["locator"]
        )

        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(data, status=status.HTTP_200_OK)


class NftEvolveDataView(GenericAPIView):
    def post(self, request, *args, **kwargs):

        data = client.nft_evolve_data(
            id=kwargs["id"], address=kwargs["address"], next_state=kwargs["next_state"]
        )

        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(data, status=status.HTTP_200_OK)


class NftBurnDataView(GenericAPIView):
    def post(self, request, *args, **kwargs):
        data = client.nft_burn_data(id=kwargs["id"], address=kwargs["address"])
        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(data, status=status.HTTP_200_OK)


class LocatorsView(GenericAPIView):
    def get(self, request, *args, **kwargs):
        data = client.get_locators(id=kwargs["id"])
        if not data:
            return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(data, status=status.HTTP_200_OK)


class BeaconUploadRequestView(GenericAPIView):
    def get(self, request, *args, **kwargs):

        locator = client.beacon_upload_request(
            id=kwargs["id"],
            to_address=kwargs["to_address"],
            signature=kwargs["signature"],
        )

        if locator:
            return Response(
                {"success": True, "locator": locator}, status=status.HTTP_200_OK
            )

        return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BeaconAssetsView(GenericAPIView):
    def get(self, request, *args, **kwargs):

        sc_id = kwargs["id"]

        success = client.get_beacon_assets(
            id=sc_id,
            locators=kwargs["locators"],
            address=kwargs["address"],
            signature=kwargs["signature"],
        )

        if success:
            remote_nft_media_to_urls.apply_async(args=[sc_id], countdown=30)
            return Response({"success": True}, status=status.HTTP_200_OK)

        return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class WithdrawVbtcView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        data = request.data

        result = client.withdraw_btc(data)

        if result:
            return Response(
                {"success": True, "result": result}, status=status.HTTP_200_OK
            )

        return Response(
            {"success": False, result: None},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
