-- =============================================================================
-- Synthetic P2P fraud network for Microsoft Fabric (Spark SQL)
--
-- Writes the same 9 tables and columns as p2p_network into p2p_synthetic.
-- Deterministic: every random draw is a hash of (seed, tag, key), so the same
-- seed always produces the same data, whatever the cluster or partitioning.
-- Change `seed` in syn_params to get a different dataset with the same shape.
--
-- Run statements in order in one Spark session (temp views are session-scoped).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS p2p_synthetic;

-- -----------------------------------------------------------------------------
-- 1. Parameters
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_params;
CACHE TABLE syn_params AS SELECT
    20261006           AS seed,
    DATE'1900-01-01'   AS no_chargeback,   -- legacy "never happened" sentinel, as in the source
    61000              AS ip_local_pool,    -- IPs shared by a handful of accounts
    6200               AS ip_mid_pool,      -- IPs shared by tens of accounts
    1687               AS ip_hub_pool,      -- carrier NAT / VPN / campus IPs shared by hundreds+
    1.95D              AS ip_hub_skew,
    2200               AS weak_card_pairs,  -- unrelated pairs that happen to share one card
    700                AS weak_device_pairs,
    10                 AS kiosk_devices     -- devices used by 11-52 accounts
;

-- -----------------------------------------------------------------------------
-- 2. Households: the latent groups that identity resolution should recover
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_household_types;
CACHE TABLE syn_household_types AS SELECT * FROM VALUES
    ('single', 24300,   1,   1, 1.0D, 0, 0, 0.00D, 0.0052D, 0.00D),
    ('pair',    3200,   2,   2, 1.0D, 2, 7, 0.75D, 0.0225D, 0.08D),
    ('triple',   480,   3,   3, 1.0D, 2, 7, 0.75D, 0.0440D, 0.10D),
    ('small',    255,   4,  10, 4.0D, 2, 8, 0.70D, 0.0450D, 0.02D),
    ('ring',      12,  11,  30, 3.0D, 3, 9, 0.85D, 0.0000D, 0.00D),
    ('fraud_ring', 2,  12,  25, 1.5D, 3, 9, 0.90D, 1.0000D, 0.06D),
    ('fraud_ring_large', 1, 30, 42, 1.0D, 3, 9, 0.90D, 1.0000D, 0.06D),
    ('mega',       1, 150, 190, 2.2D, 4, 6, 0.60D, 0.0000D, 0.00D)
AS t(kind, n_households, size_lo, size_hi, size_skew, home_ip_lo, home_ip_hi,
     p2p_prob, fraud_house_prob, extra_fraud_prob);

DROP VIEW IF EXISTS syn_households;
CACHE TABLE syn_households AS
WITH h AS (
    SELECT t.*, explode(sequence(1, t.n_households)) AS k
    FROM syn_household_types t
),
r AS (
    SELECT h.*,
        (pmod(xxhash64(p.seed, 'h_size',  kind, k), 2147483629) + 1) / 2147483630D AS u1,
        (pmod(xxhash64(p.seed, 'h_ips',   kind, k), 2147483629) + 1) / 2147483630D AS u2,
        (pmod(xxhash64(p.seed, 'h_fraud', kind, k), 2147483629) + 1) / 2147483630D AS u3
    FROM h CROSS JOIN syn_params p
)
SELECT
    row_number() OVER (ORDER BY kind, k)                                      AS hid,
    kind,
    CAST(size_lo + floor((size_hi - size_lo + 1) * pow(u1, size_skew)) AS INT) AS size,
    CAST(home_ip_lo + floor((home_ip_hi - home_ip_lo + 1) * u2) AS INT)       AS home_ips,
    p2p_prob,
    u3 < fraud_house_prob                                                     AS fraud_house,
    extra_fraud_prob
FROM r;

-- -----------------------------------------------------------------------------
-- 3. Users
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_users;
CACHE TABLE syn_users AS
WITH m AS (
    SELECT h.*, explode(sequence(0, h.size - 1)) AS j FROM syn_households h
),
r AS (
    SELECT m.*, p.seed,
        (pmod(xxhash64(p.seed, 'u_fraud', hid, j), 2147483629) + 1) / 2147483630D AS uf
    FROM m CROSS JOIN syn_params p
)
SELECT
    row_number() OVER (ORDER BY hid, j)                                       AS uid,
    hid, j, kind, size, home_ips, p2p_prob,
    CASE WHEN fraud_house AND (j = 0 OR uf < extra_fraud_prob) THEN 1 ELSE 0 END AS fraud,
    md5(concat('U', seed, '-', hid, '-', j))                                  AS guid
FROM r;

DROP VIEW IF EXISTS syn_users_ranked;
CACHE TABLE syn_users_ranked AS
SELECT u.*,
    row_number() OVER (ORDER BY xxhash64(p.seed, 'pop', u.uid)) AS pop_rank,
    row_number() OVER (ORDER BY xxhash64(p.seed, 'ref', u.uid)) AS ref_rank,
    row_number() OVER (ORDER BY xxhash64(p.seed, 'match', u.uid)) AS match_rank,
    count(*) OVER ()                                            AS n_users
