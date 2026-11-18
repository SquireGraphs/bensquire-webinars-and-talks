"""Slim a Scryfall bulk file down to one record per unique card (oracle_id).

    python slim_cards.py default-cards-20260428090833.json cards.jsonl.gz

Streams the file line by line (Scryfall writes one card object per line), so
the 500+ MB input is never loaded into memory at once. For each oracle_id it
keeps the most recent paper printing and only the fields synergy work needs.
"""
import gzip
import json
import sys

KEEP = ["oracle_id", "name", "layout", "mana_cost", "cmc", "type_line",
        "colors", "color_identity", "keywords", "power", "toughness",
        "loyalty", "produced_mana", "released_at", "set", "rarity",
        "edhrec_rank", "game_changer"]


def cards(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if line.startswith("{"):
                yield json.loads(line)


def slim(c):
    out = {k: c.get(k) for k in KEEP}
    faces = c.get("card_faces") or []
    # Multi-face cards keep their rules text on each face.
    out["oracle_text"] = c.get("oracle_text") or "\n//\n".join(
        f.get("oracle_text", "") for f in faces)
    if not out["mana_cost"] and faces:
        out["mana_cost"] = " // ".join(f.get("mana_cost", "") for f in faces)
    out["faces"] = [{"name": f.get("name"), "type_line": f.get("type_line"),
                     "oracle_text": f.get("oracle_text")} for f in faces]
    out["legal_in"] = sorted(fmt for fmt, s in (c.get("legalities") or {}).items()
                             if s in ("legal", "restricted"))
    # all_parts: tokens and other referenced cards. "combo_piece" mostly means
    # "creates or references", not a real combo, so keep the raw component.
    out["related"] = [{"name": p["name"], "component": p["component"],
                       "type_line": p.get("type_line")}
                      for p in c.get("all_parts") or [] if p.get("name") != c["name"]]
    out["is_token"] = c.get("layout") in ("token", "double_faced_token")
    # Image link only (front face for double-faced cards): the app loads it from Scryfall on hover.
    uris = c.get("image_uris") or ((c.get("card_faces") or [{}])[0].get("image_uris") or {})
    out["image"] = uris.get("normal")
    return out


def main(src, dst):
    best, n = {}, 0
    for c in cards(src):
        n += 1
        oid = c.get("oracle_id")
        if not oid or c.get("layout") in ("art_series",):
            continue
        prev = best.get(oid)
        # Prefer paper printings, then the most recent release.
        rank = ("paper" in (c.get("games") or []), c.get("released_at", ""))
        if prev is None or rank > prev[0]:
            best[oid] = (rank, slim(c))
    with gzip.open(dst, "wt", encoding="utf-8") as out:
        for _, card in best.values():
            out.write(json.dumps(card, ensure_ascii=False) + "\n")
    print(f"read {n} printings, wrote {len(best)} unique cards to {dst}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
