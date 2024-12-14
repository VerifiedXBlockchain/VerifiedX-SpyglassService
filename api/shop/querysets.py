from shop.models import Shop, Collection, Listing


ALL_SHOPS_QUERYSET = Shop.objects.filter(is_deleted=False)
ALL_COLLECTIONS_QUERYSET = Collection.objects.filter(is_deleted=False)
ALL_LISTINGS_QUERYSET = Listing.objects.filter(is_deleted=False, is_sale_complete=False)
