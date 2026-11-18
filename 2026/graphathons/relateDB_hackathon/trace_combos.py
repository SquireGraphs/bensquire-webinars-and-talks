"""Trace known combos through their own steps, with the states they need.

    python trace_combos.py fixtures                                   # verify the hand-written examples
    python trace_combos.py build mtg_live.rl variants_slim.jsonl.gz   # known combos in the sample decks -> requests
    python trace_combos.py run                                        # an LLM turns each step into State form (API key)
    python trace_combos.py verify                                     # walk every translation -> combo_traces.json

Starting zones come from Commander Spellbook (each card's zone: B battlefield, G graveyard, H hand, E exile, L library,
C command zone). Each step REQUIRES states (a card in a zone, at least N of a count) and CAUSES changes (moves,
count changes, transfers). Walking three laps gives a verdict:
  infinite         every card is back where it started and no count falls
  near-infinite    the same, but a finite pool falls each lap (a library, a life total): it ends when that runs out
  not sustainable  the cards return, but something the loop needs (mana...) falls each lap
  fails            a step's requirement isn't met (which step, and why)
The walk checks the translation: a step that doesn't follow from the one before shows up as a failure.
"""
import argparse
import gzip
import json
import os
import re
import sys

ZONES = {"B": "battlefield", "G": "graveyard", "H": "hand", "E": "exile", "L": "library", "C": "command zone", "S": "stack"}
FINITE = {"library", "opponent_library", "life", "opponent_life", "opponents_life"}

FIXTURES = [
    {"id": "1368-3518-4856", "name": "Underworld Breach + Lion's Eye Diamond + Brain Freeze",
     "start": {"hand": 6, "graveyard": 0, "mana": 0, "storm": 0, "library": 60, "opponent_library": 99},
     "steps": [
         {"say": "Activate Lion's Eye Diamond: discard your hand, sacrifice it, add {U}{U}{U}",
          "requires": {"zones": {"Lion's Eye Diamond": "B"}},
          "causes": {"moves": {"Lion's Eye Diamond": "G"}, "transfer": [{"from": "hand", "to": "graveyard", "amount": "all"}], "add": {"mana": 3}}},
         {"say": "Escape Lion's Eye Diamond, exiling three other cards from your graveyard",
          "requires": {"zones": {"Lion's Eye Diamond": "G", "Underworld Breach": "B"}, "at_least": {"graveyard": 3}},
          "causes": {"moves": {"Lion's Eye Diamond": "B"}, "add": {"graveyard": -3, "storm": 1}}},
         {"say": "Escape Brain Freeze: pay {1}{U}, exile three cards from your graveyard",
          "requires": {"zones": {"Brain Freeze": "G", "Underworld Breach": "B"}, "at_least": {"graveyard": 3, "mana": 2}},
          "causes": {"moves": {"Brain Freeze": "S"}, "add": {"graveyard": -3, "mana": -2, "storm": 1}}},
         {"say": "Storm copies resolve: you mill 6, the opponent mills 3 per extra copy; Brain Freeze returns to the graveyard",
          "requires": {"at_least": {"storm": 2}},
          "causes": {"moves": {"Brain Freeze": "G"}, "add": {"library": -6, "graveyard": 6, "opponent_library": "-3*(storm-2)"}}}]},
    {"id": "1409-3821", "name": "Deadeye Navigator + Peregrine Drake",
     "start": {"mana": 3},
     "steps": [
         {"say": "Pay {1}{U}: Deadeye Navigator blinks Peregrine Drake",
          "requires": {"zones": {"Peregrine Drake": "B", "Deadeye Navigator": "B"}, "at_least": {"mana": 2}},
          "causes": {"moves": {"Peregrine Drake": "B"}, "add": {"mana": -2, "enters": 1}}},
         {"say": "Peregrine Drake untaps five lands; tap them for mana",
          "requires": {"zones": {"Peregrine Drake": "B"}},
          "causes": {"add": {"mana": 5}}}]},
    {"id": "690-3966", "name": "Sanguine Bond + Exquisite Blood",
     "start": {"life": 40, "opponent_life": 40}, "repeat_from": 1,
     "steps": [
         {"say": "Gain life (to start it)", "requires": {}, "causes": {"add": {"life": 1}}},
         {"say": "Sanguine Bond: an opponent loses 1 life",
          "requires": {"zones": {"Sanguine Bond": "B"}}, "causes": {"add": {"opponent_life": -1}}},
         {"say": "Exquisite Blood: you gain 1 life",
          "requires": {"zones": {"Exquisite Blood": "B"}}, "causes": {"add": {"life": 1}}}]},
    {"id": "751-2757", "name": "Earthcraft + Shrieking Drake",
     "start": {"mana": 0, "untapped_islands": 1, "storm": 0},
     "steps": [
         {"say": "Tap an Island for {U}", "requires": {"at_least": {"untapped_islands": 1}},
          "causes": {"add": {"untapped_islands": -1, "mana": 1}}},
         {"say": "Cast Shrieking Drake for {U}", "requires": {"zones": {"Shrieking Drake": "H"}, "at_least": {"mana": 1}},
          "causes": {"moves": {"Shrieking Drake": "B"}, "add": {"mana": -1, "storm": 1, "enters": 1}}},
         {"say": "With its trigger on the stack, tap Shrieking Drake to Earthcraft: untap the Island",
          "requires": {"zones": {"Shrieking Drake": "B", "Earthcraft": "B"}}, "causes": {"add": {"untapped_islands": 1}}},
         {"say": "The trigger returns Shrieking Drake to your hand",
          "requires": {"zones": {"Shrieking Drake": "B"}}, "causes": {"moves": {"Shrieking Drake": "H"}, "add": {"leaves": 1}}}]},
]


