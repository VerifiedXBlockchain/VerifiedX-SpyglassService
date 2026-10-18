"""Reserve reversals the mainnet applied the old way.

Before Core commit 6b914353 (2026-08-30), CallBack() and Recover() refunded
the reserve sender on every node but flipped the transfer's reserve row only
on a node whose local wallet held the transaction. Every other node left the
row pending and, 24 hours later, paid the recipient as well. For these ten
control transactions Core keeps that behaviour (HistoricalTransactionExceptions
.MainnetReserveReversals): the sender (or the recovery address) is credited and
the recipient keeps the transfer. Spyglass mirrors the same list, bound the same
way: hash, block height, sender, function and, for CallBack(), the transfer
called back. Mainnet only.
"""

import json

from django.conf import settings

# hash -> (height, from_address, function, called_back_hash)
MAINNET_RESERVE_REVERSALS = {
    # Recover(): the two pending transfers to REjHm... (995.99 + 8,998.99)
    "2f20dff8b6264688179edb8bf911fef22647643ae3dda4b7983b5f780568c7ed": (1_243_110, "xRBX9Zgi3Dsyufu8xG4Yf1W95gdhXa1ApZ", "Recover()", None),
    # CallBack(): the transfer called back (amount, recipient)
    "e86c39058693c6a564a5afe21a5e2b6128f623a7a1cfd652e862133be2b968bb": (1_659_325, "xRBX68MKPk1rqXvB7jzsRW4BSTZCkkvHh7", "CallBack()", "bc76703f48f0e330c01001cc9abf5e2b1a76120b04aec2fce3e287225ed7542a"),  # 4,000 to RF3X6EB...
    "63bed6dbe633ef339be99056b3405f34c0932c559e56f3ea8e2226e0d3c482b4": (1_659_327, "xRBX68MKPk1rqXvB7jzsRW4BSTZCkkvHh7", "CallBack()", "b033369360f0484ac3e63cc6b484888964d1d9888aab8b3ea3c8da9ce009e691"),  # 1,500 to RBFKT...
    "7f285e062d6dcb355b9b13e1c90fb5ebf48c9fd81667376f18c70949038e6d79": (1_659_333, "xRBX68MKPk1rqXvB7jzsRW4BSTZCkkvHh7", "CallBack()", "76f22da053567c6c7a7f363219bf6092f1fb1277d4d4bb0d40df1632e9f70afa"),  # 3,500 to RBEE9...
    "a115b5d34c58bc61c5284de04bb163c4a39b9a803f31949cd9598ada889835eb": (1_659_381, "xRBX68MKPk1rqXvB7jzsRW4BSTZCkkvHh7", "CallBack()", "52f29b16076136c7edb2f37ac2c4fa8115af795fe0d41908ee7ada0f1f6314e5"),  # 3,500 to RBEE9...
    "d6c8c9d87c08a4f3724802dd80aacc88db83a038ae6a259834131fa6ec077fd4": (3_821_832, "xRBXapXc1PGevze59V7Wc4Gb5pK32ShvT9", "CallBack()", "cefdefdd32939e85bcb6ef5ef8f8c55f68293a515ba2d55321e3abbdba6826e5"),  # 10 to RQKDU...
    "9a1fd70f1735aa69c19e91aea2d27b0a121e7cf4acf512b9896c3d50554ca15b": (3_944_533, "xRBXYfe8TNEmptPriAehPpvPTdBQgVhebP", "CallBack()", "60af58affb14a79b322677870949dbc27636c4f0e89cc7c2afae94a540b85f3e"),  # 10 to RC8Jd...
    "f7aacfb052b9782619c24e3f4809aa025dd020f792224f93bfdec6cf04a79ac6": (5_099_581, "xRBX4G1QWZFjc6srKvxYeAYFsLkdC68d1z", "CallBack()", "c172b606c36eb8d79265204de8d4f6fde3c82d00e84745b60741bd17849e76c2"),  # 1 to RWwag...
    "f82d35452b020bb135274201dc82b0727c2157468b87e1f33c20688289b5b52b": (5_351_562, "xRBXYfe8TNEmptPriAehPpvPTdBQgVhebP", "CallBack()", "89ef3abae49ca2270cc6b9fc055befc27f923869bd5a3b195f86d0adc54c41b2"),  # 277,100 to RC9YF...
    "6238a4d64a3ae255ee05b0fb177bfa5915e76bf2dc774004d376a4faba461b56": (5_519_286, "xRBXTWkCBdbzJRrETU8ZMYRJBPzWuzx2Ei", "CallBack()", "bcb8f5ca2c36ce610f96215decb8840f2a4bad428ad54d602a8e4ce80ed46fba"),  # 0.01 to RW6fa...
}


def on_mainnet():
    # VBTC_NETWORK is the deployment's chain setting (testnet on the testnet and devnet apps).
    return getattr(settings, "VBTC_NETWORK", "mainnet") == "mainnet"


def matches(tx_hash, height, from_address, data):
    """Whether a mined RESERVE transaction (by its fields) is a listed reversal on mainnet."""
    if not on_mainnet():
        return False
    entry = MAINNET_RESERVE_REVERSALS.get(tx_hash)
    if entry is None:
        return False
    listed_height, listed_from, listed_function, called_back_hash = entry
    if height != listed_height or from_address != listed_from:
        return False
    try:
        parsed = json.loads(data) if isinstance(data, str) else data
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict) or parsed.get("Function") != listed_function:
        return False
    return called_back_hash is None or parsed.get("Hash") == called_back_hash


def recipient_keeps_transfer(tx):
    """Whether this CallBack()/Recover() leaves the reversed transfers with their recipients, as Core applied it."""
    from rbx.models import Transaction

    return tx.type == Transaction.Type.RESERVE and matches(tx.hash, tx.height, tx.from_address, tx.data)
