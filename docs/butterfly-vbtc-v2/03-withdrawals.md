# Withdrawals — 4-Step Flow, Constraints, Monitoring

## Any Holder Can Withdraw

The `OwnerAddress` param in `PrepareCompleteWithdrawalRaw` is really the "requestor address" — any address holding vBTC balance in a token can initiate a withdrawal from that token. The FROST signing uses the requestor's leader auth, and validators sign the BTC TX regardless of who the token owner is.

This means:
- User deposits BTC to their own token → can withdraw ✓
- User receives vBTC via transfer (balance in someone else's token) → can ALSO withdraw ✓
- The BTC comes from the token's deposit address (funded by whoever deposited)

## 4-Step Withdrawal Flow

Per D3, any holder can withdraw from any token they hold balance in. The backend validator picks the best token for the requested amount (see "Backend Withdrawal Service" below). The user does not pick the source token — they pick the amount + BTC destination, and the backend selects.

```
Step 1: Request (Type 27 TX on VFX chain)
  Frontend: prepare → sign → send via SDK
  Result: withdrawal_request_hash

Step 2: Prepare FROST
  Frontend: calls Spyglass prepare endpoint
  Result: session_id + two messages to sign

Step 3: Execute FROST (async)
  Frontend: signs messages → calls execute → polls job status
  Result: signed_btc_tx_hex
  Frontend: broadcasts BTC TX via Spyglass
  Result: btc_txid

Step 4: Record Completion (Type 28 TX on VFX chain)
  Frontend: prepare → sign → send via SDK
  Result: withdrawal recorded on-chain
```

**From the frontend's perspective, these 4 steps happen inside a single SDK call:** `client.requestWithdrawal({ scIdentifier, requestorAddress, btcAddress, amount, feeRate, privateKey, onProgress })` runs the entire sequence — prepare/sign/send Type 27, FROST polling, BTC broadcast, prepare/sign/send Type 28 — and emits `onProgress` callbacks per step. The diagram above is what's happening under the hood; the frontend code only loops on the `onProgress` events. The cancel UX (below) is the only place where the multi-step nature leaks back into the UI.

## Backend Withdrawal Service

Withdrawal can happen from ANY token the user has balance in. The backend picks the best token: not currently pending withdrawal, prefer the largest balance, prefer the user's primary as a tiebreaker.

```python
class VbtcWithdrawalService:
    def validate_withdrawal(self, user, amount: Decimal, btc_address: str) -> dict:
        """Pre-validate and select which token to withdraw from.

        Precedence (in order):
          1. Token is not currently pending withdrawal (hard filter).
          2. Largest single-token balance >= amount.
          3. Tiebreaker: user's primary token wins.
        """
        balance = balance_service.get_balance(user.vfx_address)

        total = Decimal(str(balance['total_balance']))
        if total <= 0:
            raise WithdrawalError(reason='not_activated', message='No vBTC balance available.')
        if total < amount:
            raise WithdrawalError(reason='insufficient_total', available=total, requested=amount)

        # Filter to tokens with no pending withdrawal, sort by (balance desc, primary first as tiebreaker).
        eligible = []
        for entry in balance['breakdown']:
            detail = spyglass.get_token_detail(entry['sc_identifier'])
            if detail.get('is_pending_withdrawal'):
                continue
            eligible.append(entry)
        eligible.sort(key=lambda x: (-Decimal(str(x['balance'])), not x['is_primary']))

        selected = next(
            (e for e in eligible if Decimal(str(e['balance'])) >= amount),
            None,
        )

        if not selected:
            largest = eligible[0] if eligible else None
            raise WithdrawalError(
                reason='insufficient_single_token',
                largest_single_token=largest['balance'] if largest else None,
                message=(
                    'No single token has enough balance for this withdrawal. '
                    'Withdraw a smaller amount or wait until balance consolidates.'
                ),
            )

        return {
            'sc_identifier': selected['sc_identifier'],
            'owner_address': user.vfx_address,
            'btc_address': btc_address,
            'amount': amount,
            'fee_estimate_sats': estimate_btc_fee(amount, fee_rate),
        }
```

## UTXO Constraint

Each withdrawal consumes UTXOs on the Bitcoin network. The change output must confirm (~10 min) before the next withdrawal can proceed. Since each user has their own token with their own deposit address, this only affects sequential withdrawals by the SAME user.

**Mitigation**: After a successful withdrawal, show "Your next withdrawal will be available after the previous one confirms on Bitcoin (~10 minutes)."

## Withdrawal Monitoring (SSE, not polling)

The SDK's `requestWithdrawal` drives the active path via its `onProgress` callback — that handles the in-flight tab. For two cases the SDK callback isn't enough:

1. **Other open tabs / devices.** Same user, two views. Tab A drives the withdrawal; Tab B needs to see the same status transitions.
2. **Cold reload mid-flow.** User closes the tab during FROST signing and reopens after BTC broadcast. The SDK is no longer in scope; the UI rehydrates from the backend.

Both are solved by a single SSE event:

```
vbtc:withdrawal_status
  payload: {
    withdrawal_request_hash: str,
    sc_identifier: str,
    status: 'withdraw_request_sent' | 'frost_signing' | 'btc_broadcast' | 'btc_confirmed' | 'completed' | 'failed' | 'cancelled',
    btc_txid: str | null,
    error_reason: str | null,
  }
```

Emitted by the new Celery task `monitor_v2_withdrawal_status`, which is driven by the on-chain timeline (Type 27 request → FROST progress polled from Spyglass → BTC broadcast → BTC confirmation → Type 28 completion) rather than by polling the SDK. Other tabs subscribed to the same user's SSE stream pick up status changes without polling.

For cold reload: the frontend on-mount calls the existing `getWithdrawals` read endpoint (against the preserved local `VbtcWithdrawal` model — see TC-6 in the gap analysis) to find any rows in non-terminal states, rehydrates the UI from that snapshot, then subscribes to SSE for live updates from that point forward.

## Withdrawal UX

**Before activation**: If the user has zero balance (no received vBTC, no activated token), the Withdraw page shows "Activate your BTC wallet to withdraw or deposit BTC to your VFX address from another holder." If the user has received vBTC from another holder but never activated, withdrawal still works — the validator picks the held token (D3) and the SDK can drive `requestWithdrawal` against that token's `sc_identifier`.

**Activated, no withdrawable balance**: Withdraw button shows available amount as 0. Tooltip: "Deposit BTC to your address or have someone send you vBTC to start withdrawing."

**Activated, balance available**: Normal flow. Amount field validates against `total_balance` (sum across all held tokens) — the backend picks which token to draw from. When the user's largest single-token balance is less than their total, show a hint: "You have 0.003 BTC available; the largest single deposit is 0.001 BTC. Withdraw up to 0.001 BTC at a time." (Multi-token single-withdrawal would need a node-side feature; not in MVP.)

**Pending withdrawal — pre-FROST**: "Requesting withdrawal..." with a visible **Cancel** button. Calls SDK `client.cancelWithdrawal({ scIdentifier, ownerAddress, withdrawalRequestHash, privateKey })`, which signs and submits a Type 30 cancel TX. Backend marks the row `cancelled`; SSE fires.

**Pending withdrawal — FROST started**: "Bitcoin signing in progress... (this can take 1-3 minutes)" — **Cancel button is hidden**. Once FROST yields a signed BTC TX, cancellation is no longer possible because the BTC is already broadcastable. The phase transition from `withdraw_request_sent` → `frost_signing` is the hide trigger; the frontend toggles on the SSE `status` field directly.

**Pending withdrawal — BTC broadcast**: "Sending Bitcoin to {address}..." with the BTC txid linked to a block explorer the moment we have it.

**Post-withdrawal**: "Withdrawal complete! BTC sent to {address}. TX: {btc_txid}" with link to block explorer.

## Withdrawal Limits

Consider adding:
- Minimum withdrawal: 0.0001 BTC (avoid dust)
- Maximum withdrawal: the user's largest single-token balance (multi-token single-withdrawal is not supported in MVP — would require a node-side TransferVBTCV2Multi-style feature).
- Rate limit: 1 withdrawal per 15 minutes (UTXO confirmation time)
