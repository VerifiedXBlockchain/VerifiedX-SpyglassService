from django.db import models

# Create your models here.


class BtxSubTx:
    address: str
    amount: str

    def __init__(self, data: dict):
        self.address = data["address"]
        self.amount = data["amount"]

    def serialize(self):
        return {
            "address": self.address,
            "amount": self.amount,
        }


class BtcTxRecipient(BtxSubTx):
    pass


class BtcTxSender(BtxSubTx):
    pass


class BtcTx:
    id: str
    index: int
    hash: str
    recipients: list[BtcTxRecipient]
    senders: list[BtcTxSender]
    timestamp: int
    fee: str

    def __init__(self, data: dict):

        self.id = data["transactionId"]
        self.index = data["index"]
        self.hash = data["transactionHash"]
        self.recipients = [BtcTxRecipient(r) for r in data["recipients"]]
        self.senders = [BtcTxRecipient(r) for r in data["senders"]]
        self.timestamp = data["transactionHash"]
        self.fee = data["fee"]["amount"]

    def __str__(self):
        return f"{self.hash}"

    def serialize(self) -> dict:

        return {
            "id": self.id,
            "index": self.index,
            "hash": self.hash,
            "recipients": [r.serialize() for r in self.recipients],
            "senders": [s.serialize() for s in self.senders],
            "timestamp": self.timestamp,
            "fee": self.fee,
        }


class Utxo:
    address: str
    amount: str
    index: int
    is_available: bool
    is_confirmed: bool
    timestamp: int
    transaction_id: str

    def __init__(self, data: dict):
        self.address = data["address"]
        self.amount = data["amount"]
        self.index = data["index"]
        self.is_available = data["isAvailable"]
        self.is_confirmed = data["isConfirmed"]
        self.timestamp = data["timestamp"]
        self.transaction_id = data["transactionId"]

    def serialize(self):
        return {
            "address": self.address,
            "amount": self.amount,
            "index": self.index,
            "is_available": self.is_available,
            "is_confirmed": self.is_confirmed,
            "timestamp": self.timestamp,
            "transaction_id": self.transaction_id,
        }