class _Counts(dict):
    def __missing__(self, key):                     # a count nobody set yet is 0
        return 0


def _flat(d, prefix=""):
    """{"counters": {"Walking Ballista": 1}} -> {"counters_Walking_Ballista": 1}"""
    out = {}
    for k, v in (d or {}).items():
        key = re.sub(r"\W+", "_", f"{prefix}{k}").strip("_")
        if isinstance(v, dict):
            out.update(_flat(v, key + "_"))
        else:
            out[key] = v
    return out


def _value(x, counts):
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, (int, float)):
        return x
    expr = re.sub(r"[A-Za-z_][\w ]*?(?=\s*[-+*/)]|$)", lambda m: re.sub(r"\W+", "_", m.group(0).strip()), str(x).strip())
    if not re.fullmatch(r"[\w\s+\-*/().]+", expr):
        raise ValueError(f"bad expression {x!r}")
    return eval(expr, {"__builtins__": {}}, _Counts(counts))   # simple arithmetic over the counts only


def walk(trace, zones, laps=3, _seeded=None):
    """Walk the loop; a loop can be entered at any step, so if the written order fails, try starting elsewhere.
    A loop that completes one lap but can't start another is a one-shot (a combo that wins in one go)."""
    best = None
    k0 = int(trace.get("repeat_from") or 0)
    lap_steps = trace["steps"][k0:]
    for r in range(max(len(lap_steps), 1)):
        t = dict(trace, steps=trace["steps"][:k0] + lap_steps[r:] + lap_steps[:r])
        res = _walk_seeded(t, zones, laps, _seeded)
        if res[1] != "fails":
            if r:
                res = (res[0] + f"; entered at step {k0 + r + 1}", res[1], res[2])
            return res
        best = best or res
    verdict, kind, table = best
    if kind == "fails" and re.match(r"lap 2,", verdict) and table:
        return "one lap works but it can't repeat: " + verdict, "one-shot", table
    return best


def _walk_seeded(trace, zones, laps=3, _seeded=None):
    """Walk with the translation's start; if the setup or lap 1 is short of a count the loop replenishes, retry
    with that much more at the start and report it as a prerequisite."""
    verdict, kind, table = _walk(trace, zones, laps)
    seeded = dict(_seeded or {})
    for _ in range(6):
        m = re.search(r"lap [01], step \d+ .*?: needs ([\d.]+) (.+?), has ([\d.]+)$", verdict) if kind == "fails" else None
        if not m:
            break
        need, name, have = float(m.group(1)), m.group(2).replace(" ", "_"), float(m.group(3))
        seeded[name] = seeded.get(name, 0) + (need - have)
        t2 = dict(trace, start={**_flat(trace.get("start")), **{k: _flat(trace.get("start")).get(k, 0) + v for k, v in seeded.items()}})
        verdict, kind, table = _walk(t2, zones, laps)
    if seeded and kind != "fails":
        verdict += "; needs to start with " + ", ".join(f"{v:g} {k.replace('_', ' ')}" for k, v in seeded.items())
    return verdict, kind, table


