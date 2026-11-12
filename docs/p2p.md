# P2P shops (`p2p` app, `/api/p2p/`)

The central registry of shops, collections, listings and bids that the web wallet (and later the GUI) sells NFTs through. The design and its decisions (Q1 to Q13) are in `vfx-platform-context/docs/plans/2026-11-11-p2p-web-to-web-design.md`. The old `/api/shop/` stays readable for clients that still use it; the shop crawler is gone.

## Where things live

- `p2p/models.py`: `Shop`, `Collection`, `Listing`, `Bid`, with the unique constraints: a normalised URL and a `unique_id` per live shop, a purchase key once per contract, one active listing per NFT, one default collection per shop.
- `p2p/services.py`: every change of state. The API, the dry runs, the closer and the block indexer all call it.
- `p2p/rules.py`: Core's rules and constants (URL pattern, name and description limits, shop costs, bid message, payment tolerance, the bid increment ladder).
- `p2p/signatures.py`: bid signatures verified locally, as Core's `SignatureService.VerifySignature` does. No node is called. `p2p/tests/vectors/` holds signatures made by vfx-web-sdk's `signMessage` and the script that made them.
- `p2p/dryrun.py`: the dry-run chain steps. `p2p/indexer.py`: the same steps from real transactions in block sync.
- `p2p/auth.py`, `p2p/errors.py`, `p2p/serializers.py`, `p2p/views.py`, `p2p/urls.py`: the API.

## Rules that hold everywhere

- Spyglass never broadcasts or relays a shop transaction: nothing in `p2p` imports the node client or makes an HTTP request, and `p2p/tests/test_never_broadcast.py` fails if any P2P path reaches one.
- Shops come from the chain only (`DSTR`, or its dry run). Name, description, URL and owner change only through `DecShopUpdate()` (Q2).
- A listing's NFT must be owned by the shop owner, who is the token's address (Q3). Tokens and vBTC vault NFTs cannot be listed (Q13).
- URLs are normalised (`vfx://` or `rbx://` removed, lower case) and held by the first live shop (Q9). A later shop the chain accepted with the same URL is kept with `url_collision` set and is not served by URL.
- A dry run never changes what the chain owns, and the chain wins over a dry run. A dry-run step refuses a shop registered on chain (`shop_on_chain`) and a listing whose sale is on chain or that was made with dry runs off (`sale_on_chain`), checked on the locked row. A shop the chain registers takes the URL from a dry-run shop that held it (which is then kept aside), and takes over a dry-run shop with its `UniqueId`: the same owner's becomes the registered shop, anyone else's is deleted (`end_reason` `shop_registered_on_chain` for its listings). A chain `DecShopDelete()` only deletes a registered shop. A real `Sale_Start` supersedes a dry-run completion or cancellation.

## Auth

`POST /api/auth/sign-token/` returns a one-hour token. Every write sends `Authorization: Bearer <token>` (`basic <token>` is still read). The token's address is the actor; no write takes an address from its body. Reads are public; `GET /me/` needs the token. A missing, unknown or expired token on a write is a 401 with code `not_authenticated`, `token_invalid` or `token_expired`.

## Endpoints

