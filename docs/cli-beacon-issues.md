# CLI Beacon Issues — Analysis & Suggested Fixes

Two issues with the beacon/asset queue system that affect web wallet and butterfly reliability.

---

## Issue 1: First beacon upload fails after restart

After restarting the CLI, the first `CreateBeaconUploadRequest` call fails ("Failed to talk to beacon" or "Process Failed"). Second call works fine.

### Root Cause

Beacon connections are lazily initialized — not established at startup.

- `StartupService.LoadBeacons()` (lines 726-758) populates `Globals.Beacons` (beacon references from DB) at startup
- But `Globals.Beacon` (the actual connected beacon nodes) starts **empty**
- `TXV1Controller.CreateBeaconUploadRequest` (lines 630-639) checks `Globals.Beacon.Values.Where(x => x.IsConnected).Any()` — finds nothing on first call
- It then calls `BeaconUtility.EstablishBeaconConnection(true, false)` to lazily connect
- This first connection attempt often fails or times out, especially if called immediately after startup

### Suggested Fix

Call `EstablishBeaconConnection()` during startup (in `StartupService`) instead of waiting for the first API call. This warms up the beacon connections before any client requests arrive.

Something like:
```csharp
// In StartupService, after LoadBeacons():
_ = Task.Run(async () => {
    await Task.Delay(5000); // wait for network to settle
    await BeaconUtility.EstablishBeaconConnection(true, false);
});
```

### Files
- `Services/StartupService.cs` — lines 726-758 (LoadBeacons)
- `Controllers/TXV1Controller.cs` — lines 630-639 (lazy connect check)
- `Utilities/BeaconUtility.cs` — lines 12-76 (EstablishBeaconConnection)

---

## Issue 2: Failed transfer corrupts asset queue

When a beacon upload succeeds but the transfer TX fails or the CLI crashes before completion, the asset queue entry gets stuck. All future transfers for that smart contract are blocked until `rsrvassetqueue.db` is manually deleted and the CLI restarted.

### Root Cause

Two problems:

**A) Missing cleanup on the success path**

`SmartContractService.TransferSmartContract()` has cleanup (`aqDB.DeleteSafe(aq.Id)`) on every failure path:
- SC state null → cleanup (line 335)
- Account not found → cleanup (line 347)
- Insufficient balance → cleanup (line 394)
- Private key null → cleanup (line 407)
- Signature failed → cleanup (line 419)
- TX verification failed → cleanup (line 455)
- Beacon send failed → cleanup (line 467)
- Exception → cleanup (line 499)

But the **success path** (lines 449-450) returns WITHOUT deleting the queue entry:
```csharp
SCLogUtility.Log($"TX Success. SCUID: {scMain.SmartContractUID}", ...);
return;  // Queue entry NOT deleted
```

If the TX broadcast then fails in the P2P layer (network issue, pool rejection), the queue entry becomes orphaned with `IsComplete = false`.

**B) Queue blocks future transfers**

`AssetQueue.CreateAssetQueueItem()` (lines 103-107) checks for existing incomplete entries:
```csharp
var acRec = aqDB.FindOne(x => x.SmartContractUID == aq.SmartContractUID && 
                               x.AssetTransferType == aq.AssetTransferType && 
                               x.IsComplete != true);
if (acRec != null)
    return false;  // Blocks new entry creation
```

An orphaned incomplete entry blocks ALL future transfers for that SC. The only recovery is deleting the DB file.

**C) No startup cleanup**

There's no routine in `StartupService` that cleans orphaned queue entries on restart. A stale entry from a crashed CLI persists forever.

### Suggested Fixes

1. **Mark complete or delete on success** — after `WalletService.SendTransaction()` succeeds, either delete the queue entry or set `IsComplete = true`

2. **Startup cleanup** — on CLI startup, delete any queue entries older than N minutes (e.g., 30 min) that are still `IsComplete = false`. These are guaranteed to be stale.

3. **Timeout in CreateAssetQueueItem** — when checking for existing incomplete entries, also check the creation timestamp. If the entry is older than N minutes, delete it and allow the new one.

### Files
- `Services/SmartContractService.cs` — lines 449-450 (missing cleanup on success)
- `Models/AssetQueue.cs` — lines 103-107 (blocking logic), lines 50-90 (CreateAssetQueueItem)
- `Services/StartupService.cs` — no cleanup routine exists
- `Data/DbContext.cs` — line 156 (DB initialization, no validation)