FROM syn_users u CROSS JOIN syn_params p;

-- -----------------------------------------------------------------------------
-- 4. Household links: a random tree inside each household
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_hh_links;
CACHE TABLE syn_hh_links AS
WITH child AS (
    SELECT u.*,
        CAST(floor(j * (pmod(xxhash64(p.seed, 'parent', uid), 2147483629) + 1) / 2147483630D) AS INT) AS parent_j,
        (pmod(xxhash64(p.seed, 'l_card',  uid), 2147483629) + 1) / 2147483630D AS uc,
        (pmod(xxhash64(p.seed, 'l_card2', uid), 2147483629) + 1) / 2147483630D AS uc2,
        (pmod(xxhash64(p.seed, 'l_dev',   uid), 2147483629) + 1) / 2147483630D AS ud,
        (pmod(xxhash64(p.seed, 'l_p2p',   uid), 2147483629) + 1) / 2147483630D AS up
    FROM syn_users u CROSS JOIN syn_params p
    WHERE u.j > 0
),
l AS (
    SELECT
        par.uid AS ua, c.uid AS ub, c.hid, c.kind, c.p2p_prob, c.up, c.ud,
        CASE WHEN c.uc < 0.86 THEN
                 1 + CASE WHEN c.uc2 < 0.30 THEN 1 + CAST(floor(-ln(c.uc2 / 0.30) * 0.8) AS INT) ELSE 0 END
             ELSE 0 END AS shared_cards
    FROM child c
    JOIN syn_users par ON par.hid = c.hid AND par.j = c.parent_j
)
SELECT ua, ub, hid, kind, shared_cards,
       (ud < 0.40 OR shared_cards = 0) AS shared_device,
       up < p2p_prob                    AS transacts
FROM l;

-- -----------------------------------------------------------------------------
-- 5. Card links (User)-[:HAS_CC]->(Card)
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_card_links;
CACHE TABLE syn_card_links AS
WITH per_user AS (
    SELECT u.uid, u.fraud,
        (pmod(xxhash64(p.seed, 'c_n0', uid), 2147483629) + 1) / 2147483630D AS u0,
        (pmod(xxhash64(p.seed, 'c_n1', uid), 2147483629) + 1) / 2147483630D AS u1
    FROM syn_users u CROSS JOIN syn_params p
),
private_n AS (
    SELECT uid,
        CASE WHEN fraud = 1 THEN LEAST(450, GREATEST(1, CAST(floor(3.5 * pow(u1, -0.7)) AS INT)))
             WHEN u0 < 0.08 THEN 0
             WHEN u0 > 0.996 THEN LEAST(150, CAST(floor(20 * pow(u1, -0.6)) AS INT))
             ELSE 1 + CAST(floor(-ln(u1) * 2.9) AS INT) END AS n
    FROM per_user
),
private_cards AS (
    SELECT uid, concat('p:', uid, ':', i) AS card_key
    FROM (SELECT uid, explode(sequence(1, n)) AS i FROM private_n WHERE n > 0)
),
household_cards AS (
    SELECT stack(2, ua, ub) AS uid, concat('h:', ua, ':', ub, ':', i) AS card_key
    FROM (SELECT ua, ub, explode(sequence(1, shared_cards)) AS i FROM syn_hh_links WHERE shared_cards > 0)
),
group_card_sizes AS (
    SELECT h.hid,
        LEAST(h.size, 3 + CAST(floor(8 * (pmod(xxhash64(p.seed, 'g_csz', h.hid), 2147483629) + 1) / 2147483630D) AS INT)) AS members
    FROM syn_households h CROSS JOIN syn_params p
    WHERE h.size >= 3
      AND (pmod(xxhash64(p.seed, 'g_card', h.hid), 2147483629) + 1) / 2147483630D < 0.15
),
group_cards AS (
    SELECT u.uid, concat('g:', g.hid) AS card_key
    FROM group_card_sizes g JOIN syn_users u ON u.hid = g.hid AND u.j < g.members
),
weak_pairs AS (
    SELECT i,
        1 + CAST(floor(r.n_users * (pmod(xxhash64(p.seed, 'wc_a', i), 2147483629) + 1) / 2147483630D) AS INT) AS a,
        1 + CAST(floor(r.n_users * (pmod(xxhash64(p.seed, 'wc_b', i), 2147483629) + 1) / 2147483630D) AS INT) AS b
    FROM (SELECT explode(sequence(1, weak_card_pairs)) AS i FROM syn_params)
    CROSS JOIN syn_params p
    CROSS JOIN (SELECT max(n_users) AS n_users FROM syn_users_ranked) r
),
weak_cards AS (
    SELECT stack(2, a, b) AS uid, concat('w:', i) AS card_key FROM weak_pairs WHERE a <> b
)
SELECT DISTINCT uid, card_key FROM (
    SELECT * FROM private_cards
    UNION ALL SELECT * FROM household_cards
    UNION ALL SELECT * FROM group_cards
    UNION ALL SELECT * FROM weak_cards
);

