"""Deck analysis over the ability graph: loops, what they cost, what they produce.

    python engine.py mtg.rl          # runs the acceptance checks

Relate has no graph-algorithm library, so the deck's subgraph is pulled with
Cypher and cycles are found in Python. A deck is small (<= 100 cards), so this
is fast.
"""
import re
import json
import os
import time
import sys
from collections import defaultdict

import relate

OBSERVER_EVENTS = {"creature_enters", "creature_leaves", "creature_dies"}
REENTER_UNTAPS = True
STATE_WALK = os.environ.get("MTG_STATES", "1") != "0"   # the state walk (held-by, zones); MTG_STATES=0 turns it off
STATE_WALK_EFFORT = int(os.environ.get("MTG_STATE_EFFORT", "400"))   # effect steps per walk before it gives up
STATE_STATS = {"walked": 0, "rejected": 0, "undecided": 0}     # the state walk (held-by, zones): on with MTG_STATES=1 while it matures
RESOURCE_EVENTS = {"counters": ("counters_placed", "self_counters_placed"),      # (creature -> "a creature enters" connected far too much)
                   "lifegain": ("gain_life",), "life_loss_opponents": ("opponent_loses_life",),
                   "damage_players": ("opponent_loses_life", "deals_damage", "self_deals_damage"), "card_draw": ("draw",), "token": ("token_created",)}



# --- Mechanics taught by users ("Teach a mechanic" tab) ----------------------------------------
# custom_rules.json holds rules like: cards whose text matches "you may cast ~ from your graveyard"
# get an ability that costs their mana value, produces a creature, and makes them enter again.
# They apply as an in-memory layer whenever a deck's graph is loaded: no rebuild needed.
RULES_FILE = os.environ.get("MTG_RULES", "custom_rules.json")
RULES_OVERRIDE = None          # set by the server to test a rule before saving it


def custom_rules():
    if RULES_OVERRIDE is not None:
        return RULES_OVERRIDE
    try:
        return json.load(open(RULES_FILE, encoding="utf-8")).get("rules", [])
    except (OSError, ValueError):
        return []


SELF_WORDS = re.compile(r"\bthis (?:card|creature|permanent|artifact|enchantment|spell)\b", re.I)


def rule_regex(rule):
    """A rule's wording, written in plain English or as a regular expression. "This card", "this creature"
    and the card's own name all mean the same thing (old Oracle text names the card; new text says "this card")."""
    pat = SELF_WORDS.sub("~", rule["match"])
    try:
        return re.compile(pat, re.I)
    except re.error:
        return re.compile(re.escape(pat), re.I)


def apply_custom_rules(db, g, deck):
    rules = custom_rules()
    if not rules:
        return 0
    from tag_functions import normalise
    A = g["abilities"]
    rows = list(db.execute("""MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS card, c.oracleText AS text, c.cmc AS cmc,
                              c.types AS cardTypes, c.typeLine AS typeLine, c.manaCost AS manaCost, c.colorIdentity AS identity,
                              c.power AS cardPower""", {"n": list(deck)}))
    added = 0
    for rule in rules:
        rx = rule_regex(rule)
        needs = (rule.get("needs") or "").strip().lower()       # "as long as you control a Zombie"
        for c in rows:
            if needs and not any(needs in (o["typeLine"] or "").lower() for o in rows if o["card"] != c["card"]):
                continue
            text = SELF_WORDS.sub("~", normalise({"name": c["card"], "text": c["text"] or ""}))
            m = rx.search(text)
            if not m:
                continue
            sentence = next((s for s in re.split(r"(?<=[.])\s", text) if rx.search(s)), m.group(0))
            aid = f"rule:{rule['name']}:{c['card']}"
            A[aid] = {"id": aid, "card": c["card"], "kind": rule.get("kind", "Activated"), "text": f"{rule['name']}: {sentence}",
                      "labels": ["Ability", rule.get("kind", "Activated")], "cardTypes": c["cardTypes"], "typeLine": c["typeLine"],
                      "manaCost": c["manaCost"], "identity": c["identity"], "cardPower": c["cardPower"], "cardMV": c["cmc"],
                      "source": "user rule"}
            amt = lambda x: int(c["cmc"] or 0) if str(x.get("amount")) == "mana value" else int(x.get("amount") or 1)
            g["TRIGGERED_BY"][aid] = [(ev, {}) for ev in rule.get("trigger", [])]
            if any(x.get("resource") == "tap" for x in rule.get("costs", [])):
                A[aid]["tapCost"] = True                     # "{T}: ...": repeats only if something untaps it
            g["COSTS"][aid] = [(x["resource"], {"amount": amt(x)}) for x in rule.get("costs", []) if x.get("resource") != "tap" and amt(x) > 0]
            g["PRODUCES"][aid] = [(x["resource"], {"amount": amt(x)}) for x in rule.get("produces", [])]
            g["RETRIGGERS"][aid] = [(x["event"], {"scope": x.get("scope", "self"), "immediate": True}) for x in rule.get("causes", [])]
            added += 1
    return added


# --- Modeled edges (Teach: model a combo by drawing it) -------------------------------------------------
MODEL_FILE = "custom_model.json"
MODEL_DRAFT = None            # the edges being tested in the editor (the server sets it around a test)
MODEL_DRAFT_CARDS = []        # ...and the cards picked for it


