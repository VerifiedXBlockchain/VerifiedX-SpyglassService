# vBTC V2 Ownership Transfer — Butterfly Integration

## Context

Butterfly needs ownership transfer for the pre-activation flow: butterfly mints a vBTC token with its own service account, then transfers ownership to the user when they activate. This happens server-side (no user interaction for the transfer itself).

## When It's Used

1. **Pre-activation**: Celery task mints tokens for active users. When user logs in, butterfly transfers ownership.
2. **Potentially**: If butterfly ever needs to move tokens between service accounts.

## The Flow

Same 3-step flow as the web wallet, but executed server-side by butterfly's backend.

### Step 1: Beacon Upload

```python
import requests
from lib.auth import sign_message  # butterfly's signing utility

# Sign the SC identifier with butterfly's service key
signature = sign_message(sc_identifier, service_private_key)

response = requests.get(
    f"{SPYGLASS_URL}/raw/beacon/upload/{sc_identifier}/{to_address}/{signature}/"
)
data = response.json()
locator = data.get("locator")
```

### Step 2: Get Transfer TX Data

```python
response = requests.get(
    f"{SPYGLASS_URL}/btc/vbtc-v2/ownership-transfer/{sc_identifier}/{to_address}/{locator}/"
)
tx_data = response.json()  # JSON array with Transfer() payload
```

### Step 3: Build, Sign, Send Raw TX

```python
from vfx_client import VfxClient  # butterfly's existing VFX client

# Or use the raw TX pipeline directly:
timestamp = get_timestamp()  # POST /raw/timestamp/
nonce = get_nonce(from_address)  # POST /raw/nonce/{address}/

tx = {
    "FromAddress": service_address,  # butterfly's service account
    "ToAddress": to_address,  # user's VFX address
    "TransactionType": 18,  # TKNZ_TX
    "Amount": 0,
    "Data": tx_data,
    "Timestamp": timestamp,
    "Nonce": nonce,
    "Height": 0,
    "UnlockTime": None,
}

# Get fee
fee_response = requests.post(f"{SPYGLASS_URL}/raw/fee/", json={"transaction": tx})
tx["Fee"] = fee_response.json()["Fee"]

# Get hash
hash_response = requests.post(f"{SPYGLASS_URL}/raw/hash/", json={"transaction": tx})
tx["Hash"] = hash_response.json()["Hash"]

# Sign with butterfly's service key
signature = sign_vfx_transaction(tx["Hash"], service_private_key)
tx["Signature"] = signature

# Send
send_response = requests.post(f"{SPYGLASS_URL}/raw/send/", json={"transaction": tx})
result = send_response.json()
```

## Butterfly Backend Service

```python
class VbtcOwnershipTransferService:
    """Transfers vBTC V2 token ownership from butterfly's service account to a user."""
    
    def __init__(self):
        self.spyglass_url = settings.SPYGLASS_API_URL
        self.service_keypair = settings.VBTC_V2_SERVICE_KEYPAIR  # butterfly's minting account
    
    def transfer_ownership(self, sc_identifier: str, to_address: str) -> bool:
        """
        Transfer a vBTC V2 token from butterfly's service account to a user.
        Called after pre-activation when user logs in.
        """
        # Step 1: Beacon upload
        signature = self._sign(sc_identifier)
        beacon_resp = requests.get(
            f"{self.spyglass_url}/raw/beacon/upload/{sc_identifier}/{to_address}/{signature}/"
        )
        locator = beacon_resp.json().get("locator")
        if not locator:
            logger.error(f"Beacon upload failed for {sc_identifier}")
            return False
        
        # Step 2: Get transfer data
        data_resp = requests.get(
            f"{self.spyglass_url}/btc/vbtc-v2/ownership-transfer/{sc_identifier}/{to_address}/{locator}/"
        )
        tx_data = data_resp.json()
        if isinstance(tx_data, dict) and tx_data.get("Success") is False:
            logger.error(f"Transfer data failed: {tx_data.get('Message')}")
            return False
        
        # Step 3: Build and send raw TX
        tx_hash = self._build_sign_send(
            from_address=self.service_keypair.address,
            to_address=to_address,
            tx_data=tx_data,
            tx_type=18,  # TKNZ_TX
        )
        
        if tx_hash:
            logger.info(f"Ownership transferred: {sc_identifier} → {to_address} (TX: {tx_hash})")
            return True
        
        return False
    
    def _sign(self, message: str) -> str:
        """Sign a message with butterfly's service key."""
        # Use the same signing method as the VFX SDK
        return vfx_sign(message, self.service_keypair.private_key)
    
    def _build_sign_send(self, from_address, to_address, tx_data, tx_type) -> str | None:
        """Build, sign, and send a raw TX through Spyglass."""
        # ... standard raw TX pipeline (fee → hash → sign → send)
        pass
```

## Pre-Activation Integration

In the existing pre-activation Celery task flow:

```python
@celery_task(queue='vbtc')
def complete_pre_activation(user_id, sc_identifier):
    """Called when a pre-activated user logs in."""
    user = User.objects.get(id=user_id)
    token = UserVbtcToken.objects.get(user=user, status='pending_ownership')
    
    transfer_service = VbtcOwnershipTransferService()
    success = transfer_service.transfer_ownership(
        sc_identifier=token.sc_identifier,
        to_address=user.vfx_address,
    )
    
    if success:
        token.status = 'active'
        token.activated_at = timezone.now()
        token.save()
    else:
        logger.error(f"Ownership transfer failed for user {user_id}")
        # Retry later or notify
```

## Configuration

```python
# settings/vbtc.py
VBTC_V2_SERVICE_PRIVATE_KEY = env.str("VBTC_V2_SERVICE_PRIVATE_KEY")
VBTC_V2_SERVICE_ADDRESS = env.str("VBTC_V2_SERVICE_ADDRESS")
```

This is the VFX account butterfly uses for minting tokens. It needs enough VFX balance to cover minting fees and transfer fees.

## Signing

Butterfly needs VFX-compatible signing (secp256k1 ECDSA, same format as the web SDK). The existing `VfxClient` in butterfly-service or the `vfx-web-sdk` can provide this. The signature format is `${base64DERSignature}.${base58PublicKey}`.

## Dependencies

- Spyglass endpoints deployed: `/raw/beacon/upload/`, `/btc/vbtc-v2/ownership-transfer/`, `/raw/fee/`, `/raw/hash/`, `/raw/send/` — all deployed ✓
- CLI updated with `GetVBTCOwnershipTransferData` — deployed ✓
- Butterfly needs a VFX service account with VFX balance for gas