-- -----------------------------------------------------------------------------
-- 6. Device links (User)-[:USED]->(Device)
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_device_links;
CACHE TABLE syn_device_links AS
WITH per_user AS (
    SELECT u.uid, u.fraud,
        (pmod(xxhash64(p.seed, 'd_n0', uid), 2147483629) + 1) / 2147483630D AS u0,
        (pmod(xxhash64(p.seed, 'd_n1', uid), 2147483629) + 1) / 2147483630D AS u1
    FROM syn_users u CROSS JOIN syn_params p
),
private_n AS (
    SELECT uid,
        CASE WHEN fraud = 1 THEN 1 + CAST(floor(-ln(u1) * 1.3) AS INT)
             WHEN u0 < 0.10 THEN 0
             WHEN u0 > 0.997 THEN LEAST(70, CAST(floor(8 * pow(u1, -0.6)) AS INT))
             ELSE 1 + CAST(floor(-ln(u1) * 0.95) AS INT) END AS n
    FROM per_user
),
private_devices AS (
    SELECT uid, concat('p:', uid, ':', i) AS device_key
    FROM (SELECT uid, explode(sequence(1, n)) AS i FROM private_n WHERE n > 0)
),
household_devices AS (
    SELECT stack(2, ua, ub) AS uid, concat('h:', ua, ':', ub) AS device_key
    FROM syn_hh_links WHERE shared_device
),
kiosk_sizes AS (
    SELECT k, 11 + CAST(floor(42 * (pmod(xxhash64(p.seed, 'k_size', k), 2147483629) + 1) / 2147483630D) AS INT) AS members
    FROM (SELECT explode(sequence(1, kiosk_devices)) AS k FROM syn_params) CROSS JOIN syn_params p
),
kiosk_devices AS (
    SELECT 1 + CAST(floor(r.n_users * (pmod(xxhash64(p.seed, 'k_user', k, i), 2147483629) + 1) / 2147483630D) AS INT) AS uid,
           concat('k:', k) AS device_key
    FROM (SELECT k, explode(sequence(1, members)) AS i FROM kiosk_sizes)
    CROSS JOIN syn_params p
    CROSS JOIN (SELECT max(n_users) AS n_users FROM syn_users_ranked) r
),
weak_pairs AS (
    SELECT i,
        1 + CAST(floor(r.n_users * (pmod(xxhash64(p.seed, 'wd_a', i), 2147483629) + 1) / 2147483630D) AS INT) AS a,
        1 + CAST(floor(r.n_users * (pmod(xxhash64(p.seed, 'wd_b', i), 2147483629) + 1) / 2147483630D) AS INT) AS b
    FROM (SELECT explode(sequence(1, weak_device_pairs)) AS i FROM syn_params)
    CROSS JOIN syn_params p
    CROSS JOIN (SELECT max(n_users) AS n_users FROM syn_users_ranked) r
),
weak_devices AS (
    SELECT stack(2, a, b) AS uid, concat('w:', i) AS device_key FROM weak_pairs WHERE a <> b
)
SELECT DISTINCT uid, device_key FROM (
    SELECT * FROM private_devices
    UNION ALL SELECT * FROM household_devices
    UNION ALL SELECT * FROM kiosk_devices
    UNION ALL SELECT * FROM weak_devices
);

