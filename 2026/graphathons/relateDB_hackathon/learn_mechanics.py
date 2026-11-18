"""Learn mechanics from verified state traces: what the graph should model next.

    python learn_mechanics.py mtg_live.rl          # combo_traces.json + combo_steps.jsonl -> mechanics_from_traces.json

For every combo whose trace verifies (infinite or near-infinite), check whether the graph's loop finder also
traces it. For the ones it can't, read the State patterns in its steps, name the mechanic behind each (casting
from the graveyard for an extra cost, storm copies, untapping, mill...), and rank mechanics by how many verified
combos they would unlock. Each comes with its State pattern, examples and a rule proposal in the Teach-a-mechanic
format, ready to test with mine.py's verifier once the graph has the counts it needs.
"""
import json
import os
import sys
from collections import defaultdict

import relate

from engine import find_loops
from wins import find_wins
from trace_combos import FIXTURES


def patterns(trace):
    """Name the mechanic behind each step from its State pattern."""
    out = []
    for s in trace["steps"]:
        req, cau = s.get("requires") or {}, s.get("causes") or {}
        zones_req, moves, add = req.get("zones") or {}, cau.get("moves") or {}, cau.get("add") or {}
        say = (s.get("say") or "").lower()
        for card, to in moves.items():
            frm = zones_req.get(card)
            if frm == "G" and to in ("B", "S"):
                extra = [k for k, v in add.items() if isinstance(v, (int, float)) and v < 0 and k not in ("mana",)]
                out.append(("cast or return from the graveyard" + (f", paying {', '.join(extra)}" if extra else ""), card))
            elif frm == "B" and to == "G":
                out.append(("sacrifice or discard into the graveyard", card))
            elif frm == "H" and to == "B":
                out.append(("cast from hand again (it was returned to hand)", card))
            elif frm == "B" and to == "H":
                out.append(("return to hand", card))
        if "storm" in add or "storm" in say:
            out.append(("storm copies", None))
        if any(k.startswith("untapped") for k in add) or "untap" in say:
            out.append(("untapping", None))
        if any(k in add for k in ("library", "opponent_library")):
            out.append(("mill", None))
        if any(k in add for k in ("life", "opponent_life")):
            out.append(("life gain and loss", None))
    return out


def main(store):
    traces = json.load(open("combo_traces.json"))
    steps = {t["id"]: t for t in FIXTURES}
    if os.path.exists("combo_steps.jsonl"):
        steps.update({t["id"]: t for t in map(json.loads, open("combo_steps.jsonl"))})
    db = relate.connect(store)
    learned = defaultdict(lambda: {"combos": [], "cards": set()})
    verified = graph_traced = 0
    for cid, t in traces.items():
        if t["kind"] not in ("infinite", "near-infinite") or cid not in steps:
            continue
        verified += 1
        cards = sorted(t["zones"])
        if any(set(l["cards"]) <= set(cards) for l in find_loops(db, cards) + find_wins(db, cards)):   # loops, wins and patterns
            graph_traced += 1
            continue
        for mech, card in patterns(steps[cid]):
            learned[mech]["combos"].append(" + ".join(cards))
            if card:
                learned[mech]["cards"].add(card)
    ranked = sorted(learned.items(), key=lambda kv: -len(set(kv[1]["combos"])))
    out = [{"mechanic": m, "unlocks": len(set(v["combos"])), "examples": sorted(set(v["combos"]))[:5], "cards": sorted(v["cards"])} for m, v in ranked]
    json.dump({"verified": verified, "graph_traces": graph_traced, "mechanics": out}, open("mechanics_from_traces.json", "w"), indent=1)
    print(f"{verified} verified traces; the graph already traces {graph_traced}; {verified - graph_traced} it can't yet")
    for m in out:
        print(f"  {m['unlocks']:3}  {m['mechanic']}  (e.g. {m['examples'][0]})")


if __name__ == "__main__":
    main(sys.argv[1])