def _walk(trace, zones, laps=3):
    """Walk a translated combo. Returns (verdict, kind, table): kind is infinite/near-infinite/not sustainable/fails."""
    zone = dict(zones)
    n = {k: _value(v, {}) if not isinstance(v, (int, float)) else v for k, v in _flat(trace.get("start")).items()
         if not isinstance(v, str) or re.fullmatch(r"[\d\s+\-*/().]+", v)}
    for k, v in {"life": 40, "opponent_life": 40, "library": 60, "opponent_library": 99}.items():
        n.setdefault(k, v)                       # finite pools the translation didn't set: sensible defaults
    steps = trace["steps"]
    k0 = int(trace.get("repeat_from") or 0)
    table = []

    def step(i, s, lap):
        for card, z in ((s.get("requires") or {}).get("zones") or {}).items():
            if card not in zone and card not in zones:
                zone[card] = z                    # a generic object ("a nonland permanent"): it starts where it's first needed
            if zone.get(card) != z:
                return f"lap {lap}, step {i + 1} ({s.get('say', '')}): {card} must be in the {ZONES.get(z, z)}, but it's in the {ZONES.get(zone.get(card), zone.get(card) or 'nowhere')}"
        for c, at_least in _flat((s.get("requires") or {}).get("at_least")).items():
            if n.get(c, 0) < _value(at_least, n):
                return f"lap {lap}, step {i + 1} ({s.get('say', '')}): needs {at_least} {c.replace('_', ' ')}, has {n.get(c, 0)}"
        c = s.get("causes") or {}
        for t in c.get("transfer") or []:
            amt = n.get(t["from"], 0) if t.get("amount") == "all" else _value(t.get("amount", 0), n)
            n[t["from"]] = n.get(t["from"], 0) - amt
            n[t["to"]] = n.get(t["to"], 0) + amt
        deltas = {k: _value(v, n) for k, v in _flat(c.get("add")).items()}
        for k, v in deltas.items():
            n[k] = n.get(k, 0) + v
            if k in FINITE:
                n[k] = max(n[k], 0)
        zone.update({k: v for k, v in (c.get("moves") or {}).items() if isinstance(v, str)})
        return None

    for i, s in enumerate(steps[:k0]):
        err = step(i, s, 0)
        if err:
            return err, "fails", table
    start_zone = dict(zone)
    for lap in range(1, laps + 1):
        for i, s in enumerate(steps[k0:], k0):
            err = step(i, s, lap)
            if err:
                return err, "fails", table          # (laps already completed stay in the table)
        table.append({"lap": lap, "zones": dict(zone), "counts": dict(n)})
    real = set(zones)                            # only the combo's own cards must return; generic objects don't count
    if any(zone.get(c) != start_zone.get(c) for c in real):
        moved = [c for c in real if zone.get(c) != start_zone.get(c)]
        return f"the cards don't return: {', '.join(moved)}", "fails", table
    a, b = table[-2]["counts"], table[-1]["counts"]
    per = {k: b.get(k, 0) - a.get(k, 0) for k in set(a) | set(b)}
    per = {k: (int(v) if float(v).is_integer() else round(v, 2)) for k, v in per.items()}
    rising = {k: v for k, v in per.items() if v > 0}
    falling = {k: v for k, v in per.items() if v < 0}
    lost = {k: v for k, v in falling.items() if k not in FINITE}
    gains = ", ".join(f"+{v} {k.replace('_', ' ')}" for k, v in sorted(rising.items()))
    if lost:
        return f"every card returns, but each lap loses {', '.join(f'{-v} {k}' for k, v in lost.items())}", "not sustainable", table
    if falling:
        pools = [k.replace('_', ' ') for k in sorted(falling)]
        return f"every card returns; per lap {gains or 'nothing gained'}; ends when {' and '.join(pools)} run{'s' if len(pools) == 1 else ''} out", "near-infinite", table
    return f"every card returns; per lap {gains or 'nothing the translation records (its gains, like triggers or tokens, are not written down)'}", "infinite", table


# --- the translator (an LLM turns Spellbook's sentences into State steps; the walk verifies them) -----------------
SYSTEM = """You translate the steps of a known Magic: The Gathering combo into State steps that a program can walk.
State = each card's zone (B battlefield, G graveyard, H hand, E exile, L library, S stack) plus named counts
(mana, life, opponent_life, hand, graveyard, library, opponent_library, storm, untapped_lands, counters_<card>, tokens...).
Each step: {"say": short sentence, "requires": {"zones": {card: zone}, "at_least": {count: number}},
"causes": {"moves": {card: zone}, "add": {count: number or arithmetic over counts like "-3*(storm-2)"},
"transfer": [{"from": count, "to": count, "amount": number or "all"}]}}.
Give "start" counts that satisfy the prerequisites, and "repeat_from" (0-based) if setup steps happen once.
One lap of the steps must bring every card back to its starting zone. Describe only what the steps and cards say.
Answer only by calling state_steps."""
TOOL = {"name": "state_steps", "description": "The combo as State steps.",
        "input_schema": {"type": "object", "properties": {"start": {"type": "object"}, "repeat_from": {"type": "integer"},
                                                          "steps": {"type": "array", "items": {"type": "object"}}},
                         "required": ["start", "steps"]}}