def saved_models():
    try:
        return json.load(open(MODEL_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def resource_events(g, res):
    """The events making this resource counts as: built in ("lifegain" -> "whenever you gain life"), plus any
    drawn in the Teach editor ("counts as")."""
    return tuple(RESOURCE_EVENTS.get(res, ())) + tuple(sorted(g.get("COUNTS_AS", {}).get(res, ())))


def apply_model(g):
    """Add drawn relationships (saved models plus the draft being tested) to a deck's graph, so every check sees
    them. Each edge: {s: ability id, t: Event/Resource name or "s:<state>", type, p: {amount, scope, who, by}}."""
    A = g["abilities"]
    drawn = [(x, m.get("cards") or []) for m in saved_models() for x in m.get("edges", [])] + [(x, MODEL_DRAFT_CARDS) for x in MODEL_DRAFT or []]
    for e, model_cards in drawn:
        a, t, typ, p = e.get("s"), str(e.get("t", "")), e.get("type"), e.get("p") or {}
        if typ == "FIRES" and a and t:                # a resource (or event) that counts as an event: no ability
            src = str(a).split(":", 1)[1] if str(a)[:2] in ("n:", "r:", "e:") else str(a)
            g.setdefault("COUNTS_AS", {}).setdefault(src, set()).add(t)
            continue
        if a not in A or not t:
            continue
        name = t[2:] if t.startswith("s:") else t
        A[a]["modeled"] = True                        # drawn by the user: their statement of what the card does
        A[a]["modelCards"] = sorted(set(A[a].get("modelCards", [])) | {c for c in model_cards if c})   # the combo it belongs to
        A[a]["temporary"] = False
        have = {"TRIGGERED_BY": g["TRIGGERED_BY"], "RETRIGGERS": g["RETRIGGERS"], "PRODUCES": g["PRODUCES"], "COSTS": g["COSTS"]}.get(typ)
        if have is not None and any(x == name for x, _ in have.get(a, [])):
            continue                                  # already in the graph (baked, or the graph knew it): don't count it twice
        if typ.endswith("_STATE") and any(r[0] == typ and r[1] == name for r in g.get("STATES", {}).get(a, [])):
            continue
        if typ == "TRIGGERED_BY":
            g["TRIGGERED_BY"].setdefault(a, []).append((name, {}))
        elif typ == "RETRIGGERS":
            g["RETRIGGERS"].setdefault(a, []).append((name, {"scope": p.get("scope") or "any"}))
        elif typ in ("PRODUCES", "COSTS"):
            g[typ].setdefault(a, []).append((name, {"amount": int(p.get("amount") or 1)}))
            if typ == "PRODUCES" and name == "mana":    # which colours the drawn mana is: "any" (the default) or e.g. "UR"
                cols = str(p.get("colors") or "any").upper()
                A[a]["modelColors"] = sorted(set(COLOURS) if cols == "ANY" else {c for c in cols if c in COLOURS})
        elif typ in ("REQUIRES_STATE", "CAUSES_STATE", "CLEARS_STATE"):
            g.setdefault("STATES", {}).setdefault(a, []).append((typ, name, p.get("who") or "", p.get("by") or "", ""))


def explain_no_loop(db, cards, limit=6):
    """Why the chosen cards don't loop: for cycles among them, the check each one fails, in the words of the
    comment at that check ("can't sustain itself", "the states don't come back where they started")."""
    import linecache
    import sys
    g = deck_subgraph(db, cards)
    A = g["abilities"]
    out, tried = [], 0
    one_step = [[a] for a in A if A[a].get("modeled") and A[a]["kind"] == "Activated"]
    for cyc in one_step + list(cycles(causal_edges(g), 4)):
        tried += 1
        if tried > 300 or len(out) >= limit:
            break
        if len({A[a]["card"] for a in cyc}) < 2 and not (len(cyc) == 1 and A[cyc[0]].get("modeled")):
            continue
        last = {}

        def tracer(frame, event, arg):
            if frame.f_code.co_name == "evaluate" and event == "line":
                last["line"] = frame.f_lineno
            return tracer
        sys.settrace(tracer)
        try:
            ok = evaluate(g, list(cyc))
        finally:
            sys.settrace(None)
        if ok:
            continue
        ln, why = last.get("line", 0), ""
        for k in range(ln, max(ln - 4, 0), -1):           # the check's own comment, or the comment just above it
            src = linecache.getline(__file__, k)
            if "#" in src:
                why = src.split("#", 1)[1].strip()
                break
        out.append({"steps": [{"card": A[a]["card"], "text": (A[a]["text"] or "")[:140]} for a in cyc],
                    "reason": why or "a rule check failed"})
    if not out:
        out.append({"steps": [], "reason": "no cycle at all: nothing these cards do sets off another of their abilities. "
                                          "Draw what each ability makes (PRODUCES), needs (COSTS) or sets off (RETRIGGERS)."})
    return out


COPY_ACTIVATED = re.compile(r"gains? all activated abilities of (?:another )?target creature", re.I)


def add_copied_abilities(g):
    """Copying activated abilities (Quicksilver Elemental: "{U}: gains all activated abilities of target creature").
    Each time it gains them it's a new ability, so the copied ability's "activate only once each turn" doesn't carry
    over. For each creature it can copy, add an ability on the copier: the copier's cost plus the copied cost,
    producing what the copied ability produces. The copied card is part of the combo (it has to be on the battlefield)."""
    A = g["abilities"]
    for c in [a for a in list(A) if A[a]["kind"] == "Activated" and COPY_ACTIVATED.search(A[a]["text"] or "")]:
        for a in list(A):
            x = A[a]
            if x["card"] == A[c]["card"] or x["kind"] != "Activated" or "#copies#" in a or COPY_ACTIVATED.search(x["text"] or ""):
                continue
            if "Creature" not in (x.get("cardTypes") or []) or not (g["PRODUCES"].get(a) or g["RETRIGGERS"].get(a)):
                continue
            if re.search(r"from your (?:hand|graveyard)|exile this card|discard this card", x["text"] or "", re.I):
                continue                                  # works only from the hand or graveyard: the copier can't pay it
            nid = f"{c}#copies#{a}"
            text = re.sub(r"\s*activate only[^.]*\.", "", x["text"] or "", flags=re.I).strip()
            text = text.replace("~", x["card"].split(" // ")[0])          # X is Vivi's power, not the copier's
            variable = "where x is" in text.lower()
            A[nid] = {**x, "id": nid, "card": A[c]["card"], "kind": "Activated", "oncePerTurn": False, "temporary": False,
                      "copiedFrom": x["card"],
                      "text": f"gains {x['card'].split(',')[0]}'s ability: {text}" + (" (X counted as 2: needs X of 2 or more)" if variable else "")}
            g["COSTS"][nid] = list(g["COSTS"].get(c, [])) + list(g["COSTS"].get(a, []))
            prods = []
            for res, p in g["PRODUCES"].get(a, []):
                p = dict(p)
                if res == "mana" and (p.get("variable") or variable):
                    p["amount"] = max(p.get("amount") or 0, 2)         # the least X that makes a {U} copy pay off
                prods.append((res, p))
            g["PRODUCES"][nid] = prods
            for k in ("RETRIGGERS", "MODIFIES", "TRIGGERED_BY"):
                if g[k].get(a):
                    g[k][nid] = list(g[k][a])
            if g.get("STATES") is not None and g["STATES"].get(a):
                g["STATES"][nid] = list(g["STATES"][a])


def deck_subgraph(db, deck):
    q = lambda s: list(db.execute(s, {"deck": deck}))
    abil = q("""MATCH (c:Card)-[h:HAS_ABILITY|GRANTS]->(a:Ability) WHERE c.name IN $deck
                RETURN c.name AS card, c.types AS cardTypes, c.typeLine AS typeLine, c.manaCost AS manaCost, c.colorIdentity AS identity,
                       c.power AS cardPower, c.cmc AS cardMV, a.id AS id, labels(a) AS labels, a.text AS text, a.limitedBy AS limitedBy, a.oncePerTurn AS oncePerTurn, a.temporary AS temporary, a.consumesSource AS consumesSource,
                       a.manaValue AS manaValue, a.tapCost AS tapCost, type(h) = 'GRANTS' AS granted""")
    rel = lambda r: q(f"""MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability)-[x:{r}]->(t)
                          WHERE c.name IN $deck RETURN a.id AS id, t.name AS target, properties(x) AS props""")
    # A land's plain "{T}: add mana" isn't a combo piece; untapping lands already counts as mana.
    abil = [a for a in abil if not re.match(r"^\{t\}: add ", a["text"] or "")]
    g = {"abilities": {a["id"]: {**a, "kind": next(l for l in a["labels"] if l != "Ability")} for a in abil}}
    g["identity"] = sorted({c for a in abil for c in (a.get("identity") or [])})
    # States (states.py): what each ability requires, causes and clears, for the state walk
    g["FROM_GRAPH"] = {a["id"] for a in abil}            # exactly the abilities the graph returned (their States are complete)
    g["STATES"] = defaultdict(list)
    for r in ("REQUIRES_STATE", "CAUSES_STATE", "CLEARS_STATE"):
        for row in rel(r):
            p = row["props"] or {}
            g["STATES"][row["id"]].append((r, row["target"], p.get("who", ""), p.get("by", ""), p.get("via", "")))
    for r in ("TRIGGERED_BY", "RETRIGGERS", "PRODUCES", "COSTS", "MODIFIES"):
        g[r] = defaultdict(list)
        for row in rel(r):
            if row["id"] in g["abilities"]:          # skip links of filtered-out abilities
                g[r][row["id"]].append((row["target"], row["props"] or {}))
    g["customAbilities"] = apply_custom_rules(db, g, deck)
    add_copied_abilities(g)
    apply_model(g)
    # Dying is a way of leaving the battlefield: "dies" also fires "leaves the battlefield" triggers.
    for a, rts in list(g["RETRIGGERS"].items()):
        have = {ev for ev, _ in rts}
        for ev, p in list(rts):
            implied = {"self_dies": "self_leaves", "creature_dies": "creature_leaves"}.get(ev)
            if implied and implied not in have:
                rts.append((implied, p))
                have.add(implied)
    return g


def can_return(returner_text, card):
    """Reveillark returns creatures "with power 2 or less"; Sun Titan permanents "with mana value 3 or less"."""
    t = returner_text or ""
    p = re.search(r"with power (\d+) or less", t)
    if p and str(card.get("cardPower") or "").isdigit() and int(card["cardPower"]) > int(p.group(1)):
        return False
    m = re.search(r"with (?:mana value|converted mana cost) (\d+) or less", t)
    if m and card.get("cardMV") is not None and float(card["cardMV"]) > int(m.group(1)):
        return False
    return True



TYPE_WORDS = r"(?:artifact|enchantment|creature|planeswalker|land|permanent)s?"
COPY_OF_ENTERED = re.compile(r"copy of (?:that creature|it\b|that permanent|the creature)", re.I)
LIBRARY_DIG = re.compile(r"look at the top|reveal the top|search your library|from among (?:them|those cards)", re.I)
CHANGES_ON_RETURN = re.compile(r"return (?:it|~|this card|that card) to the battlefield (?:transformed|attached)|returns? [^.]*transformed|as an aura|"
                               r"it's an aura|it's an enchantment|aura enchantment with enchant|"
                               r"if it had no counters on it", re.I)


def text_target_types(text):
    """Types an effect can reach, read from its text: "sacrifice a creature" (Phyrexian Altar), "exile target artifact
    or enchantment" (Leonin Relic-Warder). None means no restriction was found."""
    t = (text or "").lower()
    found = set()
    for m in re.finditer(rf"(?:sacrifice (?:a|an|another|one|two) (?:other )?|exile (?:up to (?:one|two|\w+) )?(?:another |other )?target (?:non-?\w+ )?)"
                         rf"({TYPE_WORDS}(?:(?:,|,? or|,? and/or) {TYPE_WORDS})*)", t):
        found |= {w.rstrip("s") for w in re.findall(r"artifact|enchantment|creature|planeswalker|land|permanent", m.group(1))}
    return None if not found or "permanent" in found else found


CARD_EXILE_TYPES = {}          # card -> the types its exile effect reaches (set per deck graph)


def reaches(text, target, card=None):
    allowed = text_target_types(text)
    if allowed is None and card and re.search(r"exiled card|cards? it exiled|the exiled|return (?:that card|those cards)", (text or "").lower()):
        allowed = CARD_EXILE_TYPES.get(card)       # a return only brings back what this card exiled
    return allowed is None or bool({t.lower() for t in (target.get("cardTypes") or [])} & allowed)

CARD_EXILE_EXCLUDED = {}       # card -> types its exile can't touch ("non-Angel"), for its "return the exiled" ability

NON_TYPE = r"(?:target|any number of|other|each|up to (?:one|two|\w+)) non-?([a-z]+) (?:creatures?|permanents?|artifacts?)"


def excluded_type(text, target, card=None):
    """"target non-Angel creature": True when the target has the excluded type. A "return the exiled card" ability
    inherits its card's exile restriction: Glorious Protector never exiled an Angel, so it can't return one."""
    low = (text or "").lower()
    if card and re.search(r"exiled card|cards? it exiled|the exiled|return (?:that card|those cards)", low):
        if any(w in (target.get("typeLine") or "").lower() for w in CARD_EXILE_EXCLUDED.get(card, ())):
            return True
    for m in re.finditer(NON_TYPE, low):
        if m.group(1) not in ("token", "land", "legendary") and m.group(1) in (target.get("typeLine") or "").lower():
            return True
    return False


def causal_edges(g):
    CARD_EXILE_TYPES.clear()
    CARD_EXILE_EXCLUDED.clear()
    for x in g["abilities"].values():
        low = (x["text"] or "").lower()
        if re.search(r"\bexile\b", low):
            ex = {m.group(1) for m in re.finditer(NON_TYPE, low) if m.group(1) not in ("token", "land", "legendary")}
            if ex:
                CARD_EXILE_EXCLUDED[x["card"]] = CARD_EXILE_EXCLUDED.get(x["card"], set()) | ex
    for x in g["abilities"].values():
        ty = text_target_types(x["text"]) if re.search(r"\bexile\b", (x["text"] or "").lower()) else None
        if ty:
            CARD_EXILE_TYPES[x["card"]] = CARD_EXILE_TYPES.get(x["card"], set()) | ty
    """a -> b when a's effect fires b's trigger (respecting self/other scope),
    or when a produces mana that b's activation costs."""
    A = g["abilities"]
    edges = defaultdict(set)
    triggered_by = defaultdict(list)
    for b, evs in g["TRIGGERED_BY"].items():
        for ev, _ in evs:
            triggered_by[ev].append(b)
    for src, dsts in g.get("COUNTS_AS", {}).items():      # drawn in Teach: an event that counts as another
        for d in dsts:
            triggered_by[src] += [b for b in triggered_by.get(d, []) if b not in triggered_by[src]]
    for a, rts in g["RETRIGGERS"].items():
        for ev, p in rts:
            if not p.get("immediate", True):
                continue
            for b in triggered_by.get(ev, []):
                same = A[a]["card"] == A[b]["card"]
                scope = p.get("scope", "any")
                # "exile target creature" can't make an enchantment leave: check what the effect may target.
                allowed = p.get("targetTypes")
                if allowed and "permanent" not in allowed and ev.startswith("self_") and \
                        not {t.lower() for t in (A[b].get("cardTypes") or [])} & set(allowed):
                    continue
                if ev == "self_enters" and not same and not can_return(A[a]["text"], A[b]):
                    continue
                if ev.startswith("self_") and not same and excluded_type(A[a]["text"], A[b], A[a]["card"]):
                    continue                  # Restoration Angel can't flicker an Angel
                if ev.startswith("self_") and not same and not reaches(A[a]["text"], A[b], A[a]["card"]):
                    continue                  # Phyrexian Altar can't sacrifice an enchantment; Relic-Warder can't exile a creature
                if ev in ("self_enters", "enters_from_graveyard") and not same and LIBRARY_DIG.search(A[a]["text"] or "") \
                        and "graveyard" not in (A[a]["text"] or "").lower():
                    continue                  # Ao's top-seven dig and Recruiter's tutor bring back nothing specific
                if ev in OBSERVER_EVENTS or scope in ("any", "granted") \
                        or (scope == "self" and same) or (scope in ("other", "copy") and not same):
                    edges[a].add(b)
                    g.setdefault("_fires", defaultdict(set))[b].add(a)       # a's effect fires b
                    fe = g.setdefault("_fire_ev", {})
                    if (a, b) not in fe or (fe[(a, b)].startswith("self_") and not ev.startswith("self_")):
                        fe[(a, b)] = ev          # a death everyone sees beats a targeted one
    for b, prods in g["PRODUCES"].items():
        for res, _ in prods:
            for a, costs in g["COSTS"].items():
                if any(r == res for r, _ in costs):
                    edges[b].add(a)
            # Resources that are also events: gaining life fires "whenever you gain life".
            for ev in resource_events(g, res):
                for t in triggered_by.get(ev, []):
                    if ev == "self_counters_placed" and A[t]["card"] != A[b]["card"] and not A[t].get("granted") and not A[b].get("granted"):
                        continue                  # "counters are put on THIS creature": its own counters only
                    if ev == "self_deals_damage" and A[t]["card"] != A[b]["card"] and not A[t].get("granted"):
                        continue                  # "this creature deals damage": its own damage only (or, for a
                                                  # granted ability, damage from the creature it's granted to)
                    edges[b].add(t)
            # Untapping pays for a tap cost; returning a card to hand lets a spell be cast again.
            if res == "untap":
                for a, x in A.items():
                    if x["kind"] == "Activated" and x.get("tapCost"):
                        edges[b].add(a)
            # Only returning an instant/sorcery card lets a spell be cast again (Archaeomancer),
            # not bouncing a permanent (Cyclonic Rift).
            if res in ("return_to_hand", "cast_from_graveyard") and re.search(r"instant|sorcery", A[b]["text"] or ""):
                for a, x in A.items():
                    if x["kind"] == "Spell" and g["COSTS"].get(a):
                        edges[b].add(a)
    # Whatever makes a creature enter fresh (a token, or one coming back) can pay "tap an untapped creature".
    tappers = [a for a in A if any(r == "untapped_creature" for r, _ in g["COSTS"].get(a, []))]
    if tappers:
        for b in A:
            if any(r == "creature" for r, _ in g["PRODUCES"].get(b, [])) or \
                    any(ev == "self_enters" and p.get("immediate", True) for ev, p in g["RETRIGGERS"].get(b, [])):
                for a in tappers:
                    if a != b:
                        edges[b].add(a)
    # Re-entering untaps a permanent: whatever makes a card enter again can reset its tap abilities
    # (Restoration Angel's copy flickering Kiki-Jiki).
    for b, rts in (g["RETRIGGERS"].items() if REENTER_UNTAPS else []):
        for ev, p in rts:
            if ev != "self_enters" or not p.get("immediate", True):
                continue
            allowed = p.get("targetTypes")
            for a, x in A.items():
                if x["kind"] == "Activated" and x.get("tapCost") and \
                        ((p.get("scope") == "self" and x["card"] == A[b]["card"]) or (p.get("scope") == "other" and x["card"] != A[b]["card"])) and \
                        (not allowed or "permanent" in allowed or {t.lower() for t in (x.get("cardTypes") or [])} & set(allowed)) and \
                        "Creature" in (x.get("cardTypes") or []):   # creatures only: mana rocks reset by flicker were mostly noise
                    edges[b].add(a)                      # a creature-only flicker can't reset an artifact's tap ability
    return edges


def cycles(edges, max_len=4):
    """Simple cycles (length 2..max_len); each found once, starting from its smallest id."""
    found = []
    def dfs(start, node, path):
        for nxt in edges.get(node, ()):
            if nxt == start:
                if len(path) > 1:
                    found.append(list(path))
            elif nxt > start and nxt not in path and len(path) < max_len:
                dfs(start, nxt, path + [nxt])
    for s in sorted(edges):
        dfs(s, s, [s])
    return found


def amount(props):
    return props.get("amount") or 0


def side_options(g, cycle):
    """Second creatures a two-target flicker in the cycle could also hit, if they make mana."""
    A = g["abilities"]
    for a in cycle:
        if re.search(r"\btwo target", A[a]["text"] or "") and any(ev == "self_enters" for ev, _ in g["RETRIGGERS"].get(a, [])):
            in_loop = {A[x]["card"] for x in cycle}
            return [b for b, evs in g["TRIGGERED_BY"].items()
                    if A[b]["card"] not in in_loop and any(ev == "self_enters" for ev, _ in evs)
                    and any(r == "mana" for r, _ in g["PRODUCES"].get(b, []))]
    return []


COLOURS = set("WUBRG")


def colours_ok(g, abilities):
    """Every coloured symbol the loop's costs need must be made by the loop.
    Untapping lands makes the deck's colours; Treasure or "any color" makes all of them."""
    A = g["abilities"]
    sym = lambda s: {x.upper() for x in re.findall(r"\{([^}]+)\}", s or "") if x.upper() in COLOURS}
    need, made, makes_mana = set(), set(), False
    for a in abilities:
        x, t = A[a], (A[a]["text"] or "")
        if x["kind"] == "Activated":
            need |= sym(t.split(":")[0])
        if x["kind"] == "Spell" or re.search(r"return ~ to (?:its owner's|your) hand", t) or \
                (x.get("source") == "user rule" and g["COSTS"].get(a)) or "castfromgraveyard" in a:   # casting it again needs its colours
            need |= sym(x.get("manaCost"))                   # cast it (again)
        pay = re.search(r"\bpay ((?:\{[^}]+\})+)", t)
        if x["kind"] == "Triggered" and pay:
            need |= sym(pay.group(1))
        if any(r == "mana" for r, _ in g["PRODUCES"].get(a, [])) or "untap" in t:
            makes_mana = True
            if re.search(r"treasure|any color|any one color|any type", t):
                made |= COLOURS
            if re.search(r"untap (?:up to )?(?:\w+ )?lands?|untap all lands", t):
                made |= set(g.get("identity") or COLOURS)
            made |= {c for s in re.findall(r"add ((?:\{[^}]+\})+)", t) for c in sym(s)}
            made |= set(x.get("modelColors") or [])     # a drawn mana edge says its colours
    return not need or not makes_mana or need <= made


GENERIC_WORDS = {"nontoken", "other", "another", "creature", "creatures", "token", "tokens", "nonland", "you", "control",
                 "a", "an", "one", "or", "more", "two", "three", "of", "your", "this", "that", "card", "cards", "permanent",
                 "permanents", "nonartifact", "noncreature", "untapped", "tapped", "attacking", "blocking", "and", "with"}
COLOUR_WORDS = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}


def requirements(a):
    """Creature types, colours or names an ability needs: "whenever an Egg you control dies",
    "sacrifice another Phyrexian", "a creature named Spitting Drake"."""
    t = (a["text"] or "").lower()
    words, names = set(), set()
    if a["kind"] == "Triggered":
        clause = t.partition(", ")[0]
        m = re.search(r"whenever (?:an?|another|one or more) (?:other )?((?:[a-z-]+ ){0,3}?)(?:creatures? )?(?:you control |cards? )?(?:dies|die|enters|enter)\b", clause)
        if m:
            words |= set(m.group(1).split()) - GENERIC_WORDS
    if a["kind"] == "Activated" and ":" in t:
        cost = t.split(":")[0]
        for m in re.finditer(r"sacrifice (?:another|two|three|a|an|x) (?:other )?([a-z-]+(?: [a-z-]+)?)", cost):
            words |= set(m.group(1).split()) - GENERIC_WORDS - {"creature", "artifact", "land", "enchantment"}
    names |= {n.strip() for n in re.findall(r"creature named ([^,:.]+)", t)}
    # ("as long as you control a Zombie" is checked against the whole deck: see deck_conditions)
    words = {w for w in words if not w.startswith("non")}    # "non-Spirit" excludes; it doesn't require
    return {w.rstrip("s") if len(w) > 3 else w for w in words}, names



# --- States: "held in exile by X" and zones --------------------------------------------------------
# Each step of a loop REQUIRES some states, CAUSES some and CLEARS some. A loop is real only if, walked in
# order from some starting arrangement of its few cards, every step's requirements hold and it ends exactly
# where it started. (Felidar Guardian flickering Lumbering Battlement can't be returned BY Battlement:
# Felidar was on the battlefield, not held by it.)
HOLDS = re.compile(r"until (?:~|it|this \w+) leaves the battlefield", re.I)
RETURN_HELD = re.compile(r"cards it exiled return|return the exiled cards?|the exiled card to the battlefield|implied by \"until", re.I)
FLICKER = re.compile(r"exile [^.]*?,? then return (?:it|that card|them|those cards|the exiled card)|exile [^.]*\. return (?:it|that card|them|those cards) to the battlefield", re.I)
FROM_GRAVEYARD = re.compile(r"(?:return|put) [^.]*from (?:your|a|their) graveyard (?:to|onto) the battlefield|return (?:this card|~) from your graveyard|"
                            r"(?:return|put) enchanted creature card (?:to|onto) the battlefield", re.I)
RETURN_THAT_CARD = re.compile(r"(?:dies|put into exile|leaves the battlefield)[^.]*, return that card to the battlefield", re.I)
SACRIFICE = re.compile(r"sacrific", re.I)


_WALKED = {}

STATE_SHORTCUT = True


def state_key(g, cycle):
    """The state-touching steps of a cycle, in order (rotated to a fixed start). Steps that touch no state only add
    requirements, so a larger cycle with the same state-touching steps can't pass a walk this one failed."""
    graph_ids = g.get("FROM_GRAPH") or set()
    rel = [a for a in cycle if g["STATES"].get(a) or a not in graph_ids]
    if not rel:
        return None
    i = rel.index(min(rel))
    return tuple(rel[i:] + rel[:i])



def state_walk(g, cycle):
    # remembered on the deck's own graph: keying by id(g) could hand one deck another deck's results,
    # because Python reuses an object's id once it's freed
    memo = g.setdefault("_WALKED", {})
    key = tuple(cycle)
    if key not in memo:
        try:
            ok = _state_walk(g, cycle)
            STATE_STATS["walked"] += 1
            STATE_STATS["rejected"] += not ok
        except _Undecided:                             # too much work: keep it (MTG_STATE_UNDECIDED=keep) or reject it
            ok = os.environ.get("MTG_STATE_UNDECIDED", "reject") == "keep"
            STATE_STATS["undecided"] += 1
        memo[key] = ok
    return memo[key]


class _Undecided(Exception):
    pass


def _state_walk(g, cycle):
    """Walk the loop in order, exploring which of its cards each effect targets. Each ability's REQUIRES_STATE,
    CAUSES_STATE and CLEARS_STATE come from the graph (states.py); the loop is real only if some starting
    arrangement satisfies every step, each next step is triggered by what actually happened, and it ends
    where it started."""
    A = g["abilities"]
    txt = lambda a: (A[a]["text"] or "").lower()
    card = lambda a: A[a]["card"]
    trig = lambda a: {ev for ev, _ in g["TRIGGERED_BY"].get(a, [])}
    STATES = g.get("STATES") or {}

    graph_ids = g.get("FROM_GRAPH") or set()
    cache = g.setdefault("_STATE_ROWS", {})

    def rows(a):
        if a not in cache:
            if a in graph_ids or a in STATES:
                cache[a] = STATES.get(a, [])              # from the graph: no rows means the ability touches no state
            else:
                import states                             # added at run time (taught mechanics): the same classifier
                holder = any(r[1] == "held" and r[3] == "self" for x in A.values() if x["card"] == card(a) for r in STATES.get(x["id"], []))
                cache[a] = [(rel, st, p.get("who", ""), p.get("by", ""), p.get("via", ""))
                            for rel, st, p in states.classify(A[a]["text"], A[a]["kind"], holder)]
        return cache[a]

    has_memo = g.setdefault("_STATE_HAS", {})

    def has(a, rel, st, who=None, by=None):
        key = (a, rel, st, who, by)                       # depends only on the ability: remember it
        if key not in has_memo:
            has_memo[key] = any(r == rel and s == st and (who is None or w == who) and (by is None or b == by) for r, s, w, b, _ in rows(a))
        return has_memo[key]

    # the walk matters only for loops that hold cards in exile, flicker, or return cards from the graveyard
    if not any(has(a, "CAUSES_STATE", "held") or has(a, "CLEARS_STATE", "held", by="self") or has(a, "CLEARS_STATE", "held", by="target")
               or has(a, "REQUIRES_STATE", "graveyard") for a in cycle):
        return True
    cards = sorted({card(a) for a in cycle})
    info = {c: next(x for x in A.values() if x["card"] == c) for c in cards}
    holders = sorted({x["card"] for x in A.values() if x["card"] in cards and
                      (has(x["id"], "CAUSES_STATE", "held", by="self") or has(x["id"], "CLEARS_STATE", "held", by="self"))})

    reach_memo = g.setdefault("_STATE_REACH", {})

    def targets(a, S, zone):
        """Cards this effect can reach, in the zone it needs. Whether it can reach a card never changes during a
        walk, only the card's zone does, so the reach is worked out once per ability and card."""
        out = []
        for c in cards:
            if S[c] != zone:
                continue
            key = (a, c)
            if key not in reach_memo:
                t = A[a]["text"]
                self_ok = not re.search(r"\banother\b|\bother\b", txt(a))
                reach_memo[key] = (c != card(a) or self_ok) and reaches(t, info[c], card(a)) and not excluded_type(t, info[c], card(a))
            if reach_memo[key]:
                out.append(c)
        return out

    FRESH = {}                                            # id(outcome state) -> (flickered card, cards it released)

    def release(S2, c):
        back = {z for z in cards if S2[z] == "held:" + c}
        for z in back:
            S2[z] = "bf"
        return back

    effort = [0]

    def effects(a, S, prev_left=frozenset(), fresh=None):
        """Every outcome of this ability, from its State relationships: (new state, entered, left)."""
        effort[0] += 1
        if effort[0] > STATE_WALK_EFFORT:
            raise _Undecided()
        me, outs = card(a), []
        blocked = fresh[1] if fresh and fresh[0] == me else set()     # 603.3d: targets chosen before they return
        if has(a, "CAUSES_STATE", "battlefield", who="copy"):        # Kiki-Jiki: the copy's enter trigger fires
            return [(dict(S), {c}, set()) for c in cards if S[c] == "bf" and c != me] or [(dict(S), set(), set())]
        if has(a, "REQUIRES_STATE", "graveyard", who="that card"):   # Kaya's Ghostform: the card that just died
            for c in [c for c in prev_left if S[c] in ("gy", "exile")]:
                S2 = dict(S); S2[c] = "bf"
                if S2.get(me) == "bf" and has(a, "CAUSES_STATE", "graveyard", who="self"):
                    S2[me] = "gy"
                outs.append((S2, {c}, set()))
            return outs or [(dict(S), set(), set())]
        if has(a, "CLEARS_STATE", "held", by="target"):              # flicker
            for c in targets(a, S, "bf"):
                S2 = dict(S); back = release(S2, c)
                FRESH[id(S2)] = (c, back)
                outs.append((S2, {c} | back, {c}))
        elif has(a, "CAUSES_STATE", "held", by="self"):              # exile until this card leaves
            for c in [c for c in targets(a, S, "bf") if c not in blocked]:
                S2 = dict(S); S2[c] = "held:" + me; back = release(S2, c)
                outs.append((S2, back, {c}))
        elif has(a, "CLEARS_STATE", "held", by="self"):              # this card left: what it held returns
            S2 = dict(S); back = release(S2, me)
            outs.append((S2, back, set()))
        elif has(a, "REQUIRES_STATE", "graveyard", who="self"):
            if S[me] == "gy":
                S2 = dict(S); S2[me] = "bf"; outs.append((S2, {me}, set()))
        elif has(a, "REQUIRES_STATE", "graveyard", who="target"):
            for c in targets(a, S, "gy"):
                S2 = dict(S); S2[c] = "bf"; outs.append((S2, {c}, set()))
        if has(a, "REQUIRES_STATE", "battlefield", who="sacrificed"):
            for c in [c for c in cards if S[c] == "bf" and "Creature" in (info[c].get("cardTypes") or []) and
                      (c != me or "sacrifice this" in txt(a) or "sacrifices it" in txt(a))]:
                S2 = dict(S); S2[c] = "gy"; back = release(S2, c)
                outs.append((S2, back, {c}))
        return outs or [(dict(S), set(), set())]

    def ok_actor(a, S, entered, left):
        tr = trig(a)
        if tr & {"self_enters"}:
            return card(a) in entered and S[card(a)] == "bf"
        if tr & {"self_leaves", "self_dies"}:
            # "whenever this creature OR ANOTHER creature dies" (Pawn of Ulamog): any card that just left will do
            if tr & {"creature_dies", "creature_leaves", "dies", "leaves"}:
                return bool(left)
            return card(a) in left
        if has(a, "REQUIRES_STATE", "graveyard", who="self"):
            return S[card(a)] == "gy"                     # activated or cast from the graveyard (Reassembling Skeleton)
        if A[a]["kind"] in ("Activated", "Static"):
            return S[card(a)] == "bf"
        return True                                       # watches others (a creature dies, enters...)

    import itertools
    # Enter abilities of the loop's cards that the cycle itself doesn't list (Fiend Hunter re-exiling Sun Titan):
    # they fire as side effects of a card entering, optionally ("you may").
    side_etb = {}
    for x in A.values():
        if x["card"] in cards and x["id"] not in cycle and "self_enters" in trig(x["id"]) and \
                has(x["id"], "CAUSES_STATE", "held", by="self"):
            side_etb.setdefault(x["card"], []).append(x["id"])

    def with_side_effects(outs):
        more = []
        for S2, ent, lft in outs:
            more.append((S2, ent, lft))
            for c in ent:
                for x in side_etb.get(c, []):
                    if S2.get(c) == "bf":
                        for S3, e3, l3 in effects(x, S2, fresh=FRESH.get(id(S2))):
                            more.append((S3, ent | e3, lft | l3))
        return more

    uses_gy = any(has(a, "REQUIRES_STATE", "graveyard") or has(a, "CAUSES_STATE", "graveyard") or
                  trig(a) & {"self_dies"} for a in cycle)
    # Only arrangements that could happen: a card starts held by X only if X's exile can reach it, and in the
    # graveyard only if the loop can put it there or take it from there.
    def can_hold(h, c):
        return any(x["card"] == h and has(x["id"], "CAUSES_STATE", "held", by="self") and
                   (c != h) and reaches(x["text"], info[c], h) and not excluded_type(x["text"], info[c], h) for x in A.values())

    def gy_ok(c):
        creature = "Creature" in (info[c].get("cardTypes") or [])
        return uses_gy and (creature or any(has(a, "REQUIRES_STATE", "graveyard") and (card(a) == c or c in targets(a, {z: "gy" for z in cards}, "gy"))
                                            for a in cycle))

    options = {c: ["bf"] + (["gy"] if gy_ok(c) else []) + ["held:" + h for h in holders if h != c and can_hold(h, c)] for c in cards}
    first = cycle[0]
    for combo in itertools.product(*(options[c] for c in cards)):
        start = dict(zip(cards, combo))
        # the first step was triggered by the last step of the previous lap: allow whatever its trigger needs
        # the first step was triggered by the previous lap's last step: if it reacts to a death (Kaya's Ghostform),
        # the cards that start in the graveyard are the ones that just died
        just_left = {card(first)} if trig(first) & {"self_leaves", "self_dies"} else set()
        if trig(first) & {"creature_dies", "creature_leaves", "dies", "leaves"}:
            just_left |= {c for c in cards if start[c] in ("gy", "exile")}
        frontier = [(start, {card(first)} if "self_enters" in trig(first) else set(), just_left)]
        recent_in, recent_out = set(), set()
        for i, a in enumerate(cycle):
            nxt = []
            for S, entered, left in frontier:
                # triggers from one event resolve over a couple of steps: look back across the last two
                if not ok_actor(a, S, entered | recent_in, left | recent_out):
                    continue
                nxt += with_side_effects(effects(a, S, left | recent_out, fresh=FRESH.get(id(S))))
            recent_in = set().union(*[f[1] for f in frontier]) if frontier else set()
            recent_out = set().union(*[f[2] for f in frontier]) if frontier else set()
            # different paths often reach the same arrangement: explore each one once
            uniq = {}
            for S2, e2, l2 in nxt:
                key = (tuple(sorted(S2.items())), frozenset(e2), frozenset(l2))
                if key not in uniq:
                    uniq[key] = (S2, e2, l2)
            frontier = list(uniq.values())[:300]
            if not frontier:
                break
        if any(S == start and ok_actor(first, S, entered | recent_in, left | recent_out) for S, entered, left in frontier):
            return True
        # Activated abilities in the loop (a sacrifice outlet) can be used a few extra times to restore the
        # start, as the balance engine already counts: Karmic Guide + Reveillark needs two sacrifices a lap.
        acts = [a for a in cycle if A[a]["kind"] == "Activated"]
        for S, entered, left in frontier:
            states = [S]
            for _ in range(2):
                states = [S3 for S2 in states for a in acts if S2[card(a)] == "bf" for S3, _, _ in effects(a, S2)]
                if any(S2 == start for S2 in states):
                    return True
    return False

def evaluate(g, cycle, side=None, mult=None):
    """Does the loop pay for itself, and what does it produce each time around?"""
    if STATE_WALK and STATE_SHORTCUT and state_key(g, cycle) in g.setdefault("_STATE_REJECTED", set()):
        return None                                       # the same state-touching steps already failed their walk
    A = g["abilities"]
    produced, spent = defaultdict(int), defaultdict(int)
    extra_cards = set()
    # Type, colour and name requirements must be met by cards (or tokens) the loop actually involves.
    loop_cards = {A[a]["card"] for a in cycle} | ({A[side]["card"]} if side else set())
    tokens_made = " ".join(str(p.get("tokenDesc") or "") for a in cycle for _, p in g["PRODUCES"].get(a, [])).lower()
    def described(cards):
        return " ".join((x.get("typeLine") or "") + " " + " ".join(COLOUR_WORDS.get(c, "") for c in (x.get("identity") or []))
                        for x in A.values() if x["card"] in cards).lower() + " " + tokens_made
    # Conditions on what you control ("as long as you control a Zombie") are met by any other card in the
    # deck; the first that qualifies is named as a helper.
    for a in cycle:
        for need in re.findall(r"as long as you control (?:a|an|another) ([a-z-]+)", (A[a]["text"] or "").lower()):
            helper = next((x["card"] for x in A.values() if x["card"] != A[a]["card"] and need.rstrip("s") in (x.get("typeLine") or "").lower()), None)
            if not helper:
                return None
            extra_cards.add(helper)
    for a in cycle:
        words, names = requirements(A[a])
        if not words and not names:
            continue
        others = described(loop_cards - {A[a]["card"]})       # the creature that dies or enters is another card
        if any(w not in others for w in words) or any(n not in {c.lower() for c in loop_cards} for n in names):
            return None
    # "Sacrifice this creature" in a cost (Cankerbloom, Twitching Doll): once, unless the loop brings the card back.
    self_sac = [a for a in cycle if A[a]["kind"] == "Activated" and ":" in (A[a]["text"] or "") and
                re.search(r"sacrifice (?:~|this creature|this artifact|this enchantment|this permanent)", (A[a]["text"] or "").lower().split(":")[0])]
    if self_sac and not all(any(ev == "self_enters" and not LIBRARY_DIG.search(A[x]["text"] or "") and
                                ((p.get("scope") == "self" and A[x]["card"] == A[a]["card"]) or
                                 (p.get("scope") in ("other", "any") and A[x]["card"] != A[a]["card"]))
                                for x in cycle for ev, p in g["RETRIGGERS"].get(x, [])) for a in self_sac):
        return None
    if any(CHANGES_ON_RETURN.search(A[a]["text"] or "") and any(ev == "self_enters" for ev, _ in g["RETRIGGERS"].get(a, []))
           for a in cycle):
        return None                           # back as an Aura or transformed, or "if it had no counters": once only
    if any(A[a].get("oncePerTurn") for a in cycle):
        return None                                      # "triggers only once each turn" can't repeat
    if any(A[a].get("temporary") for a in cycle):
        return None                                      # "gains ... until end of turn" doesn't survive a zone change
    doubled = {ev for evs in g["MODIFIES"].values() for ev, _ in evs}     # Teysa: death triggers twice
    extra_doublers = set()
    # Each spell in a loop needs its own returner (Archaeomancer returns one instant per trigger).
    spells = [a for a in cycle if A[a]["kind"] == "Spell"]
    returners = [a for a in cycle if A[a]["kind"] != "Spell" and re.search(r"instant|sorcery", A[a]["text"] or "")
                 and any(r in ("return_to_hand", "cast_from_graveyard") for r, _ in g["PRODUCES"].get(a, []))]
    if len(spells) > len(returners):
        return None
    # A flicker with two targets (Ghostly Flicker) also re-fires a second creature's enters ability.
    if side:
        for r, p in g["PRODUCES"].get(side, []):
            produced[r] += amount(p)
        extra_cards.add(A[side]["card"])
    # Mana colours: colourless mana from Ashnod's Altar can't pay Reassembling Skeleton's {B}.
    if not colours_ok(g, list(cycle) + ([side] if side else [])):
        return None
    untaps = any(r == "untap" and not re.search(r"untap (?:up to )?(?:\w+ )?lands?\b|untap all lands", A[a]["text"] or "")
                 for a in cycle for r, _ in g["PRODUCES"].get(a, []))
    def returns_spell_for(spell):
        # Another card's ability that returns an instant or sorcery to hand (Archaeomancer).
        return any(A[a]["card"] != A[spell]["card"] and re.search(r"instant|sorcery", A[a]["text"] or "")
                   and any(r in ("return_to_hand", "cast_from_graveyard") for r, _ in g["PRODUCES"].get(a, []))
                   for a in cycle)
    for a in cycle:
        types_a = {t.lower() for t in (A[a].get("cardTypes") or [])}
        reenters = REENTER_UNTAPS and "creature" in types_a and any(ev == "self_enters" and p.get("immediate", True) and
                       ((p.get("scope") == "self" and A[x]["card"] == A[a]["card"]) or (p.get("scope") == "other" and A[x]["card"] != A[a]["card"])) and
                       (not p.get("targetTypes") or "permanent" in p["targetTypes"] or types_a & set(p["targetTypes"]))
                       for x in cycle for ev, p in g["RETRIGGERS"].get(x, []))
        if A[a]["kind"] == "Activated" and A[a].get("tapCost") and not untaps and not reenters:
            return None                                  # once per untap: not a loop unless something untaps it
        if A[a]["kind"] == "Spell" and not returns_spell_for(a):
            return None                                  # a spell is gone once cast unless something returns it
        factor = 2 if any(ev in doubled for ev, _ in g["TRIGGERED_BY"].get(a, [])) else 1
        if factor == 2:     # the doubler (Teysa, Panharmonicon) is part of the combo
            extra_doublers.update(A[d]["card"] for d, evs in g["MODIFIES"].items()
                                  if any(ev in doubled and ev in {e for e, _ in g["TRIGGERED_BY"].get(a, [])} for ev, _ in evs))
        k = (mult or {}).get(a, 1)                       # how many times it fires per loop
        for res, p in g["PRODUCES"].get(a, []):
            produced[res] += amount(p) * factor * k
        for res, p in g["COSTS"].get(a, []):
            spent[res] += amount(p) * k
    # Mana reducers ("creature spells you cast cost {1} less"): static helpers that shrink CASTING costs in the
    # loop. Used only when the loop is short on mana, fewest first, and never below the coloured part.
    if produced.get("mana", 0) < spent.get("mana", 0):
        recast = re.compile(r"cast it again|return (?:~|this creature|a creature you control|another creature you control|target creature you control) to (?:its owner's|your) hand")
        casts = [a for a in cycle if any(r == "mana" for r, _ in g["COSTS"].get(a, [])) and
                 (A[a]["kind"] == "Spell" or "castfromgraveyard" in a or recast.search((A[a]["text"] or "").lower()))]
        reducers = []
        for x in A.values():
            if x["kind"] != "Static" or x["card"] in {A[a]["card"] for a in cycle}:
                continue
            m = re.search(r"(?:(creature|artifact|instant and sorcery|instant|sorcery|noncreature|enchantment|historic) )?spells (?:you cast )?cost \{(\d)\} less", (x["text"] or "").lower())
            if m and "opponents" not in (x["text"] or "").lower() and "your opponents cast" not in (x["text"] or "").lower():
                reducers.append((x["card"], int(m.group(2)), m.group(1)))
        def applies(kind, a):
            types = {t.lower() for t in (A[a].get("cardTypes") or [])}
            return kind is None or (kind == "noncreature" and "creature" not in types) or \
                (kind == "instant and sorcery" and types & {"instant", "sorcery"}) or kind in types or \
                (kind == "historic" and (types & {"artifact"} or "legendary" in (A[a].get("typeLine") or "").lower()))
        def generic(a):
            colored = len(re.findall(r"\{[WUBRG](?:/[WUBRGP])?\}", A[a].get("manaCost") or ""))
            return max(0, next((p.get("amount", 0) for r, p in g["COSTS"].get(a, []) if r == "mana"), 0) - colored)
        used, saving = [], 0
        for card, n, kind in sorted({r for r in reducers}, key=lambda r: -r[1]):
            if produced.get("mana", 0) >= spent["mana"] - saving:
                break
            if any(applies(kind, a) for a in casts):
                used.append((card, n, kind))
                saving = sum(min(generic(a), sum(k for _, k, kd in used if applies(kd, a))) for a in casts)
        # Activated-ability reducers (Training Grounds, Zirda, Heartstone): "activated abilities of creatures you
        # control cost {2} less to activate", usually never below one mana.
        if produced.get("mana", 0) < spent["mana"] - saving:
            acts = [a for a in cycle if A[a]["kind"] == "Activated" and any(r == "mana" for r, _ in g["COSTS"].get(a, []))]
            for x in A.values():
                low = (x["text"] or "").lower()
                m = re.search(r"activated abilities (?:of (creatures|artifacts|permanents) you control )?cost \{(\d)\} less", low)
                if not m or x["card"] in {A[a]["card"] for a in cycle} or "opponents" in low or produced.get("mana", 0) >= spent["mana"] - saving:
                    continue
                kind = (m.group(1) or "").rstrip("s")
                floor = 1 if re.search(r"less than one mana", low) else 0
                hit = [a for a in acts if not kind or kind == "permanent" or kind in {t.lower() for t in (A[a].get("cardTypes") or [])}]
                if hit:
                    used.append((x["card"], 0, "activated"))
                    for a in hit:
                        cost = next((p.get("amount", 0) for r, p in g["COSTS"].get(a, []) if r == "mana"), 0)
                        saving += max(0, min(int(m.group(2)), cost - floor))
        if used and produced.get("mana", 0) >= spent["mana"] - saving:
            spent["mana"] -= saving
            extra_cards.update(c for c, _, _ in used)
    if spent.get("untapped_creature"):
        # Earthcraft's "tap an untapped creature": every creature that enters fresh this lap arrives untapped.
        produced["untapped_creature"] += produced.get("creature", 0) + sum(
            1 for a in cycle for ev, p in g["RETRIGGERS"].get(a, []) if ev == "self_enters" and p.get("immediate", True))
    # {C} in a cost needs colourless mana specifically (Eldrazi Displacer); Phyrexian Altar's coloured mana can't pay it.
    if any(A[a]["kind"] == "Activated" and "{c}" in (A[a]["text"] or "").lower().split(":")[0] for a in cycle) and \
            not any(re.search(r"add [^.]*\{c\}|colorless mana|untap (?:up to \w+|all|target|each)[^.]*lands?", (A[a]["text"] or "").lower())
                    for a in cycle):
        return None
    if any(produced[r] < spent[r] for r in spent):
        return None                                      # can't sustain itself
    # Name a doubler only if the loop needs it: would it still pay for itself without doubling?
    base = defaultdict(int)
    for a in list(cycle) + ([side] if side else []):     # the side creature's output counts too
        for res, p in g["PRODUCES"].get(a, []):
            base[res] += amount(p) * ((mult or {}).get(a, 1) if a in cycle else 1)
    if all(base[r] >= spent[r] for r in spent):
        extra_doublers = set()
        produced = base                       # report what the loop makes without the doubler
    # Persist/undying only repeat if a specific card resets the counter they leave behind;
    # that card becomes part of the combo.
    RESET = {"-1/-1": r"-1/-1 counters? (?:can't be|would be) put|can't have -1/-1|remove (?:a|all) -1/-1 counters? from (?:target|a|each|it\b)|put (?:a|an additional) \+1/\+1 counter on (?:it|target creature|each)",
             "once": r"(?!x)x",                    # nothing resets it
             "flying": r"remove (?:a|all|each) (?:flying )?counters?|loses? flying|counters? (?:can't be|would be) put",
             "+1/+1": r"\+1/\+1 counters? (?:can't be|would be) put on|can't have \+1/\+1|remove (?:a|all) \+1/\+1 counters? from (?:target|a|each|it\b)|put (?:a|an additional) -1/-1 counter on (?:it|target creature|each|up to one target creature)"}
    for a in cycle:
        lim = A[a].get("limitedBy")
        if lim:
            # The reset must be repeatable: not a loyalty ability or other once-per-turn effect, no tap cost.
            options = [x for x in A.values()
                       if x["card"] != A[a]["card"] and re.search(RESET[lim], x["text"] or "")
                       and not x.get("oncePerTurn") and not (x["kind"] == "Activated" and x.get("tapCost"))
                       and x["kind"] != "Spell"]                     # a one-shot sorcery can't reset every loop
            # The reset's own cost comes out of the loop's budget: Yawgmoth removes the counter only by
            # sacrificing another creature, which the loop must supply every time.
            def affordable(x):
                return all(produced[r] >= spent[r] + amount(p) for r, p in g["COSTS"].get(x["id"], []))
            options = sorted([x for x in options if affordable(x)], key=lambda x: (bool(g["COSTS"].get(x["id"])), x["card"]))
            if not options:
                return None
            for r, p in g["COSTS"].get(options[0]["id"], []):
                spent[r] += amount(p)
            extra_cards.add(options[0]["card"])
    # A card's own enters/leaves/dies ability only repeats if the loop moves that card again.
    def moves(card, events, copy_ok):
        for a in cycle:
            for ev, p in g["RETRIGGERS"].get(a, []):
                if not p.get("immediate", True):
                    continue                  # "at the next end step": too late to keep the loop going
                allowed = p.get("targetTypes")
                card_types = {t.lower() for x in A.values() if x["card"] == card for t in (x.get("cardTypes") or [])}
                if allowed and "permanent" not in allowed and not card_types & set(allowed):
                    continue                  # a creature-only flicker can't move an enchantment
                if ev == "self_enters" and A[a]["card"] != card and \
                        not can_return(A[a]["text"], next((x for x in A.values() if x["card"] == card), {})):
                    continue                  # Reveillark can't return a 4-power creature
                if ev.startswith("self_") and A[a]["card"] != card and \
                        excluded_type(A[a]["text"], next((x for x in A.values() if x["card"] == card), {}), A[a]["card"]):
                    continue                  # "non-Angel": can't move an Angel
                if ev.startswith("self_") and A[a]["card"] != card and \
                        not reaches(A[a]["text"], next((x for x in A.values() if x["card"] == card), {}), A[a]["card"]):
                    continue                  # the effect can't reach this card's type
                if ev in ("self_enters", "enters_from_graveyard") and A[a]["card"] != card and \
                        LIBRARY_DIG.search(A[a]["text"] or "") and "graveyard" not in (A[a]["text"] or "").lower():
                    continue
                if not copy_ok and COPY_OF_ENTERED.search(A[a]["text"] or ""):
                    continue                  # Preston's copy of "that creature" is a token: it isn't this card re-entering
                sc = p.get("scope", "any")
                hits = (sc == "self" and A[a]["card"] == card) or sc == "granted" \
                    or (sc in ("other", "copy") and A[a]["card"] != card)
                if hits and (ev in events or (copy_ok and sc == "copy" and ev == "self_enters")):
                    return True
        return False
    for b in cycle:
        # "This creature or another creature dies": it can fire through the "another creature" half,
        # so the card's own death doesn't have to repeat (Pawn of Ulamog).
        watches_others = any(not ev.startswith("self_") for ev, _ in g["TRIGGERED_BY"].get(b, []))
        for ev, p in g["TRIGGERED_BY"].get(b, []):
            card = A[b]["card"]
            if ev in ("self_dies", "self_leaves") and not watches_others and not moves(card, {"self_enters"}, False):
                return None
            # A card's own enters ability repeats only if the loop takes it out AND brings it back
            # (flicker does both; a copy counts as a new card entering).
            if ev == "self_enters" and not (moves(card, {"self_leaves", "self_dies"}, True)
                                            and moves(card, {"self_enters"}, True)):
                return None
            # Sun Titan and Karmic Guide return cards FROM THE GRAVEYARD: a card that was exiled isn't there.
            if ev == "self_enters":
                srcs = [x for x in cycle for e2, p2 in g["RETRIGGERS"].get(x, []) if e2 == "self_enters" and
                        ((p2.get("scope") == "self" and A[x]["card"] == card) or
                         (p2.get("scope") in ("other", "any") and A[x]["card"] != card))]
                if srcs and all(re.search(r"from (?:your|a|their|an opponent's) graveyard", (A[x]["text"] or "").lower()) for x in srcs) \
                        and not moves(card, {"self_dies"}, False):
                    return None
        # Preston, the Vanisher copies "that creature" (whatever just entered): the copy fires X's enter ability
        # only if X itself enters again in the loop. (Kiki-Jiki copies a creature already on the battlefield.)
        card = A[b]["card"]
        if any(ev == "self_enters" for ev, _ in g["TRIGGERED_BY"].get(b, [])):
            sources = [(x, p) for x in cycle for e2, p in g["RETRIGGERS"].get(x, []) if e2 == "self_enters" and A[x]["card"] != card]
            if sources and all(re.search(r"copy of (?:that creature|it\b|that permanent|the creature)", (A[x]["text"] or "").lower())
                               for x, p in sources) and not moves(card, {"self_enters"}, False):
                return None
        # An Aura's "when enchanted creature dies" goes to the graveyard with the creature:
        # it repeats only if the loop returns the Aura itself.
        if A[b].get("consumesSource") and not moves(A[b]["card"], {"self_enters"}, False):
            return None
    makers = [p for a in cycle for res, p in g["PRODUCES"].get(a, []) if res == "creature"]
    # "Another non-Spirit creature dies" can't be fed by the Spirit tokens it makes.
    for b in cycle:
        for ev, p in g["TRIGGERED_BY"].get(b, []):
            ex = p.get("excludesType")
            if ex and makers and all(ex.lower() in (m.get("tokenDesc") or "").lower() for m in makers):
                return None
    # Triggers that ignore tokens can't be fed by tokens alone.
    makers = [p for a in cycle for res, p in g["PRODUCES"].get(a, []) if res == "creature"]
    if makers and all(p.get("token") for p in makers):
        for b in cycle:
            if any(ev.startswith("creature_") and p.get("nontoken") for ev, p in g["TRIGGERED_BY"].get(b, [])):
                return None
    # Every triggered ability in the loop must be fired by something in the loop
    # (Goblinslide's "whenever you cast a noncreature spell" isn't fired by sacrificing creatures).
    fired_events = {ev for a in cycle for ev, p in g["RETRIGGERS"].get(a, []) if p.get("immediate", True)}
    fired_events |= {ev for a in cycle for res, _ in g["PRODUCES"].get(a, []) for ev in resource_events(g, res)}
    fired_events |= {d for ev in list(fired_events) for d in g.get("COUNTS_AS", {}).get(ev, ())}
    if side:
        fired_events |= {ev for ev, p in g["RETRIGGERS"].get(side, [])}
    for b in cycle:
        evs = [ev for ev, _ in g["TRIGGERED_BY"].get(b, [])]
        if A[b]["kind"] == "Triggered" and evs and not set(evs) & fired_events:
            return None
    has_trigger_link = any(A[a]["kind"] == "Triggered" for a in cycle)
    # Chains of activated abilities alone are usually just mana passed around; a copied ability is the exception,
    # because gaining it again makes a new ability each time (Quicksilver Elemental + Vivi Ornitier).
    if not has_trigger_link and not all(A[a].get("copiedFrom") or A[a].get("modeled") for a in cycle):
        return None
    net = {r: produced[r] - spent.get(r, 0) for r in produced}
    outcome = {r for r, n in net.items() if n > 0}
    # Events the loop fires every iteration, and observers that react to them.
    fired = {ev for a in cycle for ev, p in g["RETRIGGERS"].get(a, [])}
    loop_cards = {A[a]["card"] for a in cycle}
    for b, evs in g["TRIGGERED_BY"].items():
        for ev, _ in evs:
            if ev in fired and (ev in OBSERVER_EVENTS or A[b]["card"] in loop_cards):
                outcome |= {res for res, _ in g["PRODUCES"].get(b, [])}
    for a in cycle:
        outcome |= {res for res, _ in g["PRODUCES"].get(a, [])}
    if fired:
        outcome.add(f"infinite {'/'.join(sorted(fired))}")
    # Infinite mana + an unlimited mana-only flicker = repeat any enters ability.
    if net.get("mana", 0) > 0:
        for a, rts in g["RETRIGGERS"].items():
            if A[a]["kind"] == "Activated" and not A[a].get("tapCost") \
                    and any(ev == "self_enters" and p.get("immediate", True) for ev, p in rts):
                for b, evs in g["TRIGGERED_BY"].items():
                    if any(ev == "self_enters" for ev, _ in evs):
                        outcome |= {res for res, _ in g["PRODUCES"].get(b, [])}
    # The state walk is the most expensive check, so it runs last, only on cycles that pass everything else
    if STATE_WALK and not state_walk(g, cycle):
        k = state_key(g, cycle)
        if k:
            g.setdefault("_STATE_REJECTED", set()).add(k)
        return None                                       # the states don't come back where they started
    path = [{"card": A[a]["card"], "kind": A[a]["kind"], "text": A[a]["text"],
             "states": [list(r[:4]) for r in (g.get("STATES") or {}).get(a, [])]} for a in cycle]
    copied = {A[a]["copiedFrom"] for a in cycle if A[a].get("copiedFrom")}   # Vivi has to be there to be copied
    copied |= {c for a in cycle for c in A[a].get("modelCards", [])}        # a drawn model's cards are its combo
    return {"cards": sorted({A[a]["card"] for a in cycle} | extra_cards | extra_doublers | copied), "net": net,
            "outcome": sorted(outcome), "path": path,
            "helpers": sorted(extra_cards | extra_doublers)}


def cycle_has_costs(g, cycle):
    return any(g["COSTS"].get(a) for a in cycle)


def trigger_chain_edges(g, edges):
    """Only links where one triggered ability's effect fires another's trigger: sparse enough to
    search longer cycles (three cards exiling and returning each other is six abilities)."""
    A = g["abilities"]
    out = defaultdict(set)
    for a, bs in edges.items():
        for b in bs:
            if A[a]["kind"] == "Triggered" and A[b]["kind"] == "Triggered" and g["RETRIGGERS"].get(a) and g["TRIGGERED_BY"].get(b):
                out[a].add(b)
    return out


def balance_loops(g, cycle_list, max_seeds=400, max_helpers=12, debug=False, deadline=None):
    """Loops that need an ability to fire more than once per loop (sacrifice two creatures, get two
    Treasures, pay {1}{B}). Seeds are cycles short on a resource. One integer program per seed picks
    whole-number counts for the seed's abilities and any helpers, counting each trigger link:
      - a targeted event (one card enters/leaves/dies) goes to one card per activation
        (two for "two target"), so Eldrazi Displacer can't fire two creatures at once;
      - a broadcast event ("a creature dies") reaches every card watching for it.
    The result then goes through every usual rule (colours, re-entry, once per turn...)."""
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
    except ImportError:
        return []
    A = g["abilities"]
    fires, fire_ev = g.get("_fires", {}), g.get("_fire_ev", {})
    def net_of(abilities):
        net = defaultdict(lambda: defaultdict(int))
        for a in abilities:
            for res, p in g["PRODUCES"].get(a, []):
                net[res][a] += amount(p)
            for res, p in g["COSTS"].get(a, []):
                net[res][a] -= amount(p)
        return net
    results, seen = [], set()
    for cyc in cycle_list:
        if deadline and time.time() > deadline:
            break
        # Readings that behave identically are one seed (a weaker reading mustn't block a stronger one).
        behaviour = lambda a: (A[a]["card"], A[a]["kind"], tuple(sorted(ev for ev, _ in g["TRIGGERED_BY"].get(a, []))),
                               tuple(sorted((r, amount(p)) for r, p in g["PRODUCES"].get(a, []))),
                               tuple(sorted((r, amount(p)) for r, p in g["COSTS"].get(a, []))))
        key = frozenset(behaviour(a) for a in cyc)
        if key in seen or len(seen) >= max_seeds:
            continue
        short = {r for r, row in net_of(cyc).items() if sum(row.values()) < 0}
        if not short or not short <= {"mana", "creature", "artifact", "token"}:
            continue
        seen.add(key)
        in_cyc = set(cyc)
        used = short | {r for a in cyc for r, _ in g["COSTS"].get(a, [])}
        # One reading per ability: the rules' and the LLM's reading of Pitiless Plunderer are the same
        # trigger, and one death must not make two Treasures.
        sig = lambda a: (A[a]["card"], A[a]["kind"], tuple(sorted(ev for ev, _ in g["TRIGGERED_BY"].get(a, []))))
        taken = {sig(a) for a in cyc}
        helpers = []
        for b in sorted(A):
            if b in in_cyc or A[b]["kind"] != "Triggered" or not (fires.get(b, set()) & in_cyc) or sig(b) in taken:
                continue
            if any(r in used for r, _ in g["PRODUCES"].get(b, [])):
                helpers.append(b)
                taken.add(sig(b))
        helpers = helpers[:max_helpers]
        S = list(cyc) + helpers
        links = [(a, b) for b in S for a in sorted(fires.get(b, set())) if a in S and A[b]["kind"] == "Triggered"]
        n, m = len(S), len(links)
        X = {a: i for i, a in enumerate(S)}                  # x_a: times ability a fires per loop
        Y = {l: n + j for j, l in enumerate(links)}          # y_ab: times a fires b per loop
        rows, lo, hi = [], [], []
        def row(coef):
            v = [0.0] * (n + m)
            for k, c in coef.items():
                v[k] += c
            return v
        net = net_of(S)
        for r, rr in net.items():                            # every resource balances
            if any(v < 0 for v in rr.values()):
                rows.append(row({X[a]: v for a, v in rr.items()})); lo.append(0); hi.append(np.inf)
        for b in S:
            if A[b]["kind"] != "Triggered":
                continue
            ins = [l for l in links if l[1] == b]
            if ins:                                          # b fires only as often as something fires it
                rows.append(row({X[b]: 1, **{Y[l]: -1 for l in ins}})); lo.append(-np.inf); hi.append(0)
            if any(p.get("nontoken") for _, p in g["TRIGGERED_BY"].get(b, [])):
                made = {X[a]: -sum(amount(p) for res, p in g["PRODUCES"].get(a, []) if res == "creature"
                                   and not p.get("token") and not p.get("tokenDesc")) for a in S}
                made[X[b]] = made.get(X[b], 0) + 1           # ... and "nontoken" ones only per nontoken creature
                rows.append(row(made)); lo.append(-np.inf); hi.append(0)
        for a in S:
            targeted = [l for l in links if l[0] == a and fire_ev.get(l, "").startswith("self_")]
            if targeted:                                     # one card per activation (two for Ghostly Flicker)
                k = 2 if re.search(r"\btwo target", A[a]["text"] or "") else 1
                rows.append(row({**{Y[l]: 1 for l in targeted}, X[a]: -k})); lo.append(-np.inf); hi.append(0)
            for l in links:
                if l[0] == a and not fire_ev.get(l, "").startswith("self_"):
                    rows.append(row({Y[l]: 1, X[a]: -1})); lo.append(-np.inf); hi.append(0)   # broadcast
        lb = [1 if a in in_cyc else 0 for a in S] + [0] * m
        # Once per loop: spells (one return each) and tap abilities (untapping lands won't untap a Signet).
        once = lambda a: A[a]["kind"] == "Spell" or (A[a]["kind"] == "Activated" and A[a].get("tapCost"))
        ub = [1 if once(a) else 4 for a in S] + [8] * m
        cost = [1.0] * n + [0.0] * m
        res = milp(c=np.array(cost), integrality=np.ones(n + m), bounds=Bounds(lb, ub),
                   constraints=LinearConstraint(np.array(rows), lo, hi) if rows else ())
        if debug:
            print("   seed", [A[a]["card"][:8] for a in cyc], "helpers", [A[a]["card"][:8] for a in helpers],
                  "short", short, "->", res.status, res.message[:60], res.x if res.status == 0 else "")
        if res.status != 0:
            continue
        x = {a: int(round(res.x[X[a]])) for a in S}
        if max(x.values()) < 2:
            continue                                         # a simple loop: the cycle search covers it
        members = [a for a in S if x[a] > 0]
        r = evaluate(g, members, None, mult=x)
        if r:
            r["multiplicity"] = {A[a]["card"]: x[a] for a in members if x[a] > 1}
            results.append(r)
    return results


def find_loops(db, deck, budget_s=45, info=None):
    """Minimal loops only: drop any loop whose cards include another loop's cards.

    Shortest cycles are checked first, and any cycle whose cards already contain a found combo is
    skipped: it could only give a non-minimal version. A deck dense with blink and recursion can
    have hundreds of thousands of six-step cycles, so the search also stops after `budget_s`
    seconds; `info["stoppedEarly"]` then says the results are what was found so far."""
    t0 = time.time()
    g = deck_subgraph(db, deck)
    A = g["abilities"]
    by_cards = {}
    edges = causal_edges(g)
    found = {tuple(c) for c in cycles(edges)} | {tuple(c) for c in cycles(trigger_chain_edges(g, edges), max_len=6)}
    ordered = sorted(found, key=lambda c: (len(c), c))      # fixed order: same answer every run
    combos, stopped, checked = [], False, 0
    for c in ordered:
        if time.time() - t0 > budget_s:
            stopped = True
            break
        cards = frozenset(A[a]["card"] for a in c)
        if any(s <= cards for s in combos):
            continue                                   # can only extend a combo already found: not minimal
        checked += 1
        for side in (side_options(g, c) or [None]):
            r = evaluate(g, c, side)
            if r and len(r["cards"]) >= 2:            # a combo takes at least two cards
                key = frozenset(r["cards"])
                if key in by_cards:
                    outcome = sorted(set(by_cards[key]["outcome"]) | set(r["outcome"]))
                    # The same cards can loop through two readings of one ability (rules + LLM):
                    # keep the shortest cycle, which doesn't count an ability twice.
                    if len(r["path"]) < len(by_cards[key]["path"]):
                        by_cards[key] = r
                    by_cards[key]["outcome"] = outcome
                else:
                    by_cards[key] = r
                    combos.append(key)
    if not stopped:
        rest = budget_s - (time.time() - t0)
        seeds = [c for c in ordered if not any(s <= frozenset(A[a]["card"] for a in c) for s in combos)]
        for r in balance_loops(g, seeds, deadline=time.time() + max(rest, 2)):
            if len(r["cards"]) >= 2:
                by_cards.setdefault(frozenset(r["cards"]), r)
    # One-step loops: a copied ability that pays for itself, activated again and again (Quicksilver Elemental
    # gaining Vivi Ornitier's mana ability: pay {U}, get X mana). Checked like any other loop.
    for a in [a for a in A if A[a].get("copiedFrom") or (A[a].get("modeled") and A[a]["kind"] == "Activated")]:
        r = evaluate(g, [a])
        if r and len(r["cards"]) >= 2 and any(v > 0 for v in r["net"].values()):   # it has to come out ahead
            key = frozenset(r["cards"])
            if key not in by_cards or len(r["path"]) < len(by_cards[key]["path"]):
                by_cards[key] = r                         # the one-step loop is the clearest
    if info is not None:
        info.update({"stoppedEarly": stopped, "cycles": len(found), "checked": checked, "seconds": round(time.time() - t0, 1)})
    keys = list(by_cards)
    minimal = [k for k in keys if not any(o < k for o in keys)]
    return sorted((by_cards[k] for k in minimal), key=lambda r: (len(r["cards"]), r["cards"]))


# --- engines that repeat each turn (known combos only) ----------------------------------------------------
TURN_EVENT = re.compile(r"combat|upkeep|end_step|beginning|each_turn|draw_step", re.I)


def find_turn_engines(db, combos, max_len=5):
    """Explain known combos that repeat once each turn (Brago, King Eternal + Archaeomancer + Cyclonic Rift).
    For each combo (a set of card names, all in the deck): start at an ability fired by a turn event (combat damage,
    upkeep, end step), follow the graph's causal edges, and accept a path when every card is either a step or a
    payload: a card a step returns from the graveyard to the hand (or battlefield) so it can be cast again, of a
    type that step can return. Mana doesn't have to balance: lands untap each turn. Only known combos are tried,
    so this explains catalogued engines rather than proposing new ones."""
    out = []
    for combo in combos:
        cards = sorted(set(combo))
        g = deck_subgraph(db, cards)
        A = g["abilities"]
        E = causal_edges(g)
        info = {c: next((x for x in A.values() if x["card"] == c), {}) for c in cards}
        starts = [a for a in A if any(TURN_EVENT.search(ev) for ev, _ in g["TRIGGERED_BY"].get(a, []))]
        best = None

        def returns(step, card):
            st = g.get("STATES", {}).get(step, [])
            back = any(r == "REQUIRES_STATE" and s == "graveyard" and w == "target" for r, s, w, *_ in st) and \
                any(r == "CAUSES_STATE" and s in ("hand", "battlefield") and w == "target" for r, s, w, *_ in st)
            return back and info.get(card) and reaches(A[step]["text"], info[card], A[step]["card"])

        def walk(path):
            nonlocal best
            covered = {A[a]["card"] for a in path}
            payload = [c for c in cards if c not in covered]
            if all(any(returns(s, c) for s in path) for c in payload) and (best is None or len(path) < len(best[0])):
                best = (list(path), payload)
            if len(path) < max_len:
                for nxt in E.get(path[-1], ()):
                    if nxt not in path and A[nxt]["card"] in cards:
                        walk(path + [nxt])

        for s in starts:
            walk([s])
        if best:
            path, payload = best
            out.append({"cards": cards, "kind": "turn", "perTurn": True, "payload": payload, "net": {}, "outcome": ["each turn"],
                        "helpers": [], "path": [{"card": A[a]["card"], "kind": A[a]["kind"], "text": A[a]["text"],
                                                 "states": [list(r[:4]) for r in g.get("STATES", {}).get(a, [])]} for a in path]})
    return out


# --- acceptance checks ------------------------------------------------------------

def check(name, ok, detail):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}")
    return ok


