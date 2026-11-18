"""Stream Commander Spellbook's variants export into a slim JSONL file.

    python slim_variants.py variants.json.gz variants_slim.jsonl.gz
"""
import gzip
import json
import sys

import ijson


def main(src, dst):
    n = 0
    with gzip.open(src, "rb") as f, gzip.open(dst, "wt", encoding="utf-8") as out:
        for v in ijson.items(f, "variants.item"):
            out.write(json.dumps({
                "id": v["id"],
                # each card's starting zone and state (B battlefield, G graveyard, H hand, E exile, L library, C command zone)
                "uses": [{"card": {"name": u["card"]["name"]}, "zones": u.get("zoneLocations") or [],
                          **({"state": st} if (st := " ".join(x for x in (u.get("battlefieldCardState"), u.get("graveyardCardState"),
                                                                       u.get("exileCardState"), u.get("libraryCardState")) if x)) else {}),
                          **({"commander": True} if u.get("mustBeCommander") else {})} for u in v.get("uses", [])],
                "requires": [{"template": {"name": r["template"]["name"]}} for r in v.get("requires", [])],
                "produces": [{"feature": {"name": p["feature"]["name"]}} for p in v.get("produces", [])],
                "prereq": " ".join(x for x in (v.get("easyPrerequisites"), v.get("notablePrerequisites")) if x),
                "mana": v.get("manaNeeded") or "",
                "steps": [x for x in (v.get("description") or "").split("\n") if x.strip()],
                "legalities": {"commander": (v.get("legalities") or {}).get("commander")},
                "popularity": v.get("popularity"),
            }, default=str) + "\n")
            n += 1
    print(f"wrote {n} variants to {dst}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
