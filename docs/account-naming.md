# Account & subaccount naming — perp_bot / hb-enhanced-opms / dex_executor

How account/subaccount identity is named and threaded through the stack, and
how to configure `.env` consistently for Hyperliquid (HL), Aster, and
Lighter. Written against the code as it exists today (`account_registry.py`,
`venue_capabilities.py`, `topology.py`, `PerpMMControllerConfig`) — grep
before trusting any of this after a refactor.

## Two separate naming layers — don't conflate them

1. **`exchange` / `venue`** — the venue key: `hyperliquid`, `aster`,
   `lighter`. Selects `venue_capabilities` (position mode: net vs hedge),
   tags `ExecIntent.venue` / `PnLLedger.venue`, and is the `{EXCHANGE}`
   prefix in `.env` var names.
2. **`account_id`** — a label distinguishing *multiple credential sets on
   the same venue* (e.g. two HL subaccounts). It is **not itself a secret
   and does not select which wallet trades** — see below, that's
   architecture-dependent.

Both together, `(exchange, account_id)`, are the uniqueness key
`perp_bot.topology.validate_account_topology` checks against `coin` to
reject unsafe duplicate keepers on one netted-mode subaccount.

## Which layer actually authenticates — this matters, two live paths exist

The repo has **two different credential-resolution paths** and they are not
interchangeable:

- **`dex_executor`'s `AccountRegistry`** (`opms/service/account_registry.py`)
  reads credentials from `os.environ` live, per request, keyed by
  `{EXCHANGE}_{ACCOUNT_ID}_{CREDENTIAL_TYPE}`. This is the account model
  `dex_executor/README.md` and `perp-bot/README.md` describe. Per
  [`hl-market-neutral-mm-deployment-plan.md`](hl-market-neutral-mm-deployment-plan.md),
  this path is **currently bypassed** for live HL trading — `dex_executor`'s
  `/api/v1/intents` surface is incomplete and its HL adapter has no
  leverage/margin or post-only support.
