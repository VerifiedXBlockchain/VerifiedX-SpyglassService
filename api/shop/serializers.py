from rest_framework import serializers
from shop.models import Shop, Collection, Listing, Auction, Bid
from api.nft.serializers import NftSerializer
from rbx.models import Nft


class ShopSerializer(serializers.ModelSerializer):
    def create(self, validated_data):

        owner_address = validated_data["owner_address"]

        validated_data["unique_id"] = Shop.generate_new_unique_id()
        validated_data["shop_id"] = Shop.generate_new_shop_id(owner_address)
        validated_data["is_third_party"] = True

        if "rbx://" not in validated_data["url"]:
            validated_data["url"] = f"rbx://{validated_data['url']}"

        # TODO: check network to see if it exists already
        validated_data["url"] = validated_data["url"].strip()

        return super().create(validated_data)

    class Meta:
        model = Shop

        fields = [
            "id",
            "unique_id",
            "name",
            "description",
            "owner_address",
            "is_third_party",
            "url",
            "is_published",
            "is_online",
        ]

        read_only_fields = [
            "id",
            "unique_id",
            "is_third_party",
            "is_published",
            "is_online",
        ]


class ShopUpdateSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        return ShopSerializer(instance).data

    class Meta:
        model = Shop
        fields = [
            "name",
            "description",
            "is_published",
        ]


class CollectionSerializer(serializers.ModelSerializer):

    shop = ShopSerializer(many=False)

    class Meta:
        model = Collection
        fields = [
            "id",
            "shop",
            "name",
            "is_live",
            "description",
        ]


class CollectionUpdateSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        return CollectionSerializer(instance).data

    class Meta:
        model = Collection
        fields = [
            "name",
            "description",
            "is_live",
        ]


class CreateCollectionSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        return CollectionSerializer(instance).data

    def create(self, validated_data):

        validated_data["collection_id"] = Collection.generate_new_collection_id()

        validated_data.pop("owner_address", None)

        return super().create(validated_data)

    class Meta:
        model = Collection
        fields = [
            "id",
            "name",
            "description",
        ]

        read_only_fields = [
            "id",
        ]


class AuctionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Auction
        fields = [
            "id",
            "current_bid_price",
            "increment_amount",
            "is_reserve_met",
            "is_auction_over",
            "current_winning_address",
        ]


class BidSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bid
        fields = [
            "id",
            "bid_id",
            "address",
            "signature",
            "amount",
            "send_time",
            "is_buy_now",
            "is_processed",
            "purchase_key",
            "status",
            "send_receive",
        ]


class ListingSerializer(serializers.ModelSerializer):

    collection = CollectionSerializer(many=False)
    nft = NftSerializer(many=False)
    thumbnails = serializers.ListField(child=serializers.CharField())
    auction = AuctionSerializer(many=False)
    bids = BidSerializer(many=True)
    # accepted_bids = serializers.SerializerMethodField()

    # def get_accepted_bids(self, listing):
    #     qs = Bid.objects.filter(listing_id=listing.id, status=Bid.BidStatus.ACCEPTED)
    #     serializer = BidSerializer(instance=qs, many=True)
    #     return serializer.data

    class Meta:
        model = Listing
        fields = [
            "id",
            "listing_id",
            "collection",
            "smart_contract_uid",
            "nft",
            "owner_address",
            "buy_now_price",
            "floor_price",
            "reserve_price",
            "start_date",
            "end_date",
            "has_started",
            "has_ended",
            "is_visible_before_start_date",
            "is_visible_after_end_date",
            "final_price",
            "is_sale_complete",
            "is_sale_pending",
            "winning_address",
            "thumbnails",
            "thumbnail_previews",
            "purchase_key",
            "auction",
            "bids",
            # "accepted_bids",
        ]


class ListingUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Listing
        fields = [
            "buy_now_price",
            "floor_price",  # TODO: make sure this can only change if auction has yet to start
            "reserve_price",  # TODO:  ^
            "start_date",  # TODO:  ^
            "end_date",  # TODO:  ^
        ]


class CreateListingSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        return ListingSerializer(instance).data

    def create(self, validated_data):

        # shop_id = None
        # try:
        #     collection = Collection.objects.get(id=validated_data["collection"])
        #     shop_id = collection.shop.id
        # except Collection.DoesNotExist:
        #     pass

        validated_data["listing_id"] = Listing.generate_new_listing_id(
            validated_data["collection"].shop.id
        )
        validated_data["purchase_key"] = Listing.generate_purchase_key()
        validated_data["is_visible_before_start_date"] = True
        validated_data["is_visible_after_end_date"] = True

        try:
            nft = Nft.objects.get(identifier=validated_data["smart_contract_uid"])
            validated_data["nft"] = nft
            validated_data["owner_address"] = nft.owner_address
        except Nft.DoesNotExist:
            pass

        listing = super().create(validated_data)

        if listing.is_auction:
            a = Auction(
                auction_id=Auction.generate_new_auction_id(),
                listing=listing,
                current_bid_price=listing.floor_price,
                is_auction_over=False,
                current_winning_address=listing.owner_address,
            )
            a.save()
        elif listing.is_buy_now:
            a = Auction(
                auction_id=Auction.generate_new_auction_id(),
                listing=listing,
                current_bid_price=listing.buy_now_price,
                is_auction_over=False,
                current_winning_address=listing.owner_address,
            )
            a.save()

        return listing

    class Meta:
        model = Listing
        fields = [
            "id",
            "collection",
            "smart_contract_uid",
            "buy_now_price",
            "floor_price",
            "reserve_price",
            "start_date",
            "end_date",
            "thumbnails",
        ]

        read_only_fields = [
            "id",
            "nft",
        ]


class CreateBidSerializer(serializers.Serializer):
    amount = serializers.DecimalField(decimal_places=16, max_digits=32)
    address = serializers.CharField()
    listing = serializers.IntegerField()
    from_third_party = serializers.BooleanField(default=False)
    is_buy_now = serializers.BooleanField(default=False)
    signature = serializers.CharField(required=False)
    pre_signed_sale_complete_tx = serializers.JSONField(required=False)


class AcceptedBidSerializer(serializers.Serializer):
    bid_id = serializers.CharField()
    amount = serializers.DecimalField(decimal_places=16, max_digits=32)
    address = serializers.CharField()
    listing = serializers.IntegerField()
    is_buy_now = serializers.BooleanField(default=False)
    signature = serializers.CharField()
