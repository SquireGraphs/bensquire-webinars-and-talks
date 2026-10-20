"""Synthetic air-freight network for the Graph Data Science workshop.

Fictional airports, carriers, shippers and contracts, generated deterministically
(same seed -> same data). The data has a few planted patterns, one per workshop lens:

  1. What matters most?    Northgate (NGT) has the most routes and flights, but Corvina (CRV)
                           carries the high-value pharma contracts.
  2. What breaks?          Cape Lorne (CLR), a small regional airport, is the only link
                           between the mainland and the seven-airport archipelago.
  3. How do things travel? Westport (WSP) sits on the cheapest island-to-mainland paths;
                           closing it forces a reroute.
  4. What belongs together? Dunmore (DUN) and Kessa (KES) share exactly the same connections;
                           trade corridors (pharma, semiconductors, perishables) cut across regions.

Run `python data_generator.py` to write CSV files to ./data for inspection.
"""
import csv
import math
import random
from pathlib import Path

SEED = 42

REGIONS = {
    # region: (center_x_km, center_y_km, hub_code, hub_name)
    "North": (0, 900, "NGT", "Northgate"),
    "East": (950, 250, "CRV", "Corvina"),
    "South": (150, -750, "SOL", "Solano"),
    "West": (-850, 100, "WSP", "Westport"),
}

REGIONALS = {
    "North": [("BRK", "Brennick"), ("CLW", "Calloway"), ("DLN", "Dunlin"), ("EHM", "Easterholm"),
              ("FJH", "Fjordhaven"), ("GLM", "Glenmoor"), ("HRF", "Harrowfield"), ("IVL", "Ivel")],
    "East": [("ASG", "Ashgrove"), ("BWT", "Brightwater"), ("CDF", "Cedarfall"), ("DLM", "Delmont"),
             ("EMB", "Emberly"), ("FHV", "Fairhaven"), ("GRS", "Greystone"), ("HLD", "Halden")],
    "South": [("SLV", "Silica Valley"), ("DUN", "Dunmore"), ("KES", "Kessa"), ("MRG", "Marigold"),
              ("NVR", "Novara"), ("ORL", "Orlen"), ("PLS", "Pellston"), ("QPT", "Quarry Point")],
    "West": [("CLR", "Cape Lorne"), ("RDG", "Ridgeway"), ("STW", "Stillwater"), ("THB", "Thornby"),
             ("UMB", "Umber"), ("VRD", "Varden"), ("WLM", "Willowmere"), ("YRW", "Yarrow")],
}

ISLANDS = [("MRI", "Marrow Isle"), ("PCY", "Pearl Cay"), ("CRH", "Coral Reach"), ("GRK", "Gull Rock"),
           ("SLM", "Saltmere"), ("TRN", "Tern Island"), ("KPS", "Kelpie Sound")]
PENINSULA = [("SKR", "Skerra"), ("TLV", "Tolvik"), ("ULB", "Ulsby")]

# Undirected routes (each pair appears once).
ROUTES = [
    # hub mesh
    ("NGT", "CRV"), ("NGT", "SOL"), ("NGT", "WSP"), ("CRV", "SOL"), ("CRV", "WSP"), ("SOL", "WSP"),
    # North: every regional to Northgate, plus a few regional links
    *[("NGT", c) for c, _ in REGIONALS["North"]],
    ("BRK", "CLW"), ("DLN", "EHM"), ("GLM", "HRF"), ("FJH", "GLM"),
    # Northgate's point-to-point e-commerce flights into other regions (makes it the busiest airport)
    ("NGT", "ASG"), ("NGT", "DLM"), ("NGT", "MRG"), ("NGT", "PLS"), ("NGT", "RDG"), ("NGT", "STW"), ("NGT", "THB"),
    # peninsula behind Fjordhaven
    ("FJH", "SKR"), ("FJH", "TLV"), ("SKR", "TLV"), ("TLV", "ULB"),
    # East: every regional to Corvina, plus regional links
    *[("CRV", c) for c, _ in REGIONALS["East"]],
    ("ASG", "BWT"), ("CDF", "DLM"), ("EMB", "FHV"), ("GRS", "HLD"),
    # South: hub links (QPT is a single-link spoke); DUN and KES are twins with identical neighbors
    ("SOL", "SLV"), ("SOL", "MRG"), ("SOL", "NVR"), ("SOL", "ORL"), ("SOL", "PLS"), ("SOL", "QPT"),
    ("SOL", "DUN"), ("SOL", "KES"), ("MRG", "DUN"), ("MRG", "KES"), ("NVR", "DUN"), ("NVR", "KES"),
    ("CRV", "DUN"), ("CRV", "KES"), ("SLV", "NVR"), ("ORL", "PLS"), ("CRV", "SLV"),
    # West: hub links (YRW is a single-link spoke)
    ("WSP", "CLR"), ("WSP", "RDG"), ("WSP", "STW"), ("WSP", "THB"), ("WSP", "UMB"), ("WSP", "VRD"),
    ("WSP", "WLM"), ("WSP", "YRW"), ("CLR", "RDG"), ("STW", "THB"), ("UMB", "VRD"), ("THB", "WLM"),
    # archipelago: reachable only through Cape Lorne
    ("CLR", "MRI"), ("CLR", "PCY"), ("MRI", "PCY"), ("PCY", "CRH"), ("CRH", "GRK"), ("GRK", "SLM"),
    ("SLM", "TRN"), ("TRN", "KPS"), ("KPS", "MRI"), ("MRI", "CRH"),
]
# A seasonal route that is suspended (active=false); the projection filters it out.
INACTIVE_ROUTES = [("GRK", "SOL")]

