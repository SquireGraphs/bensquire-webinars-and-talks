# Fraud ring detection in Microsoft Fabric with Neo4j

Find fraud rings in a peer-to-peer (P2P) payments network using Neo4j graph algorithms, then write
the results back to OneLake, where a Fabric data agent (Copilot) can use them to answer questions.

The repo contains two notebooks:

| | Notebook | What it does |
|---|---|---|
| 1 | `01_generate_p2p_synthetic.ipynb` | Generates a synthetic P2P network as Delta tables in your lakehouse. |
| 2 | `02_fraud_ring_analysis.ipynb` | Runs graph analytics on that network in Neo4j and writes a fraud worklist back to OneLake. |

No real customer data is included. The generator produces data with the same shape as an
anonymized real-world P2P dataset: about 34,000 accounts, 0.7% of them confirmed fraud, and fraud
rings hidden among ordinary households.

---

## Why a graph?

A confirmed fraud account rarely acts alone. Other accounts in the same ring share its cards, phones
and payments, and none of them is flagged yet. Finding them is a connection problem:

- **One hop is noisy.** Nearly every account shares *something* with a fraud account. A busy IP
  address (a coffee shop, a mobile carrier, a VPN) links thousands of unrelated people.
- **Strong links need a threshold.** Two accounts that share three or more identifiers, anchored on
  a card or phone that few other accounts use, are probably the same person.
- **Rings are transitive.** If A is linked to B and B to C, all three are in one group. SQL needs a
  recursive query for that; a graph algorithm does it in one call.

The analysis notebook applies those rules in Neo4j, groups accounts with **Weakly Connected
Components (WCC)**, and flags every group that contains a confirmed fraud account.

---

## Architecture

```
 01_generate_p2p_synthetic         Neo4j workload in Fabric           02_fraud_ring_analysis
┌─────────────────────────┐      ┌──────────────────────────┐      ┌─────────────────────────────┐
│ Lakehouse               │      │ Neo4j AuraDB             │      │ Aura Graph Analytics session│
│ p2p_synthetic.*         │ ───► │ (:User)-[:HAS_CC]->(:Card)│ ───► │ rules → projection → WCC    │
│ 9 Delta tables          │import│ ... 4 labels, 5 rel types │      │                             │
└─────────────────────────┘      └──────────────────────────┘      └──────────────┬──────────────┘
                                                                                  │ write back
                                                                                  ▼
                                                                   ┌─────────────────────────────┐
                                                                   │ Lakehouse dbo.*             │
                                                                   │ fraud_accounts              │
                                                                   │ fraud_groups                │──► Fabric data agent
                                                                   │ group_size_summary          │
                                                                   └─────────────────────────────┘
```

---

## Prerequisites

- **A Microsoft Fabric workspace** with Spark compute (any F SKU or a trial capacity).
- **A lakehouse with schemas enabled.** Attach it to both notebooks and set it as the **default**
  lakehouse (Explorer → … → *Set as default*). Attaching alone isn't enough.
- **The Neo4j workload in Fabric**, to import lakehouse tables into AuraDB. You can load the data
  into AuraDB another way if you prefer.
- **Neo4j Aura API credentials** (client ID, client secret, tenant ID) for Aura Graph Analytics
  sessions. Create them in the Aura console under *Account → API keys*.
- **`graphdatascience==1.22`.** The analysis notebook pins this version. Version 2.x renamed several
  calls (`gds.graph.project`, `gds.graph.exists`, camelCase arguments), so an unpinned install
  breaks the notebook.

---

## Quick start

### 1. Generate the data

Open `01_generate_p2p_synthetic.ipynb` in Fabric, attach your lakehouse as default, and **Run all**.
It takes a few minutes on a small capacity.

The notebook writes nine tables to a new `p2p_synthetic` schema:

| Table | Rows (approx.) | Contents |
|---|---:|---|
| `user` | 34K | `guid`, `fraudMoneyTransfer` (1 = confirmed fraud), `firstChargebackMtDate`, `moneyTransferErrorCancelAmount` |
| `card` | 123K | `guid`, `cardType`, `level` |
| `device` | 52K | `guid`, `device`, `os` |
| `ip` | ~575K | `guid` |
| `cc_lookup` | 132K | user → card, with `cardDate` |
| `used` | 55K | user → device, with `deviceDate` |
| `has_ip` | 1.4M | user → IP, with `ipDate` |
| `p2p` | 101K | user → user payments, with `totalAmount` and `transactionDateTime` |
| `referred` | 1.9K | user → user referrals |