-- -----------------------------------------------------------------------------
-- 7. IP links (User)-[:HAS_IP]->(IP)
--    Each address slot is private, local (2-10 syn_users), mid (tens) or a hub (hundreds+).
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_ip_links;
CACHE TABLE syn_ip_links AS
WITH per_user AS (
    SELECT u.uid, u.fraud, u.hid, u.home_ips,
        (pmod(xxhash64(p.seed, 'i_z1', uid), 2147483629) + 1) / 2147483630D AS z1,
        (pmod(xxhash64(p.seed, 'i_z2', uid), 2147483629) + 1) / 2147483630D AS z2
    FROM syn_users u CROSS JOIN syn_params p
),
n_slots AS (
    SELECT uid, hid, home_ips,
        LEAST(900, GREATEST(1, CAST(round(exp(
            ln(CASE WHEN fraud = 1 THEN 31.0 ELSE 22.0 END)
            + 1.15 * sqrt(-2 * ln(z1)) * cos(2 * pi() * z2))) AS INT))) AS n
    FROM per_user
),
slots AS (
    SELECT s.uid, s.slot,
        CASE WHEN s.n > 250 THEN 0.15D ELSE 0.347D END AS pt,
        CASE WHEN s.n > 250 THEN 0.20D ELSE 0.481D END AS lt,
        CASE WHEN s.n > 250 THEN 0.30D ELSE 0.618D END AS mt,
        (pmod(xxhash64(p.seed, 'i_kind', s.uid, s.slot), 2147483629) + 1) / 2147483630D AS uk,
        (pmod(xxhash64(p.seed, 'i_pick', s.uid, s.slot), 2147483629) + 1) / 2147483630D AS up,
        p.ip_local_pool, p.ip_mid_pool, p.ip_hub_pool, p.ip_hub_skew
    FROM (SELECT uid, n, explode(sequence(1, n)) AS slot FROM n_slots) s
    CROSS JOIN syn_params p
),
slot_ips AS (
    SELECT uid,
        CASE WHEN uk < pt THEN concat('p:', uid, ':', slot)
             WHEN uk < lt THEN concat('l:', CAST(floor(ip_local_pool * up) AS INT))
             WHEN uk < mt THEN concat('m:', CAST(floor(ip_mid_pool * pow(up, 1.4)) AS INT))
             ELSE                 concat('h:', CAST(floor(ip_hub_pool * pow(up, ip_hub_skew)) AS INT))
        END AS ip_key
    FROM slots
),
home_ips AS (
    SELECT uid, concat('home:', hid, ':', i) AS ip_key
    FROM (SELECT n.uid, n.hid, explode(sequence(1, n.home_ips)) AS i FROM n_slots n WHERE n.home_ips > 0) x
    CROSS JOIN syn_params p
    WHERE (pmod(xxhash64(p.seed, 'i_home', x.uid, x.i), 2147483629) + 1) / 2147483630D < 0.80
)
SELECT DISTINCT uid, ip_key FROM (
    SELECT * FROM slot_ips
    UNION ALL SELECT * FROM home_ips
);

-- -----------------------------------------------------------------------------
-- 8. P2P transfer pairs (directed), then individual transactions
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_p2p_pairs;
CACHE TABLE syn_p2p_pairs AS
WITH matched AS (
    -- every account is paired with one random counterparty
    SELECT a.uid AS src, b.uid AS dst
    FROM syn_users_ranked a
    JOIN syn_users_ranked b ON b.match_rank = a.match_rank + 1
    WHERE pmod(a.match_rank, 2) = 1
),
active_n AS (
    -- about 10% of accounts transact with several counterparties
    SELECT u.uid, u.n_users,
        CASE WHEN (pmod(xxhash64(p.seed, 'a_is', u.uid), 2147483629) + 1) / 2147483630D < 0.10
             THEN 1 + CAST(floor(-ln((pmod(xxhash64(p.seed, 'a_n', u.uid), 2147483629) + 1) / 2147483630D) * 3.0) AS INT)
             ELSE 0 END AS n
    FROM syn_users_ranked u CROSS JOIN syn_params p
),
active AS (
    -- 70% of their targets come from a popularity skew, which creates hub receivers
    SELECT src, um < 0.70 AS by_pop,
        CASE WHEN um < 0.70 THEN 1 + CAST(floor(n_users * pow(ut, 4.0)) AS INT)
             ELSE 1 + CAST(floor(n_users * ut) AS INT) END AS dst_rank
    FROM (
        SELECT x.uid AS src, x.n_users,
            (pmod(xxhash64(p.seed, 'a_mode', x.uid, x.i), 2147483629) + 1) / 2147483630D AS um,
            (pmod(xxhash64(p.seed, 'a_tgt',  x.uid, x.i), 2147483629) + 1) / 2147483630D AS ut
        FROM (SELECT uid, n_users, explode(sequence(1, n)) AS i FROM active_n WHERE n > 0) x
        CROSS JOIN syn_params p
    )
),
active_resolved AS (
    SELECT a.src, COALESCE(pr.uid, a.dst_rank) AS dst
    FROM active a LEFT JOIN syn_users_ranked pr ON a.by_pop AND pr.pop_rank = a.dst_rank
),
fraud_out AS (
    SELECT u.uid AS src,
        1 + CAST(floor(u.n_users * (pmod(xxhash64(p.seed, 'f_out', u.uid, i), 2147483629) + 1) / 2147483630D) AS INT) AS dst
    FROM (SELECT ur.*, explode(sequence(1, 9)) AS i FROM syn_users_ranked ur WHERE fraud = 1) u
    CROSS JOIN syn_params p
    WHERE i <= 1 + CAST(floor(-ln((pmod(xxhash64(p.seed, 'f_outn', u.uid), 2147483629) + 1) / 2147483630D) * 2.5) AS INT)
),
fraud_in AS (
    SELECT 1 + CAST(floor(u.n_users * (pmod(xxhash64(p.seed, 'f_in', u.uid, i), 2147483629) + 1) / 2147483630D) AS INT) AS src,
           u.uid AS dst
    FROM (SELECT ur.*, explode(sequence(1, 6)) AS i FROM syn_users_ranked ur WHERE fraud = 1) u
    CROSS JOIN syn_params p
    WHERE i <= CAST(floor(-ln((pmod(xxhash64(p.seed, 'f_inn', u.uid), 2147483629) + 1) / 2147483630D) * 2) AS INT)
),
household AS (
    SELECT ua AS src, ub AS dst, 0.60D AS recip FROM syn_hh_links WHERE transacts
),
one_way AS (
    SELECT src, dst, 0.30D AS recip FROM matched
    UNION ALL SELECT src, dst, 0.20D FROM active_resolved
    UNION ALL SELECT src, dst, 0.22D FROM fraud_out
    UNION ALL SELECT src, dst, 0.22D FROM fraud_in
    UNION ALL SELECT * FROM household
),
with_reverse AS (
    SELECT src, dst FROM one_way
    UNION ALL
    SELECT o.dst, o.src FROM one_way o CROSS JOIN syn_params p
    WHERE (pmod(xxhash64(p.seed, 'recip', o.src, o.dst), 2147483629) + 1) / 2147483630D < o.recip
)
SELECT DISTINCT src, dst FROM with_reverse WHERE src <> dst;