def main(store):
    db = relate.connect(store)
    results = []

    # 1. Net card advantage: Mulldrifter should outrank "draw three, discard your hand".
    names = ["Mulldrifter", "Cloudblazer", "Riverwise Augur", "Bedlam Reveler", "Ox of Agonas", "Combustible Gearhulk"]
    rows = list(db.execute("""MATCH (c:Card)-[:HAS_ABILITY]->(a:Triggered)-[:TRIGGERED_BY]->(:Event {name: 'self_enters'}),
                                     (a)-[p:PRODUCES]->(:Resource {name: 'card_draw'})
                              WHERE c.name IN $n RETURN c.name AS card, p.amount AS draws, p.net AS net,
                                     p.discardsHand AS dh, p.opponentChoice AS oc""", {"n": names}))
    score = lambda r: (-1 if r["dh"] or r["oc"] else (r["net"] or 0))
    ranked = sorted(rows, key=score, reverse=True)
    results.append(check("Mulldrifter outranks discard/put-back draws",
                         ranked and ranked[0]["card"] in ("Mulldrifter", "Cloudblazer"),
                         " > ".join(f"{r['card']} (draw {r['draws']}, net {'n/a' if score(r) < 0 else r['net']})" for r in ranked)))

    # 2. Deadeye + Drake + a draw creature = infinite mana and draw.
    loops = find_loops(db, ["Deadeye Navigator", "Peregrine Drake", "Mulldrifter"])
    hit = [l for l in loops if {"Deadeye Navigator", "Peregrine Drake"} <= set(l["cards"])]
    results.append(check("Deadeye + Drake + Mulldrifter -> infinite mana + draw",
                         bool(hit) and {"mana", "card_draw"} <= set(hit[0]["outcome"]), hit or loops))

    # 3. Fiend Hunter (two triggers: flicker in response exiles for good) vs Banishing Light.
    fh = list(db.execute("MATCH (:Card {name: 'Fiend Hunter'})-[:HAS_ABILITY]->(l)-[:RETURNS_FROM]->(e) RETURN count(*) AS n"))[0]["n"]
    bl = list(db.execute("""MATCH (:Card {name: 'Banishing Light'})-[:HAS_ABILITY]->(a)-[p:PRODUCES]->(:Resource {name: 'removal'})
                            OPTIONAL MATCH (a)<-[:RETURNS_FROM]-(x) RETURN p.duration AS d, count(x) AS linked"""))
    results.append(check("Fiend Hunter trick detected, Banishing Light excluded",
                         fh > 0 and bl and bl[0]["d"] == "until_source_leaves" and bl[0]["linked"] == 0,
                         f"Fiend Hunter linked leave->enter abilities: {fh}; Banishing Light: {bl}"))

    # 4. Worldgorger Dragon + Animate Dead loop.
    loops = find_loops(db, ["Worldgorger Dragon", "Animate Dead"])
    results.append(check("Worldgorger Dragon + Animate Dead loop",
                         any(set(l["cards"]) == {"Worldgorger Dragon", "Animate Dead"} for l in loops), loops))

    # 5. Deadeye + Drake + Impact Tremors = infinite damage.
    loops = find_loops(db, ["Deadeye Navigator", "Peregrine Drake", "Impact Tremors"])
    hit = [l for l in loops if "damage_players" in l["outcome"]]
    results.append(check("Deadeye + Drake + Impact Tremors -> infinite damage", bool(hit), hit or loops))

    print(f"\n{sum(results)}/{len(results)} checks passed")


