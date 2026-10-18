"""Old-style TokenTransfer(): the transaction's ToAddress is Token_Base and
the recipient is named only in the data. Core credits the data recipient's
token account when that recipient has an account record; when it has none,
Core creates the record keyed by the transaction's ToAddress, so the tokens
land on Token_Base (StateData.TokenTransfer). From PathAuditRulesHeight Core
refuses the shape (LedgerIntegrityRules), so only history below it is
affected.

Spyglass holds no account records, so whether one existed is read from the
transactions that create one in Core's block apply (testnet-staging
f41b1ed7):

- the sender of any transaction (UpdateTreis), the transfer's own sender
  included, whose record is made before TokenTransfer runs;
- the recipient of a TX or a VFX unshield (UpdateTreis,
  PrivateTxLedgerService.CreditVfxUnshieldAsync) and of the genesis block;
- the recipient of a new-style TokenTransfer() (ToAddress the recipient) on
  a contract with TokenDetails: a token (feature 13) or a TokenizationV2
  contract (feature 14) deployed by TokenDeploy() (DeployTokenContract);
- the seller and royalty payee of a Sale_Complete() or M_Sale_Complete()
  (CompleteSaleSmartContract);
- the recovery address of a Recover() (RecoverReserveAccountTx).

An old-style transfer to the recipient creates none. Inside a block the
order is not stored: a record made in the transfer's own block counts when
the node's order puts it first (rbx.vbtc_bridge.chain_order).
"""
import json
import logging

from django.db.models import Q, TextField
from django.db.models.functions import Cast

from rbx.models import FungibleToken, Nft, Transaction
from rbx.vbtc_gates import bound_field, field, fund_loss_audit_height, net_string, parse_envelope, parse_object

TOKEN_BASE = "Token_Base"


def is_legacy_shape(tx):
    return tx.to_address == TOKEN_BASE and tx.height < fund_loss_audit_height()


def _creates_record_for(tx, address):
    """Whether applying tx creates (or needs) an account record for address."""
    if tx.from_address == address:
        return True
    if tx.to_address == address and (tx.type in Transaction.RECIPIENT_CREDIT_TYPES or tx.height == 0):
        return True
    payload = parse_object(tx.data)
    function = net_string(field(payload, "Function"))
    if (
        tx.to_address == address
        and function == "TokenTransfer()"
        and has_token_details(net_string(field(payload, "ContractUID")))
    ):
        return True
    if tx.type == Transaction.Type.NFT_SALE and function in SALE_COMPLETE_FUNCTIONS:
        return address in sale_payees(payload)
    if tx.type == Transaction.Type.RESERVE and function == "Recover()":
        return net_string(field(payload, "RecoveryAddress")) == address
    return False


SALE_COMPLETE_FUNCTIONS = ("Sale_Complete()", "M_Sale_Complete()")
TOKEN_FEATURE = 13
TOKENIZATION_V2_FEATURE = 14


def has_token_details(sc_uid):
    """Whether Core's state record of the contract carries TokenDetails: a
    TokenDeploy() of a contract with the Token feature (Spyglass's
    FungibleToken) or the TokenizationV2 feature (DeployTokenContract)."""
    if not sc_uid:
        return False
    if FungibleToken.objects.filter(sc_identifier=sc_uid).exists():
        return True
    nft = Nft.objects.filter(identifier=sc_uid).select_related("mint_transaction").first()
    if nft is None or nft.mint_transaction is None:
        return False
    payload, _ = parse_envelope(nft.mint_transaction.data)
    if net_string(field(payload, "Function")) != "TokenDeploy()":
        return False
    try:
        contract = json.loads(nft.smart_contract_data or "{}")
    except ValueError:
        return False
    features = contract.get("Features") if isinstance(contract, dict) else None
    return any(
        isinstance(f, dict) and f.get("FeatureName") in (TOKEN_FEATURE, TOKENIZATION_V2_FEATURE)
        for f in features or []
    )


def _is_true(value):
    """ToObject<bool?>(): a JSON true, or a string bool.Parse reads as true."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def sale_payees(payload):
    """The inner payments CompleteSaleSmartContract credits: with a royalty
    the ones marked 1/2 and 2/2, otherwise the first."""
    inner = field(payload, "Transactions")
    if not isinstance(inner, list):
        return set()
    inner = [t for t in inner if isinstance(t, dict)]
    if not inner:
        return set()

    def to(t):
        return net_string(bound_field(t, "ToAddress"))

    if _is_true(field(payload, "Royalty")):
        marked = [t for t in inner if "1/2" in str(bound_field(t, "Data") or "")][:1]
        marked += [t for t in inner if "2/2" in str(bound_field(t, "Data") or "")][:1]
        return {to(t) for t in marked if to(t)}
    return {to(inner[0])} if to(inner[0]) else set()


def _candidates(address, heights):
    """The transactions that can create a record for address: as sender or
    recipient, or a sale or recovery whose payload names it."""
    scope = Transaction.objects.filter(height__in=heights) if heights is not None else Transaction.objects.all()
    named = (
        scope.filter(type__in=[Transaction.Type.NFT_SALE, Transaction.Type.RESERVE])
        .annotate(data_text=Cast("data", TextField()))
        .filter(data_text__contains=address)
    )
    return scope.filter(Q(from_address=address) | Q(to_address=address)) | named


def has_account_record(address, before_tx):
    """Whether Core held an account record for address when it applied
    before_tx."""
    if not address:
        return False
    if before_tx.from_address == address:
        return True
    earlier = _candidates(address, None).filter(height__lt=before_tx.height)
    for tx in earlier.only("hash", "height", "type", "from_address", "to_address", "data").iterator():
        if _creates_record_for(tx, address):
            return True
    same_block = [
        tx
        for tx in _candidates(address, [before_tx.height]).exclude(pk=before_tx.pk)
        if _creates_record_for(tx, address)
    ]
    if not same_block:
        return False
    from rbx.vbtc_bridge import chain_order

    order = chain_order(before_tx.height)
    if order is None or before_tx.hash not in order:
        logging.warning(
            f"TokenTransfer {before_tx.hash}: a record for {address} is made in the same block and the "
            f"node gave no order; the recipient is credited."
        )
        return True
    position = order.index(before_tx.hash)
    return any(tx.hash in order and order.index(tx.hash) < position for tx in same_block)


def credited_address(tx, recipient):
    """Whom Core credits for a TokenTransfer() naming `recipient`."""
    if is_legacy_shape(tx) and not has_account_record(recipient, tx):
        return TOKEN_BASE
    return recipient