DROP VIEW IF EXISTS syn_amount_values;
CACHE TABLE syn_amount_values AS
WITH v AS (
    SELECT * FROM VALUES
        (5.0D, 0.3377D, 0.208D), (10.0D, 0.0530D, 0.030D), (100.0D, 0.0338D, 0.038D),
        (200.0D, 0.0310D, 0.099D), (500.0D, 0.0301D, 0.064D), (4000.0D, 0.0298D, 0.084D),
        (50.0D, 0.0297D, 0.099D), (20.0D, 0.0209D, 0.015D), (1000.0D, 0.0186D, 0.037D),
        (15.0D, 0.0155D, 0.027D), (300.0D, 0.0140D, 0.020D), (150.0D, 0.0115D, 0.012D),
        (25.0D, 0.0111D, 0.008D), (2000.0D, 0.0100D, 0.031D), (30.0D, 0.0096D, 0.006D),
        (40.0D, 0.0090D, 0.006D), (250.0D, 0.0088D, 0.010D), (400.0D, 0.0085D, 0.010D),
        (60.0D, 0.0073D, 0.004D), (3000.0D, 0.0056D, 0.020D), (80.0D, 0.0054D, 0.004D),
        (1500.0D, 0.0053D, 0.012D), (900.0D, 0.0052D, 0.006D), (70.0D, 0.0051D, 0.003D),
        (45.0D, 0.0050D, 0.003D), (35.0D, 0.0050D, 0.003D), (120.0D, 0.0047D, 0.004D),
        (600.0D, 0.0045D, 0.006D), (180.0D, 0.0045D, 0.004D), (55.0D, 0.0038D, 0.002D)
    AS t(amount, w_normal, w_fraud)
)
SELECT amount,
    sum(w_normal) OVER (ORDER BY amount ROWS UNBOUNDED PRECEDING) - w_normal AS lo_n,
    sum(w_normal) OVER (ORDER BY amount ROWS UNBOUNDED PRECEDING)            AS hi_n,
    sum(w_fraud)  OVER (ORDER BY amount ROWS UNBOUNDED PRECEDING) - w_fraud  AS lo_f,
    sum(w_fraud)  OVER (ORDER BY amount ROWS UNBOUNDED PRECEDING)            AS hi_f
FROM v;

DROP VIEW IF EXISTS syn_hour_weights;
CACHE TABLE syn_hour_weights AS
WITH v AS (
    SELECT * FROM VALUES
        (0,1457),(1,587),(2,396),(3,235),(4,241),(5,201),(6,262),(7,480),(8,1432),(9,4528),
        (10,6507),(11,7733),(12,8100),(13,9049),(14,8829),(15,8134),(16,7750),(17,6791),
        (18,5995),(19,6232),(20,6100),(21,5248),(22,3904),(23,2641)
    AS t(hr, w)
)
SELECT hr,
    (sum(w) OVER (ORDER BY hr ROWS UNBOUNDED PRECEDING) - w) / sum(w) OVER () AS lo,
     sum(w) OVER (ORDER BY hr ROWS UNBOUNDED PRECEDING)      / sum(w) OVER () AS hi
FROM v;

