[README.md](https://github.com/user-attachments/files/33836234/README.md)
# Webinar — Fraud: P2P Analysis

Lift an existing peer-to-peer fraud graph out of Neo4j, land it in a Microsoft
Fabric Lakehouse, and rebuild it as a fresh AuraDB instance through Fabric's
Neo4j integration.

- Handoff from the previous owner: [`docs/HANDOFF.md`](docs/HANDOFF.md)
- Plan, technical review and open questions:
  [`../docs/PROJECT_PLAN.md`](../docs/PROJECT_PLAN.md)

## The graph

| Label | Count | Properties |
|---|---:|---|
| `User` | 33,732 | `guid`, `firstChargebackMtDate`, `fraudMoneyTransfer`, `moneyTransferErrorCancelAmount` |
| `Device` | 51,451 | `guid`, `device`, `os` |
| `IP` | 585,855 | `guid` |
| `Card` | 118,818 | `guid`, `cardType`, `level` |

| Relationship | Pattern | Count | Properties |
|---|---|---:|---|
| `HAS_IP` | `(User)->(IP)` | 1,488,949 | `ipDate` |
| `HAS_CC` | `(User)->(Card)` | 128,066 | `cardDate` |
| `P2P` | `(User)->(User)` | 102,832 | `totalAmount`, `transactionDateTime` |
| `USED` | `(User)->(Device)` | 55,026 | `deviceDate` |
| `REFERRED` | `(User)->(User)` | 1,870 | — |

~790K nodes, ~1.78M relationships.

Shape worth noting for the demo: `IP` is 74% of all nodes and carries no
property but `guid`. It is a pure join anchor — its whole job is to make two
users who share an address one hop apart. `Card` and `Device` do the same.
The fraud signal lives in the `User`–`User` edges (`P2P`, `REFERRED`); the
identifier nodes are what turn isolated transactions into detectable rings.

## Layout

```
FraudP2P/
├── docs/HANDOFF.md      # inherited handoff (converted from RTF)
├── scripts/             # export, transform, validate
├── fabric/              # 01_bronze → 02_silver → 03_gold notebooks
├── neo4j/               # constraints, load, validate, demo Cypher
└── data/export/         # CSV output — gitignored
```

## Setup

```bash
cp ../shared/.env.example .env      # then fill in — .env is gitignored
pip install neo4j python-dotenv
```

`.env` needs both the **source** instance (export from) and the **target**
AuraDB (rebuild into). See comments in `shared/.env.example`.

## The analysis — already published

[`aura-graph-analytics/fraud_p2p`](https://github.com/neo4j-product-examples/aura-graph-analytics/tree/main/fraud_p2p)
uses this exact schema and supplies the whole demo:

1. `MATCH (u:User) WHERE u.fraudMoneyTransfer=1 SET u:FlaggedUser`
2. Derive `P2P_WITH_SHARED_CARD` — users who transacted *and* share a card.
3. Derive `SHARED_IDS` — users sharing more than two identifiers, excluding hub
   nodes via `COUNT { (n)<--() } <= 10`. That degree filter is the interesting
   modelling choice: a shared coffee-shop IP is noise, a shared card is signal.
4. Project `User` plus the two derived types (`OPTIONAL MATCH` keeps singletons).
5. Run **WCC**; label every user in a community containing a flagged user.

Why it makes a good webinar:

- **211 new fraud-risk accounts** — an 87.5% increase over the 241 already known.
- Those 211 are **under 1% of users** but touch **12.7% of total P2P volume**.
- **65%** of money moving between flagged accounts and everyone else is
  attributable to them.

That repo also ships a **dump file** in `raw_data/`, so a working source instance
can be stood up without waiting for credentials to the original.

## The closing beat

Ask Copilot *"which accounts belong to the same fraud ring as one of our flagged
users?"* **before** the WCC results are written back. It can join one hop —
accounts directly sharing a card with a flagged user — but not the transitive
closure. So the answer comes back **incomplete and looks complete**, silently
missing every 2- and 3-hop ring member. A fraud analyst would act on it.

Run the pipeline, write `fraud_ring_id` and `is_fraud_risk` back to OneLake, ask
the identical question: all 452 accounts, including the 211 nobody had flagged.

Full setup and live-demo risks in
[`../docs/COPILOT_BEAT.md`](../docs/COPILOT_BEAT.md). One thing that matters more
than it looks: **write the columns back with business names**, not `wccId`.
Copilot reads column names, and that rename is usually the difference between the
beat landing and falling flat.

## Status

Scaffold only. Blocked on credentials for the original source, though the public
dump routes around that — see step 2 of the project plan.

Three things to settle before a full run (§4 of the plan):

1. The handoff's `SKIP`/`LIMIT` pagination degrades quadratically over 1.5M rows.
   Switch to keyset pagination.
2. `guid` is a **string**, and Fabric's import tool requires integer property
   keys. Assign integer surrogate keys in the Fabric silver layer and keep `guid`
   as an ordinary property.
3. `id()` is deprecated and removed under Cypher 25 — used both by the handoff's
   pagination and by the `fraud_p2p` analysis queries.