if __name__ == "__main__":
    main(sys.argv[1])


def find_loops_through(db, deck, card, max_len=4, limit=None, balance=True):
    """Loops that need `card`: cycles searched only from that card's abilities (fast enough to
    score dozens of candidate cards for a deck)."""
    g = deck_subgraph(db, sorted(set(deck) | {card}))
    A = g["abilities"]
    edges = causal_edges(g)
    starts = [a for a in A if A[a]["card"] == card]
    found = set()
    def dfs(start, node, path):
        for nxt in edges.get(node, ()):
            if nxt == start and len(path) > 1:
                found.add(tuple(path))
            elif nxt not in path and len(path) < max_len:
                dfs(start, nxt, path + [nxt])
    for s in starts:
        dfs(s, s, [s])
    out = {}
    for c in sorted(found, key=lambda c: (len(c), c)):
        if limit and len(out) >= limit:
            break                                   # enough to score a suggestion
        for side in (side_options(g, list(c)) or [None]):
            r = evaluate(g, list(c), side)
            if r and len(r["cards"]) >= 2 and card in r["cards"]:
                out.setdefault(frozenset(r["cards"]), r)
    for r in (balance_loops(g, sorted(found, key=lambda c: (len(c), c)), max_seeds=60) if balance and not (limit and len(out) >= limit) else []):
        if card in r["cards"] and len(r["cards"]) >= 2:
            out.setdefault(frozenset(r["cards"]), r)
    keys = list(out)
    return [out[k] for k in keys if not any(o < k for o in keys)]