Every relationship table has `start_guid` and `end_guid` columns, which join to the `guid` columns
of the node tables.

**Deterministic output.** All random draws are hashed from a seed, so the same seed always produces
identical tables. Change `seed` in the parameters cell for a different dataset with the same
statistical shape.

The validation cells at the end print row counts, compare column types against an existing
`p2p_network` schema if you have one, and show the one-hop numbers described under
[Expected results](#expected-results).

`sql/generate_p2p_synthetic.sql` contains the same statements as a single Spark SQL script, and
`sql/validate_p2p_synthetic.sql` holds the validation queries.

### 2. Import into Neo4j

Use the Neo4j workload in Fabric to create a graph from the nine `p2p_synthetic` tables. The
analysis notebook expects **exactly** this model:

| Source table | In the graph |
|---|---|
| `user` | `(:User)` |
| `card` | `(:Card)` |
| `device` | `(:Device)` |
| `ip` | `(:IP)` |
| `cc_lookup` | `(:User)-[:HAS_CC]->(:Card)` |
| `used` | `(:User)-[:USED]->(:Device)` |
| `has_ip` | `(:User)-[:HAS_IP]->(:IP)` |
| `p2p` | `(:User)-[:P2P]->(:User)` |
| `referred` | `(:User)-[:REFERRED]->(:User)` |

> **Check the proposed model before you transform.** The import suggests names automatically, and it
> may name relationships after their source tables, for example `CC_LOOKUP` instead of `HAS_CC`.
> Rename them in the model editor to match the table above. Labels and relationship types are
> case-sensitive. If they don't match, the analysis queries return zero rows without raising an
> error.

Save the AuraDB URI, username and password that the import shows you. They are displayed only once.

After the import, run this in Neo4j to confirm the model:

```cypher
CALL db.schema.visualization()
```

### 3. Store your credentials

The analysis notebook reads six values:

```
NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD   # the AuraDB instance from step 2
CLIENT_ID, CLIENT_SECRET, TENANT_ID         # Aura API credentials
```

For a quick test, the notebook loads them from `/lakehouse/default/Files/secrets.json`. **Don't do
this beyond a sandbox.** Anyone with Viewer access to the workspace can read lakehouse files. Use
Azure Key Vault instead:

```python
KV = "https://<your-vault>.vault.azure.net/"
NEO4J_PASSWORD = notebookutils.credentials.getSecret(KV, "neo4j-password")
CLIENT_SECRET  = notebookutils.credentials.getSecret(KV, "aura-client-secret")
```

The account running the notebook needs the **Key Vault Secrets User** role on the vault. For
scheduled runs, that's the pipeline owner or service principal, not you.

### 4. Run the analysis

Open `02_fraud_ring_analysis.ipynb`, attach the same lakehouse as default, and run the cells in
order:

1. **Start an Aura Graph Analytics session.** The session is named after the AuraDB instance, so
   pointing the notebook at a new database won't conflict with an old session.
2. **Explore.** Count node and relationship types and the confirmed fraud accounts.
3. **Label confirmed fraud** as `:FlaggedUser`.
4. **Derive strong links** between accounts:
   - `P2P_WITH_SHARED_CARD`: two accounts that paid each other *and* share a card.
   - `SHARED_IDS`: two accounts that share a card or phone used by 10 accounts or fewer, and share
     more than two identifiers in total (cards, phones, IPs).
5. **Project and run WCC** over those two relationship types.
6. **Flag every group** that contains a confirmed fraud account. Its members get the
   `:FraudRiskUser` label and `fraudRisk = 1`.
7. **Write three tables** to `dbo` in the lakehouse:

| Table | One row per | Use it for |
|---|---|---|
| `fraud_accounts` | account in a flagged group | the investigator worklist: status, group, shared identifiers with fraud |
| `fraud_groups` | flagged group | group size, confirmed vs newly associated, sample members |
| `group_size_summary` | group-size band | the overall distribution |

The last cell deletes the session. **Run it.** Sessions are billed until they're deleted or their
time-to-live expires.

### 5. Ask a data agent (optional)

Create a Fabric data agent over the lakehouse and ask the same question twice, once with the source
tables only and once with the graph results:

```
How many user accounts with fraudMoneyTransfer = 0 share a device, p2p payment, or card with an account where fraudMoneyTransfer = 1?
```

```
How many user accounts with fraudMoneyTransfer = 0 share a device, p2p payment, or card with an account where fraudMoneyTransfer = 1? Use the fraud_accounts table.
```

The first answer counts every one-hop connection. The second counts only accounts in a resolved
fraud ring.

Questions about groups ("what is the largest group of connected accounts?") can't be answered from
the source tables. They need recursive traversal, which the SQL endpoint doesn't support, and the
agent says so. With `fraud_groups` available, it answers directly.

---

## Expected results

These figures come from the default seed (`20261006`), scored with the same rules the analysis
notebook applies. Treat them as approximate. Other seeds land in similar ranges.

| | Default seed |
|---|---:|
| Accounts | 33,836 |
| Confirmed fraud (`fraudMoneyTransfer = 1`) | 231 |
| Non-fraud accounts sharing **any IP** with a fraud account | 32,677 (97%) |
| Non-fraud accounts sharing a **card, device or P2P payment** with a fraud account | 1,614 |
| `SHARED_IDS` relationships | ~5,650 |
| `P2P_WITH_SHARED_CARD` relationships | ~5,740 |
| Accounts in a flagged group | 460 |
| Newly identified (flagged, not previously confirmed) | 229 |
| Largest flagged group | 35 |
| Largest group overall (no fraud) | 152 |

The markdown commentary in the analysis notebook quotes figures from the original real-world
dataset (241 confirmed, 452 flagged, 211 new). Your run on synthetic data shows the numbers in this
table instead.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A rule query returns `0` | The import named a relationship differently, for example `CC_LOOKUP` instead of `HAS_CC`. Run `CALL db.schema.visualization()` and rename in the import model, or update the queries. |
| `{fraudMoneyTransfer: 1}` matches nothing | The import stored the property as text. Use `"1"`, or set the property type in the import model. |
| `'ProjectArrowEndpoints' object is not callable` | `graphdatascience` 2.x is installed. Install `graphdatascience==1.22` and restart the session. |
| `AuraApiError 409 ... different parameters` | A session with the same name is bound to another database. Delete it in the Aura console (*Graph Analytics*), or let the session cell create a new name. |
| `TEMP_TABLE_OR_VIEW_ALREADY_EXISTS` | Fixed in this version: every helper view is dropped before it's rebuilt. Re-import the notebook if you see it. |
| Tables land in the wrong lakehouse | The notebook writes to the **default** lakehouse. Set yours as default and restart the session. |
| The data agent can't see the output tables | Tables are in `dbo`. Add them to the agent's data source explicitly, since new tables aren't picked up automatically. |
| `underlying location does not exist` from the SQL endpoint | The SQL analytics endpoint metadata is stale after an overwrite. Use **Refresh** in the SQL endpoint view and wait. If it persists, write to new table names. |
| `id is deprecated` warning | Harmless. The rules in this version compare on `guid` instead. |

---

## How the synthetic data is built

The generator creates *households*, latent groups of accounts that identity resolution should be
able to recover:

- **Single accounts** make up about 72% of users. Some are lone fraudsters.
- **Pairs, triples and small groups** share a card or phone, a few home IP addresses, and usually
  P2P payments. A small fraction contain a confirmed fraud account.
- **Three fraud rings** of 12–42 accounts are seeded in, including one large ring that holds the
  headline result steady across seeds.
- **One large benign cluster** of about 150 accounts contains no fraud. The analysis should *not*
  flag it.

Noise is added on top so the problem is realistic:

- **Hub IP addresses** shared by hundreds to thousands of accounts, which are the main source of
  false positives.
- **Kiosk devices** used by 11–52 accounts.
- **Unrelated pairs** that happen to share one card.
- A **"popular" minority** that receives payments from many accounts.

Fraud accounts differ in measurable ways: more cards (card testing), larger transfers and higher
cancellation amounts. Nearly every confirmed fraud account has a real chargeback date (99%, as in the source data). Everyone
else has the placeholder `1900-01-01`, the legacy "never happened" value from the source system. That
placeholder is kept on purpose, because an agent asked to "find fraud" will often count it as a
real date.

---

## Credits

The analysis follows the P2P fraud example in
[neo4j-product-examples/aura-graph-analytics](https://github.com/neo4j-product-examples/aura-graph-analytics/tree/main/fraud_p2p),
adapted to run in Microsoft Fabric and write its results to OneLake.
