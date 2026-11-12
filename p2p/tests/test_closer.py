"""The closer (beat task) moves listings on without any read."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from p2p.models import Bid, Listing
from p2p.tests.helpers import BUYER, SELLER, make_listing, make_nft, make_shop, owner_of, travel


def add_bid(listing, address, amount, *, buy_now=False):
    return Bid.objects.create(listing=listing, address=str(address), amount=Decimal(amount), is_buy_now=buy_now,
                              signature=f"sig-{address}-{amount}", purchase_key=listing.purchase_key,
                              created_at=timezone.now())


class CloserTests(TestCase):
    def setUp(self):
        self.shop = make_shop(SELLER)
        soon = timezone.now() + timedelta(hours=1)
        self.won = make_listing(make_nft("nft:won", SELLER), shop=self.shop, buy_now=None, floor="1", ends=soon)
        add_bid(self.won, BUYER, "3")
        self.unsold = make_listing(make_nft("nft:unsold", SELLER), shop=self.shop, buy_now="5", ends=soon)
        self.reserve = make_listing(make_nft("nft:reserve", SELLER), shop=self.shop, buy_now=None, floor="1",
                                    reserve="10", ends=soon)
        add_bid(self.reserve, BUYER, "3")
        self.later = make_listing(make_nft("nft:later", SELLER), shop=self.shop, ends=soon + timedelta(days=10))

    def close(self):
        out = StringIO()
        call_command("close_p2p_listings", stdout=out)
        return out.getvalue()

    def states(self):
        return {l.smart_contract_uid: l.state for l in Listing.objects.all()}

    def test_nothing_is_due_before_the_end(self):
        self.assertIn("moved on: 0", self.close())
        self.assertEqual(set(self.states().values()), {"live"})

    def test_ended_auctions_close(self):
        with travel(timedelta(hours=2)):
            self.assertIn("moved on: 3", self.close())
            self.assertEqual(self.states(), {"nft:won": "awaiting_sale_start", "nft:unsold": "expired",
                                             "nft:reserve": "expired", "nft:later": "live"})
            won = Listing.objects.get(pk=self.won.pk)
            self.assertEqual(won.winner_address, str(BUYER))
            self.assertEqual(won.winning_bid.amount, Decimal("3"))
        with travel(timedelta(hours=60)):
            self.close()
            self.assertEqual(Listing.objects.get(pk=self.won.pk).state, "unfulfilled")

    def test_a_late_closer_goes_straight_to_unfulfilled(self):
        with travel(timedelta(days=3)):
            self.close()
        self.assertEqual(Listing.objects.get(pk=self.won.pk).state, "unfulfilled")

    def test_moved_and_burned_nfts_end_their_listings(self):
        owner_of(self.later.nft, BUYER)
        from rbx.models import Transaction
        from rbx.tests import make_block, make_tx

        burned = self.unsold.nft
        burned.burn_transaction = make_tx(make_block(99), "burn-x", Transaction.Type.NFT_BURN, str(SELLER), str(SELLER))
        burned.save()
        self.close()
        later = Listing.objects.get(pk=self.later.pk)
        unsold = Listing.objects.get(pk=self.unsold.pk)
        self.assertEqual((later.state, later.end_reason), ("ended", "nft_moved"))
        self.assertEqual((unsold.state, unsold.end_reason), ("ended", "nft_burned"))

    def test_a_started_sale_is_not_touched(self):
        Listing.objects.filter(pk=self.won.pk).update(state="sale_pending")
        owner_of(self.won.nft, BUYER)
        with travel(timedelta(days=5)):
            self.close()
        self.assertEqual(Listing.objects.get(pk=self.won.pk).state, "sale_pending")


class BeatTests(TestCase):
    def test_the_closer_is_scheduled_and_the_crawlers_are_not(self):
        from project.celery import setup_periodic_tasks

        sender = MagicMock()
        setup_periodic_tasks(sender)
        names = [call.kwargs.get("name") for call in sender.add_periodic_task.call_args_list]
        self.assertIn("Close P2P Listings", names)
        self.assertFalse([name for name in names if "Shop" in name or "Crawl" in name])
