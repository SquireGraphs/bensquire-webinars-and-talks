"""Trace a known combo through its steps, with the states it needs: zones per card and counts.

The starting state comes from Commander Spellbook (each card's zoneLocations, plus the prerequisites). Each
step REQUIRES states (a card in a zone, at least N of a count) and CAUSES changes (moves, +/- counts). Walking
the steps lap after lap shows whether the loop returns to its start:
  * every card back in its starting zone, and every count no lower    -> infinite
  * the same, but some count only ever falls (the library)            -> near-infinite (it ends when that runs out)
  * a step's requirement fails                                         -> not a loop (and which step, and why)

This prototype writes the Breach steps by hand in the State vocabulary; the plan is for an LLM to turn each of
Spellbook's step sentences into the same form, and for this walk to verify it.
"""

ZONES = {"B": "battlefield", "G": "graveyard", "H": "hand", "E": "exile", "L": "library", "C": "command zone"}

# Underworld Breach + Lion's Eye Diamond + Brain Freeze (Commander Spellbook 1368-3518-4856)
COMBO = {
    "cards": {"Underworld Breach": "B", "Lion's Eye Diamond": "B", "Brain Freeze": "G"},   # Spellbook's zoneLocations
    "counts": {"hand": 6, "graveyard": 0, "mana": 0, "storm": 0, "library": 60, "opponent library": 99},
    "prerequisite": "You have at least six other cards in hand and/or graveyard.",
    "steps": [
        {"say": "Activate Lion's Eye Diamond: discard your hand, sacrifice it, add {U}{U}{U}",
         "requires": {"zone": {"Lion's Eye Diamond": "B"}},
         "causes": {"move": {"Lion's Eye Diamond": "G"}, "hand->graveyard": True, "mana": +3}},
        {"say": "Escape Lion's Eye Diamond: cast it from the graveyard, exiling three other cards",
         "requires": {"zone": {"Lion's Eye Diamond": "G", "Underworld Breach": "B"}, "at least": {"graveyard": 3}},
         "causes": {"move": {"Lion's Eye Diamond": "B"}, "graveyard": -3, "storm": +1}},
        {"say": "Escape Brain Freeze: pay {1}{U} and exile three cards from the graveyard",
         "requires": {"zone": {"Brain Freeze": "G", "Underworld Breach": "B"}, "at least": {"graveyard": 3, "mana": 2}},
         "causes": {"move": {"Brain Freeze": "stack"}, "graveyard": -3, "mana": -2, "storm": +1}},
        {"say": "Storm copies Brain Freeze once per spell cast before it; one copy targets you, the rest an opponent",
         "requires": {"at least": {"storm": 2}},
         "causes": {"copies": "storm - 1"}},
        {"say": "Resolve: you mill 3 + 3 (the original and one copy); the opponent mills 3 per other copy",
         "requires": {},
         "causes": {"move": {"Brain Freeze": "G"}, "self mill": 6, "opponent mill per extra copy": 3}},
    ],
}


def walk(combo, laps=3):
    zone = dict(combo["cards"])
    n = dict(combo["counts"])
    start_zone = dict(zone)
    history = []
    for lap in range(1, laps + 1):
        copies = 0
        for i, s in enumerate(combo["steps"], 1):
            for card, z in s["requires"].get("zone", {}).items():
                if zone[card] != z:
                    return history, f"lap {lap}, step {i}: {card} must be in the {ZONES.get(z, z)}, but it's in the {ZONES.get(zone[card], zone[card])}"
            for k, at_least in s["requires"].get("at least", {}).items():
                if n[k] < at_least:
                    return history, f"lap {lap}, step {i}: needs {at_least} {k}, has {n[k]}"
            c = s["causes"]
            if c.get("hand->graveyard"):
                n["graveyard"] += n["hand"]; n["hand"] = 0
            for k in ("graveyard", "mana", "storm"):
                n[k] += c.get(k, 0)
            if c.get("copies"):
                copies = n["storm"] - 1
            if c.get("self mill"):
                milled = min(c["self mill"], n["library"])
                n["library"] -= milled; n["graveyard"] += milled
                extra = max(copies - 1, 0)
                n["opponent library"] = max(n["opponent library"] - 3 * extra, 0)
            zone.update(c.get("move", {}))
        history.append({"lap": lap, "zones": dict(zone), **n})
    back = zone == start_zone
    first, last = history[0], history[-1]
    rising = [k for k in ("mana", "storm") if last[k] > first[k]]
    falling = [k for k in ("library", "opponent library") if last[k] < first[k]]
    verdict = ("near-infinite (ends when " + " and ".join(falling) + " run out)" if falling else "infinite") if back else "not a loop: cards don't return"
    return history, f"every card is back where it started; per lap: {', '.join(f'+{(last[k] - first[k]) // (len(history) - 1)} {k}' for k in rising)}; {verdict}"


if __name__ == "__main__":
    hist, verdict = walk(COMBO)
    print(f"Start: " + ", ".join(f"{c} in the {ZONES[z]}" for c, z in COMBO["cards"].items()) + f"; {COMBO['prerequisite']}")
    print(f"{'lap':>3}  {'LED':12} {'Brain Freeze':13} {'hand':>4} {'gy':>3} {'mana':>4} {'storm':>5} {'library':>7} {'opp. lib':>8}")
    for h in hist:
        z = h["zones"]
        print(f"{h['lap']:>3}  {ZONES[z['Lion' + chr(39) + 's Eye Diamond']]:12} {ZONES.get(z['Brain Freeze'], z['Brain Freeze']):13} {h['hand']:>4} {h['graveyard']:>3} {h['mana']:>4} {h['storm']:>5} {h['library']:>7} {h['opponent library']:>8}")
    print("Verdict:", verdict)
