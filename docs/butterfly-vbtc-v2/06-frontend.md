# Frontend Implementation — SDK Integration, UI/UX

## Stack

- **Framework**: Next.js 15 with React 19 (App Router)
- **State**: React Context (`VfxWalletContext.tsx`) + TanStack React Query
- **UI**: DaisyUI + Tailwind CSS
- **SDK**: `vfx-web-sdk` **v3.0.0 (file-linked until published)**. `package.json` carries `"vfx-web-sdk": "file:../../vfx/vfx-web-sdk"` until npm publish. SDK must be built (`npm install && npm run build` in the SDK repo) before `npm install` in butterfly-web; if the SDK is rebuilt and imports break at runtime, recover with `rm -rf butterfly-web/node_modules/vfx-web-sdk && npm install`. Because file-link is non-portable (works on Tyler's laptop, not CI/Vercel/teammates), Phases 7–11 land on a feature branch — never main — until v3 is published.
- **Auth**: Signature-based (no JWT/session) via `getAuthHeaders()` in `src/lib/auth.ts`
- **Notifications**: `react-hot-toast`
- **QR codes**: `react-qr-code` (already at `^2.0.18` in `package.json`). No new dependency needed.

The V2 surface area on `VfxClient` after the bump: `createVbtcToken`, `transferVbtc`, `requestWithdrawal`, `cancelWithdrawal`, `getVbtcTokens`, `getVbtcTokenDetail`, plus helper getters `getVbtcTransfers` and `getVbtcWithdrawals` (useful for per-token activity tabs). The SDK is constructed once per network via `new VfxClient(isTestnet ? Network.Testnet : Network.Mainnet)` — V2 methods do not take a `network` parameter; the SDK is network-bound at construction.

## Key Files to Modify

| File | Purpose | Change |
|------|---------|--------|
| `src/contexts/VfxWalletContext.tsx` | Wallet state, balance, SSE (~987 lines) | Add activation state, deposit address, update balance source |
| `src/lib/api.ts` | API client (~1700 lines, 40+ methods) | Replace V1 vBTC methods, add activation + V2 balance endpoints |
| `src/lib/wallet.ts` | SDK wrapper (~100 lines) | Add V2 token creation, transfer, withdrawal methods |
| `src/lib/coins.ts` | Coin configs (~117 lines) | Update vBTC coin config (deposit address, activation status) |
| `src/app/my-account/btc/withdraw/page.tsx` | Withdrawal page (~1043 lines) | Rewrite for 4-step V2 flow |
| `src/components/shared/BalanceHUD.tsx` | Header balance display (~300 lines) | No change (reads from context) |
| `src/components/btc/WithdrawalHistory.tsx` | Withdrawal list | Update to use V2 data shape |

## New Files

| File | Purpose |
|------|---------|
| `src/app/my-account/btc/activate/page.tsx` | Activation page (MPC ceremony + contract creation) |
| `src/components/btc/ActivationCard.tsx` | Dashboard card prompting activation |
| `src/components/btc/DepositAddressCard.tsx` | Shows deposit address + QR |
| `src/lib/vbtcV2.ts` | V2-specific SDK operations (ceremony, multi-token send) |

## Wallet Context Changes

`src/contexts/VfxWalletContext.tsx` manages all wallet state.

### Add to state:
```typescript
// Consolidated activation status — single source of truth instead of separate booleans.
// Default 'unknown' so cold load does NOT briefly show the wrong CTA before the
// first refresh resolves.
vbtcActivationStatus: 'unknown' | 'needs_activation' | 'pending_ceremony' | 'active';

vbtcScIdentifier: string | null;
vbtcDepositAddress: string | null;
vbtcPrimaryBalance: string | null;  // string per Phase 2's Decimal-as-string convention
// existing vbtcBalance stays (total across all tokens; also a string in V2)
```

### Balance refresh:
Currently calls `butterflyApi.getUnifiedBalances()` which returns V1 balance. Update to call the new V2 balance endpoint:

```typescript
// In refreshAllBalances() / refreshVbtcV2Balance():
const vbtcData = await butterflyApi.getVbtcV2Balance(wallet.address, isTestnet);
setVbtcBalance(vbtcData.total_balance);                  // already a string
setVbtcPrimaryBalance(vbtcData.primary_balance);
setVbtcActivationStatus(vbtcData.is_activated ? 'active' : 'needs_activation');
setVbtcDepositAddress(vbtcData.deposit_address);
setVbtcScIdentifier(vbtcData.sc_identifier);
```

The initial `'unknown'` value means the dashboard renders a skeleton/spinner for the vBTC card on first paint instead of flashing the activation CTA — important because most users *will* be activated by the time V2 ships, so flashing "Activate now" is misleading.

### SSE events:
Existing SSE infrastructure in `useSSE` hook is extended with two new event types:

- `vbtc:activation_complete` — payload `{sc_identifier, deposit_address}`. Lets another tab/device on the same user see the deposit address without refresh after activation finishes on tab 1.
- `vbtc:withdrawal_status` — payload `{withdrawal_request_hash, sc_identifier, status, btc_txid, error_reason}`. Drives the withdrawal page status pill from `withdraw_request_sent` → `frost_signing` → `btc_broadcast` → `btc_confirmed` → `completed` (or `cancelled`/`failed`).

## SDK Usage Pattern

The app uses dynamic imports for the SDK (bundle splitting):

```typescript
// Existing pattern in src/lib/wallet.ts
const { VfxClient, Network } = await import('vfx-web-sdk');
const client = new VfxClient(isTestnet ? Network.Testnet : Network.Mainnet);
```

V2 methods already on VfxClient after SDK update:
```typescript
client.createVbtcToken({ ownerAddress, privateKey, name, description, ticker, onProgress })
client.transferVbtc({ scIdentifier, fromAddress, toAddress, amount, privateKey })
client.requestWithdrawal({ scIdentifier, requestorAddress, btcAddress, amount, feeRate, privateKey, onProgress })
client.cancelWithdrawal({ scIdentifier, ownerAddress, withdrawalRequestHash, privateKey })
client.getVbtcTokens(address)
client.getVbtcTokenDetail(scIdentifier)
```

## Keypair Access

Private key is available when wallet is unlocked:
```typescript
const { wallet, privateKey, isUnlocked } = useVfxWallet();
// privateKey is null when locked, hex string when unlocked
```

The existing `PasswordPromptModal` handles unlock flow. No changes needed.

## API Layer Changes

`src/lib/api.ts` — replace V1 methods:

### Remove:
```typescript
butterflyApi.getVbtcBalance()            // V1 balance
butterflyApi.prepareWithdrawal()          // V1 withdrawal
butterflyApi.broadcastWithdrawal()        // V1 withdrawal
butterflyApi.prepareVbtcTransfer()        // V1 transfer
```

### Add:
```typescript
butterflyApi.getVbtcV2Balance(address, isTestnet)           // Aggregated V2 balance
butterflyApi.getVbtcV2ActivationStatus(authHeaders)         // Activation status
butterflyApi.recordVbtcV2Activation(data, authHeaders)      // Record activation
butterflyApi.prepareVbtcV2Send(data, authHeaders)           // Multi-token send plan
butterflyApi.validateVbtcV2Withdrawal(data, authHeaders)    // Pre-validate withdrawal
```

Note: The SDK handles all Spyglass calls directly (ceremony, transfer, withdrawal). The butterfly backend API is only for orchestration (balance aggregation, send planning, activation recording).

## Activation Flow

### Entry point
Dashboard shows `ActivationCard` when `vbtcActivated === false`:

```
┌─────────────────────────────────────┐
│  Bitcoin Wallet                     │
│                                     │
│  Balance: $0.00                     │
│                                     │
│  [Activate Bitcoin Wallet]          │
│                                     │
│  Get your personal Bitcoin deposit  │
│  address to start receiving BTC.    │
└─────────────────────────────────────┘
```

### Activation page: `src/app/my-account/btc/activate/page.tsx`

```typescript
async function handleActivate() {
  if (!privateKey || !wallet) return;
  
  setActivating(true);
  
  const { VfxClient, Network } = await import('vfx-web-sdk');
  const client = new VfxClient(isTestnet ? Network.Testnet : Network.Mainnet);
  
  try {
    const result = await client.createVbtcToken({
      ownerAddress: wallet.address,
      privateKey: privateKey,
      name: 'vBTC',
      description: 'vBTC Token',
      ticker: 'vBTC',
      onProgress: (event) => {
        setProgress(event);
      },
    });
    
    // Record on backend
    const authHeaders = await getAuthHeaders(wallet.address, privateKey, isTestnet);
    await butterflyApi.recordVbtcV2Activation({
      sc_identifier: result.scIdentifier,
      deposit_address: result.depositAddress,
      ceremony_id: result.ceremonyId,
    }, authHeaders);
    
    // Update context
    setVbtcActivated(true);
    setVbtcDepositAddress(result.depositAddress);
    setVbtcScIdentifier(result.scIdentifier);
    
    toast.success('Bitcoin wallet activated!');
    router.push('/my-account');
  } catch (err) {
    toast.error('Activation failed. Please try again.');
  } finally {
    setActivating(false);
  }
}
```

### Progress UI during activation
Match existing app patterns (DaisyUI progress bar):
```
Phase 1: "Coordinating with validators..." (0-90%)
Phase 2: "Creating your Bitcoin address..." (90-100%)
Total time: 30-90 seconds
```

## Deposit + Receive Page

After activation, users land on a Deposit + Receive view (route: `/my-account/btc` or the existing BTC dashboard route) that exposes the personal deposit address. New component:

### `src/components/btc/DepositAddressCard.tsx`

```
┌─────────────────────────────────────┐
│  Your Bitcoin Deposit Address       │
│                                     │
│  ┌────────────┐                     │
│  │            │   bc1p...           │
│  │   [ QR ]   │   [Copy]            │
│  │            │                     │
│  └────────────┘                     │
│                                     │
│  Send Bitcoin to this address to    │
│  add to your vBTC balance.          │
│                                     │
│  Recent deposits:                   │
│   · 0.001 BTC — 2 minutes ago       │
│   · 0.0005 BTC — Yesterday          │
└─────────────────────────────────────┘
```

- QR code rendered with **`react-qr-code`** (already at `^2.0.18` in `package.json` — no new dependency). Use the `value` prop with the raw `vbtcDepositAddress` (no `bitcoin:` URI scheme for now; the existing app pattern uses raw addresses).
- Copy button uses the existing toast pattern (`react-hot-toast`).
- Recent deposits list reads from `client.getVbtcTransfers(scIdentifier)` (V2 SDK getter) and filters to deposits (BTC L1 → token UTXO), not internal Type 18 transfers.

`DepositAddressCard` is gated behind `vbtcActivationStatus === 'active'`. When `'needs_activation'`, the dashboard shows `ActivationCard` instead. When `'unknown'`, it shows a skeleton until the first balance refresh resolves.

## Multi-token Send UI

The send review page calls `prepareVbtcV2Send` and iterates the returned `transfers` list through `client.transferVbtc` one TX at a time.

### Single top-level "Sending..." indicator
The user does not see the multi-token complexity by default. The button transitions to a single `loading` state with the label "Sending...". The total amount and recipient are unchanged from the V1 UX.

### Per-TX progress underneath
Underneath the spinner, a small progress section shows per-TX state for users who care or who need to debug:

```
Sending 0.003 BTC to alice...
  ✓ Transfer 1 of 2  (0.001 from your wallet)
  ⋯ Transfer 2 of 2  (0.002 from your wallet)
```

The frontend tracks `completed = string[]` (sc_identifiers that landed). Each successful `transferVbtc` push pushes to `completed`. The progress section reads from `completed.length / plan.transfers.length`.

### Partial-failure resume
If `transferVbtc` rejects mid-iteration, the page surfaces an error toast and a "Retry the rest?" button. Clicking it re-calls `prepareVbtcV2Send` with `exclude_sc_identifiers: completed` so the backend planner cannot re-issue an already-settled token even if Spyglass hasn't indexed it yet. The user sees a clean retry — `Sending 0.002 BTC...` for the remaining amount. (See plan 02 "Cache invalidation" and plan 04 of the executable phase plan.)

The fail state preserves `completed` in `useState` only — if the user navigates away, the partial transfers are still on chain; they just have to re-initiate a new send for the remainder. This is a known limitation; no localStorage persistence in MVP.

## Withdrawal Status via SSE

The withdrawal page subscribes to `vbtc:withdrawal_status` events via the existing `useSSE` infrastructure. On mount (before subscribing) the page calls `butterflyApi.getWithdrawals()` to rehydrate UI state from any rows in non-terminal status (`pending`, `frost_signing`, `btc_broadcast`) — this handles cold reload mid-flow. Then the SSE subscription drives subsequent updates.

```typescript
// On mount
const pending = await butterflyApi.getWithdrawals({ status__in: ['pending', 'frost_signing', 'btc_broadcast'] });
if (pending.length > 0) {
  hydrateActiveWithdrawal(pending[0]);
}

// Then via SSE
useSSE('vbtc:withdrawal_status', (event) => {
  if (event.withdrawal_request_hash === activeWithdrawal?.hash) {
    updateStatus(event.status, event.btc_txid);
  }
});
```

This replaces the V1 polling loop and survives tab close / cold reload.

`PasswordPromptModal` continues to work unchanged. Activation, send, and withdrawal all require the wallet unlocked; the existing `useVfxWallet().privateKey` + prompt flow handles this.

## Send Flow Changes

The existing send flow lives at `/send/` with sub-pages for coin selection, amount, review.

### Current path for vBTC:
```
/send → /send/coin?coin=btc → /send/amount → /send/review
```

### Changes needed in review page (`src/app/send/review/page.tsx`):

Replace V1 `prepareVbtcTransfer` + raw TX signing with:

```typescript
// 1. Get send plan from backend (which tokens to send from)
const plan = await butterflyApi.prepareVbtcV2Send({
  to_address: recipientAddress,
  amount: sendAmount,
}, authHeaders);

// 2. Execute each transfer via SDK
for (const transfer of plan.transfers) {
  await client.transferVbtc({
    scIdentifier: transfer.sc_identifier,
    fromAddress: wallet.address,
    toAddress: recipientAddress,
    amount: transfer.amount,
    privateKey: privateKey,
  });
}
```

UI doesn't change — still shows single "Sending..." state regardless of how many transfers execute.

## Withdrawal Page Rewrite

`src/app/my-account/btc/withdraw/page.tsx` (~1043 lines) — significant rewrite.

### Current V1 flow (remove):
```
1. Enter amount + BTC address
2. prepareWithdrawal() → backend validates + calculates fees
3. client.withdrawVbtc() → signs + broadcasts withdrawal TX
4. client.completeVbtcWithdrawal() → signs completion TX
5. broadcastWithdrawal() → notifies backend
```

### New V2 flow:
```
1. Enter amount + BTC address + fee rate
2. validateVbtcV2Withdrawal() → backend validates + selects token
3. client.requestWithdrawal() → full 4-step flow with progress callbacks
4. Show progress: request → FROST signing → BTC broadcast → completion
5. Success: show BTC TX hash
```

### Key difference:
V1 had the backend do the heavy lifting (prepare, then client signs).
V2 has the SDK do everything (backend only validates upfront).

### Reuse from existing page:
- BTC address input + validation (testnet/mainnet format check)
- Fee rate selector
- Confirmation modal pattern
- Success/failure screens
- localStorage recovery pattern for interrupted withdrawals

### Replace:
- The multi-step signing process
- Backend prepare/broadcast calls
- Balance validation logic

## Existing Patterns to Follow

| Pattern | Where | How |
|---------|-------|-----|
| Form handling | Throughout | `react-hook-form` |
| Loading states | Buttons | `isLoading` state + DaisyUI `loading` class |
| Error handling | Transient | `react-hot-toast` |
| Error handling | Inline | Form validation messages |
| Navigation | Pages | `useRouter().push()` |
| Modals | Dialogs | DaisyUI `dialog` element (see `PasswordPromptModal`) |
| Dynamic import | SDK | `const { VfxClient } = await import('vfx-web-sdk')` |
| Auth headers | API calls | `getAuthHeaders(address, privateKey, isTestnet)` |

## Environment Variables

### Remove:
```
NEXT_PUBLIC_VBTC_CONTRACT_UID    # V1 single contract ID
```

### Add:
```
NEXT_PUBLIC_VBTC_V2_ENABLED      # Feature flag for gradual rollout
```

The V2 contract ID is per-user (stored in backend DB), not a global config.