DROP VIEW IF EXISTS syn_p2p_txns;
CACHE TABLE syn_p2p_txns AS
WITH mult AS (
    SELECT pp.src, pp.dst,
        CASE WHEN (pmod(xxhash64(p.seed, 'm0', src, dst), 2147483629) + 1) / 2147483630D < 0.666 THEN 1
             ELSE LEAST(300, 1 + CAST(floor(1.25 * pow((pmod(xxhash64(p.seed, 'm1', src, dst), 2147483629) + 1) / 2147483630D, -0.75)) AS INT))
        END AS n
    FROM syn_p2p_pairs pp CROSS JOIN syn_params p
),
x AS (
    SELECT src, dst, explode(sequence(1, n)) AS i FROM mult
),
t AS (
    SELECT x.src, x.dst, x.i, s.fraud AS src_fraud,
        (pmod(xxhash64(p.seed, 't_amt',  x.src, x.dst, x.i), 2147483629) + 1) / 2147483630D AS ua,
        (pmod(xxhash64(p.seed, 't_tail', x.src, x.dst, x.i), 2147483629) + 1) / 2147483630D AS ut,
        (pmod(xxhash64(p.seed, 't_tl2',  x.src, x.dst, x.i), 2147483629) + 1) / 2147483630D AS ut2,
        (pmod(xxhash64(p.seed, 't_day',  x.src, x.dst, x.i), 2147483629) + 1) / 2147483630D AS ud,
        (pmod(xxhash64(p.seed, 't_hr',   x.src, x.dst, x.i), 2147483629) + 1) / 2147483630D AS uh,
        (pmod(xxhash64(p.seed, 't_sec',  x.src, x.dst, x.i), 2147483629) + 1) / 2147483630D AS us
    FROM x
    JOIN syn_users s ON s.uid = x.src
    CROSS JOIN syn_params p
)
SELECT t.src, t.dst,
    COALESCE(a.amount, round(LEAST(6750D, GREATEST(2.5D, exp(
        ln(CASE WHEN t.src_fraud = 1 THEN 300.0 ELSE 160.0 END)
        + 1.2 * sqrt(-2 * ln(t.ut)) * cos(2 * pi() * t.ut2)))), 0))      AS totalAmount,
    timestamp_seconds(
        (unix_date(DATE'2017-01-08') + CAST(floor(t.ud * 839) AS INT)) * 86400L
        + h.hr * 3600L + CAST(floor(t.us * 3600) AS INT))                  AS transactionDateTime
FROM t
LEFT JOIN syn_amount_values a
    ON  (t.src_fraud = 0 AND t.ua >= a.lo_n AND t.ua < a.hi_n)
    OR  (t.src_fraud = 1 AND t.ua >= a.lo_f AND t.ua < a.hi_f)
JOIN syn_hour_weights h ON t.uh >= h.lo AND t.uh < h.hi;

-- -----------------------------------------------------------------------------
-- 9. Referrals: each referred account has exactly one referrer
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS syn_referrals;
CACHE TABLE syn_referrals AS
WITH referred AS (
    SELECT u.uid AS dst, u.n_users,
        1 + CAST(floor(1600 * pow((pmod(xxhash64(p.seed, 'r_who', u.uid), 2147483629) + 1) / 2147483630D, 1.3)) AS INT) AS referrer_rank
    FROM syn_users_ranked u CROSS JOIN syn_params p
    WHERE (pmod(xxhash64(p.seed, 'r_is', u.uid), 2147483629) + 1) / 2147483630D < 0.0555
)
SELECT r.uid AS src, d.dst
FROM referred d JOIN syn_users_ranked r ON r.ref_rank = d.referrer_rank
WHERE r.uid <> d.dst;

-- =============================================================================
-- 10. Final tables — same names, columns and order as p2p_network
-- =============================================================================
DROP TABLE IF EXISTS p2p_synthetic.`user`;
CREATE TABLE p2p_synthetic.`user` AS
SELECT
    'User'                                                                       AS `_label`,
    CASE WHEN fraud = 1 AND uc < 0.99
         THEN date_add(DATE'2017-04-06', CAST(floor(ud * 717) AS INT))
         ELSE no_chargeback END                                                  AS firstChargebackMtDate,
    fraud                                                                        AS fraudMoneyTransfer,
    guid,
    CASE WHEN uz < CASE WHEN fraud = 1 THEN 0.124 ELSE 0.304 END THEN 0.0D
         ELSE round(exp(ln(CASE WHEN fraud = 1 THEN 6347.0 ELSE 1580.0 END)
                        + 1.76 * sqrt(-2 * ln(un1)) * cos(2 * pi() * un2)), 2)
    END                                                                          AS moneyTransferErrorCancelAmount
FROM (
    SELECT u.*, p.no_chargeback,
        (pmod(xxhash64(p.seed, 'a_cb',  uid), 2147483629) + 1) / 2147483630D AS uc,
        (pmod(xxhash64(p.seed, 'a_cbd', uid), 2147483629) + 1) / 2147483630D AS ud,
        (pmod(xxhash64(p.seed, 'a_z',   uid), 2147483629) + 1) / 2147483630D AS uz,
        (pmod(xxhash64(p.seed, 'a_n1',  uid), 2147483629) + 1) / 2147483630D AS un1,
        (pmod(xxhash64(p.seed, 'a_n2',  uid), 2147483629) + 1) / 2147483630D AS un2
    FROM syn_users u CROSS JOIN syn_params p
);