CARRIERS = [
    ("NFX", "Northline Freight Express"), ("BLW", "Bluewing Cargo"), ("ATL", "Atlas Air Logistics"),
    ("SKY", "Skyhaul"), ("MER", "Meridian Air"), ("ISL", "Island Hopper Freight"),
]

# industry: (shipper names, revenue range in $M per contract, corridors as airport paths)
INDUSTRIES = {
    "Pharma": (
        ["Helix Pharma", "Novacure Biologics", "Aurelia Health", "Medivane"],
        (9.0, 22.0),
        [["ASG", "CRV"], ["BWT", "CRV", "WSP"], ["FHV", "CRV", "SOL"], ["CDF", "CRV", "NGT"],
         ["EMB", "CRV", "DUN"], ["GRS", "CRV", "KES"], ["HLD", "CRV"], ["DLM", "CRV", "WSP"],
         ["CRV", "SOL", "PLS"], ["EMB", "FHV", "CRV"]],
    ),
    "Semiconductors": (
        ["Silica Microsystems", "Quantra Semiconductors", "Lumen Devices"],
        (6.0, 15.0),
        [["SLV", "SOL", "NGT"], ["SLV", "CRV"], ["NVR", "SLV", "SOL"], ["SLV", "SOL", "WSP"],
         ["SLV", "NVR"], ["ORL", "SOL", "SLV"]],
    ),
    "Perishables": (
        ["Saltmere Seafood Co-op", "Coral Fresh", "Archipelago Fisheries"],
        (1.5, 4.0),
        [["MRI", "CLR", "WSP"], ["PCY", "CLR", "WSP"], ["GRK", "CRH", "MRI", "CLR"], ["SLM", "TRN", "KPS", "MRI"],
         ["CRH", "PCY", "CLR", "RDG"], ["SKR", "FJH", "NGT"], ["ULB", "TLV", "FJH"]],
    ),
    "E-commerce": (
        ["Parcelly", "BoxRun", "QuickCart", "Shelfwise", "Dropline"],
        (0.3, 1.5),
        [["NGT", "ASG"], ["NGT", "DLM"], ["NGT", "MRG"], ["NGT", "PLS"], ["NGT", "RDG"], ["NGT", "STW"],
         ["NGT", "THB"], ["BRK", "NGT", "SOL"], ["CLW", "NGT", "WSP"], ["DLN", "NGT"], ["EHM", "NGT"],
         ["GLM", "NGT"], ["HRF", "NGT"], ["IVL", "NGT"], ["NGT", "CRV"], ["NGT", "SOL"], ["NGT", "WSP"]],
    ),
}
CONTRACTS_PER_CORRIDOR = {"Pharma": 2, "Semiconductors": 2, "Perishables": 2, "E-commerce": 4}
# weekly flights by route type: e-commerce volume makes Northgate the busiest airport
FLIGHTS = {"ngt": (18, 30), "hub": (10, 16), "regional": (3, 8)}