- **`hb-enhanced-opms` + Hummingbot's own connector** is the path actually
  driving live trading today. Here, the wallet credentials live in
  **Hummingbot's encrypted `conf/connectors/` store**, one credential set
  per **`connector_name`** (e.g. `hyperliquid_perpetual_testnet`), imported
  once via `hb-enhanced-opms/scripts/import_hl_testnet_credentials.py` (or
  HB's interactive `connect` command) — not read from `.env` at runtime.
  `PerpMMControllerConfig.connector_name` is what actually selects the
  wallet; `PerpMMControllerConfig.venue` and `.account_id` are routing
  labels only (see the comment at
  `hb-enhanced-opms/src/opms/controllers/generic/perp_mm_controller.py:42-46`
  — `connector_name` and `venue` are deliberately different namespaces,
  e.g. `connector_name="hyperliquid_perpetual_testnet"` while
  `venue="hyperliquid"`).

**Consequence**: in the live architecture, `account_id` doesn't pick a
wallet by itself — the running controller instance's `connector_name`
does. `account_id` still has to be set correctly, though, because it's what
keeps decision logs, `validate_account_topology`, and the OPMS fills
websocket (`/ws/fills/{exchange}?account_id=...`) scoped to the right
subaccount — get it wrong and two subaccounts' fills/logs get mixed up even
though the trades themselves still go to the right wallet.

## Account vs subaccount, per venue

| Venue | Position mode | Same-coin long+short on one account? | Structure needed for a hedged pair |
|---|---|---|---|
| Hyperliquid | net (`venue_capabilities.py`) | No — one signed position per coin per subaccount | 2 subaccounts minimum: one tilted long, one tilted short (`perp_bot.topology` rejects duplicates otherwise) |
| Aster | hedge (confirmed via Aster docs) | Yes — native hedge mode | 1 account per coin, 2 tilted keeper instances sharing that account_id — no subaccount pairing needed |
| Lighter | net (corrected — was misclassified as `hedge` in code; see deployment plan §1.7) | No — same single-net-position constraint as HL | Same 2-subaccount structure as HL |

Full rationale and the cross-hedge basket design (pairing two correlated
coins across two subaccounts to get per-account market neutrality) is in
[`hl-market-neutral-mm-deployment-plan.md` §1](hl-market-neutral-mm-deployment-plan.md#1-subaccount-topology--cross-hedge-basket-design)
— this doc only covers the naming/config mechanics.

`position_mode` on `PerpPairConfig` is inferred from `venue_capabilities` if
left unset; if you pass it explicitly it must match, or `PerpPairConfig.__post_init__`
raises.

## `.env` naming convention: `{EXCHANGE}_{ACCOUNT_ID}_{CREDENTIAL_TYPE}`

From `AccountRegistry.EXCHANGE_CREDENTIAL_TYPES`
(`dex_executor/src/opms/service/account_registry.py:41-49`) — this is the
canonical credential shape whether or not the live path currently reads it
directly (the HB-import script's one-off env vars follow the same shape):

| Venue | Required suffixes | Notes |
|---|---|---|
| `HYPERLIQUID` | `_PRIVATE_KEY`, `_ACCOUNT_ADDRESS`, `_IS_TESTNET` | Wallet-based — see subaccount mechanics below. |
| `ASTER` | `_API_KEY`, `_API_SECRET`, `_IS_TESTNET` | CEX-style key/secret, not wallet-based. |
| `LIGHTER` | `_PRIVATE_KEY`, `_IS_TESTNET` | No separate address var in the current registry — one signing key per account. |

### How HL subaccount authentication actually works (verified against HL docs)

Easy to get backwards, so spelled out explicitly. Per
[Hyperliquid's exchange-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint):

> Subaccounts and vaults do not have private keys. To perform actions on
> behalf of a subaccount or vault, signing should be done by the master
> account and the `vaultAddress` field should be set to the address of the
> subaccount or vault.

- A subaccount **has its own onchain address** (returned by the
  `createSubAccount` action, which itself only takes a `name` — a display
  label used nowhere else) but **no private key of its own**.
- **The address, not the private key, selects which account an action
  applies to.** Every exchange-endpoint request that should hit a subaccount
  sets `vaultAddress` to *that subaccount's* address — never the master's.
- **No subaccount name is ever passed to trading calls** — `name` only
  exists at `createSubAccount` creation time for the UI; every later
  reference (orders, positions, `vaultAddress`) is address-only.
- The **signing key** is either the master account's own private key, or an
  **agent/API wallet** (HL's "API page": a generated address+private key
  under a human-readable name, with no withdraw rights). Confirmed against
  the app: one approved agent wallet is attached to the master **and all its
  subaccounts** — it's not created per-subaccount, `vaultAddress` on each
  request is what scopes a given call to the master or to one subaccount.

**Use an agent-wallet key for `_PRIVATE_KEY`, never the master account's own
key.** The master's own key isn't something you want in a bot's `.env` at
all (full withdraw rights over master + every subaccount); the agent wallet
exists specifically so a leaked bot credential can't drain funds. So:
`{EXCHANGE}_{ACCOUNT_ID}_ACCOUNT_ADDRESS` = that account's own address (from
the Subaccount page — becomes `vaultAddress`, or is the master's own address
for a MAIN entry), and `_PRIVATE_KEY` = an agent wallet's key (from the API
page) approved by the master.

**Nonce sharing still applies to agent keys, not just the master key.**
HL tracks nonces per signer address — the *same* agent wallet used by two
keepers trading concurrently (e.g. a MAIN keeper and a subaccount keeper
both live at once) share one nonce tracker regardless of the different
`vaultAddress` each targets. If two account_ids in your `.env` will trade
*at the same time*, give them separate named agent wallets rather than
reusing one key across both — reuse is fine only for accounts that never
trade concurrently.

⚠ Hummingbot's `hyperliquid_perpetual` connector had real bugs around this —
[issue #6805](https://github.com/hummingbot/hummingbot/issues/6805)
(connector silently defaulted to the main account even with a vault address
configured) and the multi-subaccount feature request
[#7324](https://github.com/hummingbot/hummingbot/issues/7324) — both closed
now, but confirm your installed Hummingbot version actually has those fixes
before trusting `vaultAddress`-based subaccount routing through
`hb-enhanced-opms` with real capital.

`ACCOUNT_ID` is your choice — pick a short slug (`main`, `test`, `sub1`,
`basket_a`) and use it uppercased in `.env`, lowercased when passed as
`account_id=` in code (`AccountRegistry` uppercases/lowercases automatically;
`perp_bot` does not transform it, so keep casing consistent yourself there).

### Example — matches this repo's current shape (values redacted)

```bash
# Hyperliquid — two subaccounts for a cross-hedge basket pair
HYPERLIQUID_MAIN_PRIVATE_KEY=0x...
HYPERLIQUID_MAIN_ACCOUNT_ADDRESS=0x...
HYPERLIQUID_MAIN_IS_TESTNET=false

HYPERLIQUID_SUB1_PRIVATE_KEY=0x...
HYPERLIQUID_SUB1_ACCOUNT_ADDRESS=0x...
HYPERLIQUID_SUB1_IS_TESTNET=false

HYPERLIQUID_SUB2_PRIVATE_KEY=0x...
HYPERLIQUID_SUB2_ACCOUNT_ADDRESS=0x...
HYPERLIQUID_SUB2_IS_TESTNET=false

# Aster — one account per coin, hedge mode handles both tilts
ASTER_MAIN_API_KEY=...
ASTER_MAIN_API_SECRET=...
ASTER_MAIN_IS_TESTNET=false

# Lighter — same 2-subaccount shape as Hyperliquid
LIGHTER_SUB1_PRIVATE_KEY=0x...
LIGHTER_SUB1_IS_TESTNET=false
LIGHTER_SUB2_PRIVATE_KEY=0x...
LIGHTER_SUB2_IS_TESTNET=false
```

### ⚠ Found while writing this doc: malformed var in the repo-root `.env`

`AccountRegistry.get_account("hyperliquid", "E2_MAIN")` builds the prefix
`HYPERLIQUID_E2_MAIN_` and looks for `HYPERLIQUID_E2_MAIN_ACCOUNT_ADDRESS`.
The repo-root `.env` currently has:

```
HYPERLIQUID_E2_MAIN_IS_TESTNET_MAIN_ACCOUNT_ADDRESS=...
```

— a duplicated `MAIN` segment with `IS_TESTNET` spliced in, so it doesn't
match the pattern the registry looks for and `get_account` would raise
`Missing required environment variable: HYPERLIQUID_E2_MAIN_ACCOUNT_ADDRESS`
if that path is ever hit for this account. Rename it to
`HYPERLIQUID_E2_MAIN_ACCOUNT_ADDRESS` to match `E2_SUB1`/`E2_SUB2` right
below it, which are correctly formed. (This doesn't break the live
HB-connector path today since that path doesn't read this var — but fix it
before anything exercises the `AccountRegistry`/`dex_executor` path again.)

## Wiring it through the two config objects

**`perp_bot.config.PerpPairConfig`** (the venue-agnostic keeper config):

```python
PerpPairConfig(
    coin="ETH",
    exchange="hyperliquid",   # -> venue_capabilities lookup + {EXCHANGE} prefix
    account_id="sub1",        # -> {ACCOUNT_ID} in .env, topology key, fills-ws scope
    ...
)
```

**`hb-enhanced-opms`'s `PerpMMControllerConfig`** (the live HB controller,
one instance per subaccount/coin):

```yaml
connector_name: hyperliquid_perpetual_testnet   # selects the actual wallet
                                                  # (credentials imported into
                                                  # HB's encrypted conf store)
venue: hyperliquid                               # perp_bot venue key — must
                                                  # match exchange above
account_id: sub1                                 # must match the .env
                                                  # ACCOUNT_ID for this wallet,
                                                  # even though this field
                                                  # doesn't itself authenticate
trading_pair: ETH-USD
```

Keep `venue`/`account_id` here equal to the `exchange`/`account_id` you'd
pass to a bare `PerpPairConfig` for the same subaccount — the controller
builds one internally from these fields
(`perp_mm_controller.py:68-76`), and mismatched labels are what make
`validate_account_topology`, decision logs, and fills-websocket scoping
silently point at the wrong subaccount even though `connector_name` still
trades the right wallet.

## Checklist: adding a new subaccount

1. Pick a slug (`sub3`, `basket_c`, ...). Uppercase for `.env`, same
   casing verbatim for `account_id=` in `PerpPairConfig`/`PerpMMControllerConfig`.
2. Add the venue's required `.env` vars (`PRIVATE_KEY`+`ACCOUNT_ADDRESS` for
   HL/Lighter, `API_KEY`+`API_SECRET` for Aster, `IS_TESTNET` for all) —
   double check the var name against the table above, not by copy-pasting
   and editing (see the malformed-var example).
3. HL/Lighter (net mode): one `account_id` per `(coin)` you deploy — never
   two keepers on the same `(exchange, coin, account_id)`,
   `validate_account_topology` will reject it.
4. Aster (hedge mode): reuse one `account_id` per coin for both the
   long-tilt and short-tilt keeper instances.
5. If deploying live via `hb-enhanced-opms`: import the credentials into
   Hummingbot's encrypted store (`import_hl_testnet_credentials.py` or
   `connect <connector_name>`), give the controller instance a
   `connector_name` unique to that subaccount, and set `venue`/`account_id`
   to match what you used in step 1.
6. Run `perp_bot.validate_account_topology(...)` over the full set of
   configs you're about to launch before starting any of them.
