"""Old-style TokenTransfer(): the transaction's ToAddress is Token_Base and
the recipient is named only in the data. Core credits the data recipient's
token account when that recipient has an account record; when it has none,
Core creates the record keyed by the transaction's ToAddress, so the tokens
land on Token_Base (StateData.TokenTransfer). From PathAuditRulesHeight Core
refuses the shape (LedgerIntegrityRules), so only history below it is
affected.

Spyglass holds no account records. One exists on Core once the address has
sent a transaction, received a plain TX, or received a new-style
TokenTransfer() (ToAddress the recipient). An old-style transfer to it
creates none.
"""
from rbx.models import Transaction
from rbx.vbtc_gates import field, fund_loss_audit_height, net_string, parse_object

TOKEN_BASE = "Token_Base"


def is_legacy_shape(tx):
    return tx.to_address == TOKEN_BASE and tx.height < fund_loss_audit_height()


def has_account_record(address, before_tx):
    """Whether Core held an account record for address when it applied
    before_tx: some mined transaction of an earlier block created one."""
    if not address:
        return False
    earlier = Transaction.objects.filter(height__lt=before_tx.height)
    if earlier.filter(from_address=address).exists():
        return True
    if earlier.filter(to_address=address, type=Transaction.Type.TX).exists():
        return True
    from rbx.vbtc_dispatch import SC_ENVELOPE_TYPES

    for tx in earlier.filter(to_address=address, type__in=list(SC_ENVELOPE_TYPES)).only("data"):
        if net_string(field(parse_object(tx.data), "Function")) == "TokenTransfer()":
            return True
    return False


def credited_address(tx, recipient):
    """Whom Core credits for a TokenTransfer() naming `recipient`."""
    if is_legacy_shape(tx) and not has_account_record(recipient, tx):
        return TOKEN_BASE
    return recipient