def _airports(rng):
    airports = []
    for region, (cx, cy, code, name) in REGIONS.items():
        airports.append(dict(code=code, name=name, region=region, type="Hub", x=cx, y=cy))
        for i, (rc, rn) in enumerate(REGIONALS[region]):
            angle = 2 * math.pi * i / len(REGIONALS[region]) + rng.uniform(-0.2, 0.2)
            radius = rng.uniform(220, 420)
            airports.append(dict(code=rc, name=rn, region=region, type="Regional",
                                 x=round(cx + radius * math.cos(angle)), y=round(cy + radius * math.sin(angle))))
    for i, (c, n) in enumerate(ISLANDS):
        angle = 2 * math.pi * i / len(ISLANDS)
        airports.append(dict(code=c, name=n, region="Archipelago", type="Regional",
                             x=round(-1750 + 220 * math.cos(angle)), y=round(-350 + 220 * math.sin(angle))))
    for i, (c, n) in enumerate(PENINSULA):
        airports.append(dict(code=c, name=n, region="North", type="Regional", x=-450 - 120 * i, y=1250 + 60 * i))
    by_code = {a["code"]: a for a in airports}
    # pin the gateways where the story needs them
    by_code["CLR"].update(x=-1300, y=-150)   # west coast, facing the archipelago
    by_code["FJH"].update(x=-300, y=1150)    # north-west, facing the peninsula
    for i, a in enumerate(airports):
        a["idx"] = i
    return airports, by_code


def generate(seed=SEED):
    rng = random.Random(seed)
    airports, by_code = _airports(rng)

    def dist(a, b):
        pa, pb = by_code[a], by_code[b]
        return round(math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"]))

    routes, route_by_pair = [], {}
    for active, pairs in ((True, ROUTES), (False, INACTIVE_ROUTES)):
        for a, b in pairs:
            key = frozenset((a, b))
            assert key not in route_by_pair, f"duplicate route {a}-{b}"
            kind = "ngt" if "NGT" in key else ("hub" if {by_code[a]["type"], by_code[b]["type"]} == {"Hub"} else "regional")
            r = dict(id=f"R{len(routes) + 1:03d}", origin=a, destination=b, distanceKm=dist(a, b),
                     weeklyFlights=rng.randint(*FLIGHTS[kind]), active=active)
            routes.append(r)
            route_by_pair[key] = r

    def route_for(a, b):
        r = route_by_pair.get(frozenset((a, b)))
        assert r is not None and r["active"], f"corridor uses missing route {a}-{b}"
        return r

    # carriers: island routes go to Island Hopper, everything else is spread across the rest
    operates = []
    for r in routes:
        if {by_code[r["origin"]]["region"], by_code[r["destination"]]["region"]} & {"Archipelago"}:
            ops = ["ISL"]
        else:
            ops = rng.sample(["NFX", "BLW", "ATL", "SKY", "MER"], k=rng.choice([1, 1, 2]))
        operates += [dict(carrier=c, route=r["id"]) for c in ops]

    shippers, contracts, ships_on = [], [], []
    for industry, (names, (lo, hi), corridors) in INDUSTRIES.items():
        ids = []
        for n in names:
            sid = f"S{len(shippers) + 1:03d}"
            shippers.append(dict(id=sid, name=n, industry=industry))
            ids.append(sid)
        for path in corridors:
            for _ in range(CONTRACTS_PER_CORRIDOR[industry]):
                revenue = round(rng.uniform(lo, hi), 2)
                cid = f"C{len(contracts) + 1:04d}"
                contracts.append(dict(id=cid, shipper=rng.choice(ids), industry=industry,
                                      annualRevenueM=revenue, slaPenaltyPerDayK=round(revenue * rng.uniform(4, 9), 1)))
                ships_on += [dict(contract=cid, route=route_for(a, b)["id"]) for a, b in zip(path, path[1:])]

    # every active route carries at least some general cargo
    covered = {s["route"] for s in ships_on}
    general = dict(id=f"S{len(shippers) + 1:03d}", name="General Cargo Pool", industry="General")
    shippers.append(general)
    for r in routes:
        if r["active"] and r["id"] not in covered:
            cid = f"C{len(contracts) + 1:04d}"
            revenue = round(rng.uniform(0.2, 0.8), 2)
            contracts.append(dict(id=cid, shipper=general["id"], industry="General",
                                  annualRevenueM=revenue, slaPenaltyPerDayK=round(revenue * 3, 1)))
            ships_on.append(dict(contract=cid, route=r["id"]))

    for a in airports:  # coordinates are only used to compute distances
        a.pop("x"), a.pop("y")
    return dict(
        airports=airports,
        carriers=[dict(code=c, name=n) for c, n in CARRIERS],
        routes=routes,
        operates=operates,
        shippers=shippers,
        contracts=contracts,
        ships_on=ships_on,
    )


def write_csv(data, out_dir="data"):
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    for name, rows in data.items():
        with open(out / f"{name}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return out


if __name__ == "__main__":
    d = generate()
    path = write_csv(d)
    print(f"Wrote {', '.join(f'{len(v)} {k}' for k, v in d.items())} to {path}/")