DROP TABLE IF EXISTS p2p_synthetic.card;
CREATE TABLE p2p_synthetic.card AS
WITH types AS (
    SELECT * FROM VALUES
        ('CARD_TYPE_19',59012),('CARD_TYPE_13',46634),('CARD_TYPE_6',11160),('CARD_TYPE_5',777),
        ('CARD_TYPE_7',510),('CARD_TYPE_14',194),('CARD_TYPE_16',153),('CARD_TYPE_20',146),
        ('CARD_TYPE_15',74),('CARD_TYPE_9',70),('CARD_TYPE_17',37),('CARD_TYPE_11',29),
        ('CARD_TYPE_3',15),('CARD_TYPE_4',2),('CARD_TYPE_18',1),('CARD_TYPE_1',1),
        ('CARD_TYPE_2',1),('CARD_TYPE_10',1),('CARD_TYPE_8',1)
    AS t(cardType, w)
),
type_cdf AS (
    SELECT cardType,
        (sum(w) OVER (ORDER BY w DESC, cardType ROWS UNBOUNDED PRECEDING) - w) / sum(w) OVER () AS lo,
         sum(w) OVER (ORDER BY w DESC, cardType ROWS UNBOUNDED PRECEDING)      / sum(w) OVER () AS hi
    FROM types
),
levels AS (
    SELECT * FROM VALUES
        ('LEVEL_TYPE_104',31487),('LEVEL_TYPE_85',24757),('LEVEL_TYPE_39',17885),('LEVEL_TYPE_123',15899),
        ('LEVEL_TYPE_115',7616),('LEVEL_TYPE_88',3233),('LEVEL_TYPE_181',2356),('LEVEL_TYPE_154',2270),
        ('LEVEL_TYPE_102',2195),('LEVEL_TYPE_81',2181),('LEVEL_TYPE_144',1925),('LEVEL_TYPE_132',1128),
        ('LEVEL_TYPE_147',1081),('LEVEL_TYPE_184',734),('LEVEL_TYPE_176',673),('LEVEL_TYPE_OTHER',3446)
    AS t(level, w)
),
level_cdf AS (
    SELECT level,
        (sum(w) OVER (ORDER BY w DESC, level ROWS UNBOUNDED PRECEDING) - w) / sum(w) OVER () AS lo,
         sum(w) OVER (ORDER BY w DESC, level ROWS UNBOUNDED PRECEDING)      / sum(w) OVER () AS hi
    FROM levels
),
cards AS (
    SELECT DISTINCT c.card_key, p.seed,
        (pmod(xxhash64(p.seed, 'ct', c.card_key), 2147483629) + 1) / 2147483630D AS ut,
        (pmod(xxhash64(p.seed, 'cl', c.card_key), 2147483629) + 1) / 2147483630D AS ul,
        md5(concat('C', p.seed, '-', c.card_key)) AS h
    FROM syn_card_links c CROSS JOIN syn_params p
)
SELECT
    'Card'                                                                        AS `_label`,
    t.cardType,
    concat_ws('-', substr(h,1,8), substr(h,9,4), substr(h,13,4), substr(h,17,4), substr(h,21,12)) AS guid,
    CASE WHEN l.level = 'LEVEL_TYPE_OTHER'
         THEN concat('LEVEL_TYPE_', 3 + pmod(xxhash64(c.seed, 'lv', c.card_key), 226))
         ELSE l.level END                                                         AS level
FROM cards c
JOIN type_cdf  t ON c.ut >= t.lo AND c.ut < t.hi
JOIN level_cdf l ON c.ul >= l.lo AND c.ul < l.hi;

DROP TABLE IF EXISTS p2p_synthetic.device;
CREATE TABLE p2p_synthetic.device AS
WITH d AS (
    SELECT DISTINCT dl.device_key, p.seed,
        (pmod(xxhash64(p.seed, 'dm', dl.device_key), 2147483629) + 1) / 2147483630D AS um,
        (pmod(xxhash64(p.seed, 'os', dl.device_key), 2147483629) + 1) / 2147483630D AS uo,
        md5(concat('D', p.seed, '-', dl.device_key)) AS h
    FROM syn_device_links dl CROSS JOIN syn_params p
)
SELECT
    'Device'                                                                      AS `_label`,
    concat('DEVICE_MODEL_', 2 + pmod(xxhash64(seed, 'dmid', CAST(floor(1236 * pow(um, 4.0)) AS INT)), 4044)) AS device,
    concat_ws('-', substr(h,1,8), substr(h,9,4), substr(h,13,4), substr(h,17,4), substr(h,21,12)) AS guid,
    CASE WHEN uo < 0.5282 THEN 'OS_TYPE_3'
         WHEN uo < 0.9995 THEN 'OS_TYPE_2'
         WHEN uo < 0.99998 THEN 'OS_TYPE_4'
         ELSE 'OS_TYPE_1' END                                                     AS os
FROM d;

DROP TABLE IF EXISTS p2p_synthetic.ip;
CREATE TABLE p2p_synthetic.ip AS
SELECT
    'IP' AS `_label`,
    concat_ws('-', substr(h,1,8), substr(h,9,4), substr(h,13,4), substr(h,17,4), substr(h,21,12)) AS guid
