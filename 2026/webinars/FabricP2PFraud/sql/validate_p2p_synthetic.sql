-- Validation queries for p2p_synthetic. Each statement is independent.

-- Confirmed fraud accounts
SELECT count(*) AS confirmed_fraud,
       round(100.0 * count(*) / (SELECT count(*) FROM p2p_synthetic.`user`), 2) AS pct_of_accounts
FROM p2p_synthetic.`user` WHERE fraudMoneyTransfer = 1;

-- Non-fraud accounts one hop from a fraud account through a card, device or P2P payment
WITH f AS (SELECT guid FROM p2p_synthetic.`user` WHERE fraudMoneyTransfer = 1),
nf AS (SELECT guid FROM p2p_synthetic.`user` WHERE fraudMoneyTransfer = 0),
card_hot AS (SELECT DISTINCT c.end_guid FROM p2p_synthetic.cc_lookup c JOIN f ON c.start_guid = f.guid),
dev_hot  AS (SELECT DISTINCT d.end_guid FROM p2p_synthetic.used d JOIN f ON d.start_guid = f.guid),
linked AS (
    SELECT c.start_guid AS guid FROM p2p_synthetic.cc_lookup c JOIN card_hot h ON c.end_guid = h.end_guid
    UNION SELECT d.start_guid FROM p2p_synthetic.used d JOIN dev_hot h ON d.end_guid = h.end_guid
    UNION SELECT p.start_guid FROM p2p_synthetic.p2p p JOIN f ON p.end_guid = f.guid
    UNION SELECT p.end_guid   FROM p2p_synthetic.p2p p JOIN f ON p.start_guid = f.guid
)
SELECT count(*) AS nonfraud_linked_by_card_device_p2p FROM linked l JOIN nf ON l.guid = nf.guid;

-- Non-fraud accounts sharing at least one IP with a fraud account (the noisy signal)
WITH f AS (SELECT guid FROM p2p_synthetic.`user` WHERE fraudMoneyTransfer = 1),
hot AS (SELECT DISTINCT h.end_guid FROM p2p_synthetic.has_ip h JOIN f ON h.start_guid = f.guid)
SELECT count(DISTINCT h.start_guid) AS nonfraud_sharing_an_ip_with_fraud
FROM p2p_synthetic.has_ip h
JOIN hot ON h.end_guid = hot.end_guid
JOIN p2p_synthetic.`user` u ON u.guid = h.start_guid AND u.fraudMoneyTransfer = 0;

-- Top 10 non-fraud accounts by IPs shared with fraud accounts
WITH f AS (SELECT guid FROM p2p_synthetic.`user` WHERE fraudMoneyTransfer = 1),
hot AS (SELECT DISTINCT h.end_guid FROM p2p_synthetic.has_ip h JOIN f ON h.start_guid = f.guid)
SELECT h.start_guid AS user_guid, count(*) AS ips_shared_with_fraud
FROM p2p_synthetic.has_ip h
JOIN hot ON h.end_guid = hot.end_guid
JOIN p2p_synthetic.`user` u ON u.guid = h.start_guid AND u.fraudMoneyTransfer = 0
GROUP BY h.start_guid
ORDER BY ips_shared_with_fraud DESC
LIMIT 10;

-- Busiest IP addresses (carrier NAT / VPN style hubs)
SELECT end_guid AS ip_guid, count(*) AS accounts
FROM p2p_synthetic.has_ip GROUP BY end_guid ORDER BY accounts DESC LIMIT 5;