def build(store, variants, out="combo_steps_requests.jsonl"):
    import relate
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    data = json.load(open("demo_data.json"))
    ids = {c["spellbookId"] for d in data["decks"] for c in d["combos"] if c.get("spellbookId")}
    db = relate.connect(store)
    n = 0
    with open(out, "w") as f:
        for line in gzip.open(variants, "rt"):
            v = json.loads(line)
            if v["id"] not in ids or not v.get("steps"):
                continue
            names = [u["card"]["name"] for u in v["uses"]]
            texts = {r["n"]: r["t"] for r in db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.oracleText AS t", {"n": names})}
            zones = {u["card"]["name"]: (u.get("zones") or ["B"])[0] for u in v["uses"]}
            prompt = ("Cards and starting zones:\n" + "\n".join(f"- {c} [{zones[c]}]: {texts.get(c, '')}" for c in names) +
                      f"\nPrerequisites: {v.get('prereq') or 'none'}\nMana needed: {v.get('mana') or 'none'}\nSteps:\n" +
                      "\n".join(f"{i}. {s}" for i, s in enumerate(v["steps"], 1)))
            f.write(json.dumps({"id": v["id"], "cards": names, "zones": zones, "prompt": prompt}) + "\n")
            n += 1
    print(f"{n} known combos from the sample decks -> {out}")


def run(model, path="combo_steps_requests.jsonl", out="combo_steps.jsonl"):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY isn't set in this terminal. Run:  export ANTHROPIC_API_KEY=your-key")
    import anthropic
    client = anthropic.Anthropic()
    done = {json.loads(l)["id"] for l in open(out)} if os.path.exists(out) else set()
    with open(out, "a") as f:
        for r in map(json.loads, open(path)):
            if r["id"] in done:
                continue
            try:
                msg = client.messages.create(model=model, max_tokens=4000, system=SYSTEM, tools=[TOOL], tool_choice={"type": "auto"},
                                             messages=[{"role": "user", "content": r["prompt"] + "\n\nAnswer only by calling state_steps."}])
                use = next((b for b in msg.content if b.type == "tool_use"), None)
            except Exception as err:
                print(f"  error on {r['id']}: {str(err)[:150]}"); continue
            if use:
                f.write(json.dumps({"id": r["id"], "cards": r["cards"], "zones": r["zones"], **use.input}) + "\n"); f.flush()
                print(f"translated {r['id']}: {' + '.join(r['cards'])}")


def verify(paths=("combo_steps.jsonl",), out="combo_traces.json"):
    zones_by_id = {}
    for line in gzip.open("variants_slim.jsonl.gz", "rt"):
        v = json.loads(line)
        zones_by_id[v["id"]] = {u["card"]["name"]: (u.get("zones") or ["B"])[0] for u in v["uses"]}
    traces = [dict(t, fixture=True) for t in FIXTURES]
    for p in paths:
        if os.path.exists(p):
            traces += [json.loads(l) for l in open(p)]
    results, kinds = {}, {}
    for t in traces:
        zones = t.get("zones") or zones_by_id.get(t["id"], {})
        try:
            verdict, kind, table = walk(t, zones)
        except Exception as err:
            verdict, kind, table = f"couldn't walk: {err}", "fails", []
        results[t["id"]] = {"kind": kind, "verdict": verdict, "steps": [s.get("say", "") for s in t["steps"]],
                            "start": t.get("start"), "zones": zones, "table": table, "fixture": bool(t.get("fixture"))}
        kinds[kind] = kinds.get(kind, 0) + 1
        print(f"{kind:15} {t.get('name') or ' + '.join(t.get('cards', []))}: {verdict}")
    json.dump(results, open(out, "w"), indent=1, ensure_ascii=False)
    print(f"\n{len(results)} combos walked: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) + f" -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fixtures", "build", "run", "verify"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    a = ap.parse_args()
    if a.cmd == "fixtures":
        verify(paths=())
    elif a.cmd == "build":
        build(*a.args[:2])
    elif a.cmd == "run":
        run(a.model)
    else:
        verify()