JSON, snake_case, amounts as decimal strings with up to 8 places, times ISO 8601 UTC (`2026-11-11T22:00:00Z`). Lists are `{count, next, results}`, where `next` is the next page number or `null`; `?page=` picks a page and `?limit=` its size (30 by default, at most 100). Errors are `{"error": {"code", "message", "fields"}}`, with `fields` mapping a field (Core's names inside `data`, for example `data.DecShop.Name`) to one message, and status 400, 401, 403, 404 or 409.

| Method and path | Who | Returns |
|---|---|---|
| `GET /p2p/shops/?search=&owner=&page=` | anyone | `Shop[]`, newest first |
| `GET /p2p/shops/{url}/` | anyone | `Shop` with `collections` (the owner also sees hidden ones) |
| `GET /p2p/shops/available/?url=` | anyone | `{url, normalized, available, reason}`; `reason` is `taken`, `invalid` or null |
| `GET /p2p/shops/{url}/collections/` | anyone | `Collection[]` |
| `POST /p2p/shops/{url}/collections/` `{name, description, is_live}` | shop owner | `Collection` (201) |
| `GET /p2p/collections/{id}/` | anyone (a hidden one: the owner) | `Collection` (an addition to the contract) |
| `PATCH /p2p/collections/{id}/` `{name?, description?, is_live?}` | shop owner | `Collection` |
| `DELETE /p2p/collections/{id}/` | shop owner | 204; refused for the default collection and while it has active listings |
| `GET /p2p/shops/{url}/listings/?collection=&state=&page=` | anyone | `Listing[]`: active, and sold in the last 7 days, in live collections; the owner sees all |
| `POST /p2p/collections/{id}/listings/` | shop owner, who owns the NFT | `Listing` (201) |
| `GET /p2p/listings/{id}/` | anyone | `Listing` with `bids` (newest first) |
| `PATCH /p2p/listings/{id}/` | seller, while `live` with no bids | `Listing` |
| `DELETE /p2p/listings/{id}/` | seller, before `sale_pending` | the cancelled `Listing` |
| `GET /p2p/listings/{id}/bids/` | anyone | `BidSummary[]` |
| `POST /p2p/listings/{id}/bids/` `{amount, is_buy_now, signature}` | bidder | `{bid: BidSummary, listing: Listing}` (201) |
| `GET /p2p/me/` | token | `{shop, selling, buying}`; `selling` carries the winning bid's signature |
| `POST /p2p/dry-run/dstr/` `{tx}` | `tx.from_address` | `Shop` |
| `POST /p2p/dry-run/sale-start/` `{listing_id, tx}` | seller | `Listing` (`sale_pending`) |
| `POST /p2p/dry-run/sale-complete/` `{listing_id, tx}` | buyer | `Listing` (`sold`) |
| `POST /p2p/dry-run/sale-cancel/` `{listing_id, tx}` | seller | `Listing` (`ended`) |

`Listing` carries the design's fields plus `royalty_enforced` (as listed) and `end_reason` (why it closed without a sale: `nft_moved`, `nft_burned`, `shop_deleted`, `shop_owner_changed`, `sale_cancelled`, `sale_started_elsewhere`, or null). In `/me/`, each of `selling` has `action: "start_sale"` while `awaiting_sale_start`, and `winning_bid: {amount, address, signature}` once the listing has a winner (`awaiting_sale_start`, `sale_pending` and `sold`; null before): the seller's wallet builds `Sale_Start` from it, and the dry run requires `NextOwner`, `SoldFor` and `BidSignature` to be exactly that bid's. A bid's signature appears nowhere else: no `Listing` or `BidSummary`, and no one else's `/me/`. Each of `buying` is `{listing, my_high_bid, action}` with `action: "complete_purchase"` once the sale is started for that buyer.

## Listings and bids

`live` goes to `awaiting_sale_start` on a buy-now bid or when an auction ends with a high bid that meets the reserve, then to `sale_pending` (`Sale_Start`) and `sold` (`Sale_Complete`). It leaves as `cancelled` (seller, before `sale_pending`), `expired` (no valid bid at the end), `unfulfilled` (no `Sale_Start` within 48 hours of the win; the seller can list again), or `ended` (`Sale_Cancel`, or the NFT moved or burned). Prices and dates freeze once a bid exists. Each listing gets a new purchase key.

A bid is checked as Core checks it at `Sale_Start`: the bidder's signature over `{purchase_key}_{amount × 1e8}_{address}`, verified here. Then: the listing is live and inside its dates, the bidder is not the seller or a vault (xRBX) address, the indexed balance covers the amount, and the amount is the buy-now price exactly (`is_buy_now`) or at least `next_min_bid` and below any buy-now price. Bid statuses: `winning` (the high bid, reserve met), `accepted` (the high bid, reserve not met), `outbid`, then `won` or `lost`.

Time and NFT ownership move listings on in two places: the `close_p2p_listings` command (Celery beat, every minute) and every P2P read, so a listing is right without the beat.

## Dry-run mode

`P2P_DRY_RUN` (environment, on by default in this branch) turns on the four `/p2p/dry-run/` endpoints; off, they answer 404. Each takes the unsigned description of the transaction the wallet would have signed, `{type, function, from_address, to_address, amount, fee, timestamp, data}` with `data` in Core's field names, checks it with Core's rules for that function, and applies what the indexer applies when the real transaction lands, marked `chain_state: "dry_run"`. The body may also be the description itself, without `tx`.

- DSTR (type 7, to `DecShop_Base`): `DecShopCreate()` costs at least 10 VFX, its owner is the sender, its URL and `UniqueId` are free, and `HostingType` is 2. `DecShopUpdate()` is the owner's, costs 1 VFX within 12 hours of the last change, and may change the URL (if free) and the owner (Core allows it; the old owner's open listings close). `DecShopDelete()` costs 1 VFX. Name at most 64 characters and description at most 1200 characters and 200 words, counted as Core counts them. A shop registered on chain cannot be changed by a dry run.
- `Sale_Start()` (type 5, seller to the winner, amount 0): `ContractUID`, `NextOwner`, `SoldFor`, `KeySign` and `BidSignature` are the listing's NFT, winner, winning amount, purchase key and winning bid's signature, which must verify.
- `Sale_Complete()` (buyer to seller, amount 0): the payments in `data.Transactions` come from the buyer, go to the seller (and, for a royalty contract, `Data` "2/2" to the royalty payee with `Royalty`, `RoyaltyAmount` and `RoyaltyPayTo` matching the contract), add up to at least the price less 1 VFX, and the buyer's indexed balance covers them and their fees. The NFT's indexed owner is not changed: that is chain data.
- `Sale_Cancel()` (seller): ends a `sale_pending` listing.

Every description's timestamp must be current in Unix seconds (Core refuses one over an hour old or two minutes ahead), its fee above 0, and its sender the token's address.

## Live indexing

Block sync sends every DSTR to `p2p.indexer.index_dstr` and every NFT_SALE (`Sale_Start()`, `M_Sale_Start()`, `Sale_Complete()`, `M_Sale_Complete()`, `Sale_Cancel()`) to `index_nft_sale`, which apply the same service functions with `chain_state` `registered` (shops) or `live` (sales). Neither raises: a transaction that cannot be applied is logged and skipped inside its own savepoint. Every transaction they apply, or look at and find nothing to do for, is recorded in `IndexedTransaction`, and a recorded one is skipped, so a block indexed again or a replay changes nothing. Without the record, the indexer still refuses to go backwards: a create or update older than the shop's deletion does not bring it back, a DSTR older than the shop's latest is skipped, an NFT_SALE older than a listing's last chain step does not rewind it, and a `Sale_Start` outside the registry ends only the listings of that NFT made before it. `python manage.py p2p_replay_dstr` replays the indexed DSTR transactions into the registry. DSTR still also writes the old shop table's row, without any crawl, so `/api/shop/` finds shops registered on chain.

## Tests

`python manage.py test p2p api.shop api.nft` against a local Postgres (see `CLAUDE.md`). To make the signature vectors again: `node p2p/tests/vectors/make_bid_vectors.cjs <vfx-web-sdk directory> > p2p/tests/vectors/bid_signatures.json`.