FROM (
    SELECT DISTINCT md5(concat('I', p.seed, '-', il.ip_key)) AS h
    FROM syn_ip_links il CROSS JOIN syn_params p
);

DROP TABLE IF EXISTS p2p_synthetic.cc_lookup;
CREATE TABLE p2p_synthetic.cc_lookup AS
SELECT
    'HAS_CC' AS `_type`,
    u.guid   AS start_guid, 'User' AS start_label,
    concat_ws('-', substr(h,1,8), substr(h,9,4), substr(h,13,4), substr(h,17,4), substr(h,21,12)) AS end_guid,
    'Card'   AS end_label,
    date_add(DATE'2016-11-20', CAST(floor(ud * 885) AS INT)) AS cardDate
FROM (
    SELECT c.uid,
        md5(concat('C', p.seed, '-', c.card_key)) AS h,
        (pmod(xxhash64(p.seed, 'cd', c.uid, c.card_key), 2147483629) + 1) / 2147483630D AS ud
    FROM syn_card_links c CROSS JOIN syn_params p
) x JOIN syn_users u ON u.uid = x.uid;

DROP TABLE IF EXISTS p2p_synthetic.used;
CREATE TABLE p2p_synthetic.used AS
SELECT
    'USED'   AS `_type`,
    u.guid   AS start_guid, 'User' AS start_label,
    concat_ws('-', substr(h,1,8), substr(h,9,4), substr(h,13,4), substr(h,17,4), substr(h,21,12)) AS end_guid,
    'Device' AS end_label,
    date_add(DATE'2017-09-09', CAST(floor(ud * 592) AS INT)) AS deviceDate
FROM (
    SELECT d.uid,
        md5(concat('D', p.seed, '-', d.device_key)) AS h,
        (pmod(xxhash64(p.seed, 'dd', d.uid, d.device_key), 2147483629) + 1) / 2147483630D AS ud
    FROM syn_device_links d CROSS JOIN syn_params p
) x JOIN syn_users u ON u.uid = x.uid;

DROP TABLE IF EXISTS p2p_synthetic.has_ip;
CREATE TABLE p2p_synthetic.has_ip AS
SELECT
    'HAS_IP' AS `_type`,
    u.guid   AS start_guid, 'User' AS start_label,
    concat_ws('-', substr(h,1,8), substr(h,9,4), substr(h,13,4), substr(h,17,4), substr(h,21,12)) AS end_guid,
    'IP'     AS end_label,
    date_add(DATE'2015-10-13', CAST(floor(ud * 1289) AS INT)) AS ipDate
FROM (
    SELECT i.uid,
        md5(concat('I', p.seed, '-', i.ip_key)) AS h,
        (pmod(xxhash64(p.seed, 'id', i.uid, i.ip_key), 2147483629) + 1) / 2147483630D AS ud
    FROM syn_ip_links i CROSS JOIN syn_params p
) x JOIN syn_users u ON u.uid = x.uid;

DROP TABLE IF EXISTS p2p_synthetic.p2p;
CREATE TABLE p2p_synthetic.p2p AS
SELECT
    'P2P'  AS `_type`,
    s.guid AS start_guid, 'User' AS start_label,
    d.guid AS end_guid,   'User' AS end_label,
    t.totalAmount,
    t.transactionDateTime
FROM syn_p2p_txns t
JOIN syn_users s ON s.uid = t.src
JOIN syn_users d ON d.uid = t.dst;

DROP TABLE IF EXISTS p2p_synthetic.referred;
CREATE TABLE p2p_synthetic.referred AS
SELECT
    'REFERRED' AS `_type`,
    s.guid     AS start_guid, 'User' AS start_label,
    d.guid     AS end_guid,   'User' AS end_label
FROM syn_referrals r
JOIN syn_users s ON s.uid = r.src
JOIN syn_users d ON d.uid = r.dst;

-- -----------------------------------------------------------------------------
-- 11. Row counts
-- -----------------------------------------------------------------------------
SELECT 'user' AS tbl, count(*) AS rows_ FROM p2p_synthetic.`user`
UNION ALL SELECT 'card',      count(*) FROM p2p_synthetic.card
UNION ALL SELECT 'device',    count(*) FROM p2p_synthetic.device
UNION ALL SELECT 'ip',        count(*) FROM p2p_synthetic.ip
UNION ALL SELECT 'cc_lookup', count(*) FROM p2p_synthetic.cc_lookup
UNION ALL SELECT 'used',      count(*) FROM p2p_synthetic.used
UNION ALL SELECT 'has_ip',    count(*) FROM p2p_synthetic.has_ip
UNION ALL SELECT 'p2p',       count(*) FROM p2p_synthetic.p2p
UNION ALL SELECT 'referred',  count(*) FROM p2p_synthetic.referred;
