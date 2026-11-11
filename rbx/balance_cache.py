"""Address.get_balance(), cached for the API reads of /addresses/<a>/.

get_balance() sums every transaction, callback and recovery of an address
(about twelve aggregates; seconds for a busy address), and the wallets ask
for it per account on every block. An entry here holds the exact list
get_balance() returned, so a hit answers what a recompute would, as long as
none of its inputs moved:

- the rows of the address (transactions, the voided flag of one, callbacks,
  recoveries, the forged-VFX burn): every save of one queues its addresses,
  and when the database transaction commits their generation token is
  replaced (invalidate). sync_block also replaces the tokens of every
  address its block touched (rbx.block_addresses.affected_addresses);
- the chain time (rbx.models.chain_time), which decides which time-locked
  (reserve) sends are still locked: an entry records the address's next
  unlock time after the chain time it was computed at, and is used only
  while the chain time is still before it. Its cache lifetime also ends at
  that unlock time (wall clock), and never later than MAX_AGE;
- a bulk delete of transactions (a re-sync): the command calls
  invalidate_all(), which replaces the epoch every entry carries.

A reader takes the tokens before it computes and stores the entry under
them, so a block that commits while it computes leaves that entry unused.
Only the API views read through here; the indexer calls get_balance()
directly. Any cache failure falls back to an uncached get_balance().
"""

import logging
import threading
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.db.models import Min, Q
from django.utils import timezone

PREFIX = "addrbal:v1"
EPOCH_KEY = f"{PREFIX}:epoch"


def _max_age():
    return getattr(settings, "ADDRESS_BALANCE_CACHE_MAX_AGE", 300)


def _enabled():
    return getattr(settings, "ADDRESS_BALANCE_CACHE_ENABLED", True)


def _token_age():
    # Longer than any entry, so a token is not dropped under a live entry. A
    # dropped token is replaced by a fresh one, which no entry matches.
    return _max_age() * 4


def _gen_key(address):
    return f"{PREFIX}:gen:{address}"


def _entry_key(address):
    return f"{PREFIX}:entry:{address}"


def _new_token():
    return uuid.uuid4().hex


def _token(key, known):
    if known is not None:
        return known
    fresh = _new_token()
    if cache.add(key, fresh, _token_age()):
        return fresh
    return cache.get(key)


def next_unlock_after(address, now):
    """The earliest unlock time after `now` of a transaction to or from the
    address that is not voided: the next moment get_balance() can change
    without a write. A superset of the rows get_balance() reads as locked,
    so it is never later than the true one."""
    from rbx.models import Transaction

    return (
        Transaction.objects.filter(Q(to_address=address) | Q(from_address=address))
        .filter(voided_from_callback=False, unlock_time__gt=now)
        .aggregate(next_unlock=Min("unlock_time"))["next_unlock"]
    )


def balance(address_row, now=None):
    """get_balance() of an Address row, from the cache when still exact.
    `now` is chain_time(), for a caller that reads several addresses; it is
    read here when not given."""
    from rbx.models import chain_time

    if not _enabled():
        return address_row.get_balance()

    address = address_row.address
    try:
        found = cache.get_many([EPOCH_KEY, _gen_key(address), _entry_key(address)])
        epoch = _token(EPOCH_KEY, found.get(EPOCH_KEY))
        gen = _token(_gen_key(address), found.get(_gen_key(address)))
    except Exception:
        logging.exception("Address balance cache read failed; computing uncached")
        return address_row.get_balance()

    if now is None:
        now = chain_time()
    entry = found.get(_entry_key(address))
    if (
        entry is not None
        and epoch is not None
        and gen is not None
        and now is not None
        and entry["epoch"] == epoch
        and entry["gen"] == gen
        and (entry["until"] is None or now < entry["until"])
    ):
        return entry["value"]

    value = address_row.get_balance()
    if now is None or epoch is None or gen is None:
        # Nothing indexed yet (every lock counts), or the cache did not hold
        # a token: nothing to key the entry by.
        return value

    try:
        until = next_unlock_after(address, now)
        timeout = _max_age()
        if until is not None:
            timeout = max(1, min(timeout, int((until - timezone.now()) / timedelta(seconds=1)) + 1))
        cache.set(
            _entry_key(address),
            {"epoch": epoch, "gen": gen, "until": until, "value": value},
            timeout,
        )
    except Exception:
        logging.exception("Address balance cache write failed")
    return value


def invalidate(addresses):
    """Make the cached balances of these addresses miss. Call after the
    writes are committed (queue_invalidation does that)."""
    addresses = {a for a in addresses if a}
    if not addresses:
        return
    try:
        cache.set_many({_gen_key(a): _new_token() for a in addresses}, _token_age())
    except Exception:
        logging.exception(f"Address balance cache invalidation failed for {len(addresses)} addresses")


def invalidate_all():
    try:
        cache.set(EPOCH_KEY, _new_token(), None)
    except Exception:
        logging.exception("Address balance cache invalidation (all) failed")


_pending = threading.local()


def _flush():
    addresses = getattr(_pending, "addresses", None)
    _pending.addresses = set()
    if addresses:
        invalidate(addresses)


def queue_invalidation(*addresses):
    """Invalidate these addresses once the current database transaction
    commits (at once outside one). Addresses of a rolled-back transaction
    are invalidated at the next commit: harmless."""
    pending = getattr(_pending, "addresses", None)
    if pending is None:
        pending = _pending.addresses = set()
    pending.update(a for a in addresses if a)
    db_transaction.on_commit(_flush)


# post_save receivers, connected in RBXConfig.ready(). No post_delete
# receiver: one would turn the cascade of a bulk Block delete into a
# row-by-row delete; the commands that delete call invalidate_all().


def on_transaction_saved(sender, instance, **kwargs):
    queue_invalidation(instance.to_address, instance.from_address)


def on_callback_saved(sender, instance, **kwargs):
    queue_invalidation(instance.to_address, instance.from_address)


def on_recovery_saved(sender, instance, **kwargs):
    queue_invalidation(instance.original_address, instance.new_address)


def on_forged_burn_saved(sender, instance, **kwargs):
    queue_invalidation(instance.address)
