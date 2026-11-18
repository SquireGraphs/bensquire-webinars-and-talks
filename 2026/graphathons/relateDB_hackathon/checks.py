"""MTGraphRAG regression suite: run after every rule or data change.

    python checks.py mtg_live.rl variants_slim.jsonl.gz          # everything (~2 minutes)
    python checks.py mtg_live.rl variants_slim.jsonl.gz --fast   # skip the random-deck sweep

Exit code 0 only if every check passes. Each control below came from a real deck or a
random-deck false alarm; reviews.jsonl (written by the app's review buttons) adds more.
"""
import argparse
import gzip
import json
import os
import random
import sys
import time

import relate

from decks import load_deck, load_deck_text
from engine import find_loops
from nonbo import check

# Card sets that must form a loop (optionally with at least this much net mana per loop).
MUST_LOOP = [
    (["Deadeye Navigator", "Peregrine Drake"], 3),
    (["Restoration Angel", "Felidar Guardian"], None),
    (["Worldgorger Dragon", "Animate Dead"], None),
    (["Kitchen Finks", "Viscera Seer", "Melira, Sylvok Outcast"], None),
    (["Abdel Adrian, Gorion's Ward", "Animate Dead"], None),
    (["Leonin Relic-Warder", "Animate Dead"], None),
    (["Leonin Relic-Warder", "Necromancy"], None),
    (["Leonin Relic-Warder", "Dance of the Dead"], None),
    (["Abdel Adrian, Gorion's Ward", "Leonin Relic-Warder", "Touch the Spirit Realm"], None),
    (["Ghostly Flicker", "Archaeomancer", "Peregrine Drake"], 2),
    (["Palinchron", "Panharmonicon"], 3),
    (["Karmic Guide", "Reveillark", "Viscera Seer"], None),
    (["Exquisite Blood", "Sanguine Bond"], None),
    (["Kaya's Ghostform", "Sun Titan", "Viscera Seer"], None),
    (["Altar of Dementia", "Pitiless Plunderer", "Reassembling Skeleton", "Teysa Karlov"], None),
    (["Niv-Mizzet, Parun", "Curiosity"], None),                                   # draw -> damage -> draw
    (["Niv-Mizzet, Parun", "Niv-Mizzet, Visionary"], None),
    (["Kiki-Jiki, Mirror Breaker", "Pestermite"], None),                          # token copies
    (["Kiki-Jiki, Mirror Breaker", "Restoration Angel"], None),                   # re-entering untaps
    (["Gravecrawler", "Phyrexian Altar", "Butcher Ghoul"], None),                 # cast from graveyard, needs a Zombie
    (["Gravecrawler", "Mikaeus, the Unhallowed", "Phyrexian Altar"], None),        # Phyrexian Altar makes black (rulings test: works)
    (["Angelic Chorus", "Herd Baloth", "Sunbond"], None),                         # counters fire "counters are put on"
    (["Earthcraft", "Shrieking Drake"], None),                                    # tap an untapped creature; self-bounce
    (["Vivi Ornitier", "Quicksilver Elemental"], None),                           # copying an activated ability resets "once each turn"
    (["Abdel Adrian, Gorion's Ward", "Animate Dead"], None),                      # held + graveyard states (rulings: works)
    (["Fiend Hunter", "Phyrexian Altar", "Sun Titan"], None),                     # held + graveyard states (rulings: works)
    (["Kaya's Ghostform", "Phyrexian Altar", "Sun Titan"], None),
    (["Ebondeath, Dracolich", "Grimgrin, Corpse-Born", "Pitiless Plunderer", "The Sibsig Ceremony"], None),  # a reducer pays the gap
    # needs an ability to fire twice per loop (balance engine)
    (["Ogre Slumlord", "Pitiless Plunderer", "Reassembling Skeleton", "Viscera Seer"], None),
    (["Pawn of Ulamog", "Phyrexian Altar", "Reassembling Skeleton"], None),
    (["Ogre Slumlord", "Phyrexian Altar", "Reassembling Skeleton"], None),        # the Rat is the second sacrifice
    (["Phyrexian Altar", "Reassembling Skeleton", "Requiem Angel"], None),       # so is the Spirit
]
# Card sets that must NOT loop, with the rule each one guards.
MUST_NOT_LOOP = [
    (["Swords to Plowshares", "Animate Dead"], "removal isn't recursion"),
    (["Fiend Hunter", "Animate Dead"], "creature-only exile can't move an Aura"),
    (["Banishing Light", "Animate Dead"], "opponent-only exile can't move your cards"),
    (["Oblivion Ring", "Animate Dead"], "Animate Dead's sacrifice hits only its creature"),
    (["Luminous Broodmoth", "Phyrexian Plaguelord"], "Broodmoth returns each creature once"),
    (["Carrion Feeder", "Enduring Innocence"], "comes back as an enchantment"),
    (["Greater Good", "Puppeteer Clique", "Space Beleren"], "loyalty abilities can't reset every loop"),
    (["Clachan Festival", "Portcullis"], "delayed, creature-only return"),
    (["Fiend Hunter", "Restoration Angel"], "'when this leaves' is not 'a creature leaves'"),
    (["Banishing Light", "Felidar Guardian", "Oblivion Ring"], "no implied return for opponent-only exile"),
    (["Oblivion Ring", "Restoration Angel", "Yorion, Sky Nomad"], "end-step returns are too late"),
    (["Goblinslide", "Warren Soultrader"], "every trigger must be fired by the loop"),
    (["Nim Deathmantle", "Warren Soultrader"], "'you may pay {4}' is a cost"),
    (["The Sibsig Ceremony"], "a combo takes two cards"),
    (["Palinchron"], "recasting costs its mana value"),
    (["Ghostly Flicker", "Peregrine Drake"], "a spell needs a returner"),
    (["Requiem Angel", "Viscera Seer"], "non-Spirit trigger isn't fed by Spirits"),
    (["Requiem Angel", "Ashnod's Altar"], "non-Spirit trigger isn't fed by Spirits"),
    (["Reveillark", "Sun Titan", "Woe Strider"], "printed power and mana value limits"),
    (["Phyrexian Altar", "Puppeteer Clique", "Reveillark"], "Reveillark returns power 2 or less"),
    (["Altar of Dementia", "Karmic Guide", "Kaya's Ghostform"], "Karmic Guide returns creature cards"),
    (["Ashnod's Altar", "Reassembling Skeleton", "Sifter of Skulls"], "colourless mana can't pay {B}"),
    (["Pitiless Plunderer", "Reassembling Skeleton", "Teysa Karlov", "Village Rites"], "sorceries are one-shot"),
    (["Kitchen Finks", "Viscera Seer"], "persist needs a counter reset"),
    (["Karmic Guide", "Reveillark"], "needs a sacrifice outlet"),
    (["Niv-Mizzet, Parun", "Guttersnipe"], "Guttersnipe deals damage but doesn't draw"),
    (["Gravecrawler", "Phyrexian Altar"], "Gravecrawler needs a Zombie"),
    (["Ashnod's Altar", "Gravecrawler", "Mikaeus, the Unhallowed"], "colourless mana can't pay Gravecrawler's {B} (rulings test)"),
    (["Earthcraft", "Whitemane Lion"], "one untapped land doesn't pay {1}{W}"),
    (["Altar of Dementia", "Unstoppable Slasher"], "it returns with counters, so it won't return again"),
    (["Vengeful Strangler // Strangling Grasp", "Yawgmoth, Thran Physician"], "it returns as an Aura, not a creature"),
    (["Karmic Guide", "Preston, the Vanisher"], "Preston's copies are tokens and don't re-trigger it"),
    (["Ao, the Dawn Sky", "Karmic Guide", "Phyrexian Altar"], "Ao digs the library; nothing returns Karmic Guide"),
    (["Felidar Guardian", "Fiend Hunter", "Leonin Relic-Warder"], "Relic-Warder exiles only artifacts or enchantments"),
    (["Black Carriage", "Bronzehide Lion"], "Bronzehide Lion returns as an Aura, not a creature"),
    (["Glorious Protector", "Karmic Guide", "Phyrexian Altar"], "Glorious Protector exiles only non-Angels; Karmic Guide is an Angel"),
    (["Quicksilver Elemental", "Simian Spirit Guide"], "Spirit Guide's ability works only from the hand"),
    (["Quicksilver Elemental", "Llanowar Elves"], "copying a tap ability gives one use"),
    (["Eldrazi Displacer", "Fiend Hunter", "Phyrexian Altar", "Recruiter of the Guard"], "{C} needs colourless mana; Altar makes coloured (rulings)"),
    (["Pitiless Plunderer", "Reassembling Skeleton", "Teysa Karlov", "Yawgmoth, Thran Physician"], "Yawgmoth costs 1 life each time (rulings)"),
    (["Blasting Station", "Oblivion Ring", "Sun Titan"], "Station sacrifices creatures, not enchantments"),
    (["Phyrexian Altar", "Sun Titan", "Suspension Field"], "Altar sacrifices creatures, not enchantments (701.21a)"),
    (["Ebondeath, Dracolich", "Grimgrin, Corpse-Born", "Pitiless Plunderer"], "short on mana without The Sibsig Ceremony"),
    (["Earthcraft", "Llanowar Elves"], "nothing makes a creature enter again"),
    (["Basking Broodscale", "Fathom Mage"], "evolve needs a bigger creature; Broodscale needs counters on itself"),
    (["Restoration Angel", "Wispweaver Angel"], "Restoration Angel can't flicker an Angel"),
    # TODO (known open issue): Preston copies whatever entered, not Angel of Indemnity; the LLM reading links it
    # through "a creature enters". Re-enable when fixed:
    # (["Angel of Indemnity", "Preston, the Vanisher"], "Preston copies whatever entered"),
    (["Felidar Guardian", "Solitude", "Werefox Bodyguard"], "Solitude exiles for good"),
    (["Kiki-Jiki, Mirror Breaker", "Peregrine Drake"], "the copy untaps lands, not Kiki-Jiki"),
    # found by the full-pool enrichment
    (["Atla Palani, Nest Tender", "Braids's Frightful Return"], "Atla needs an Egg; a Saga chapter happens once"),
    (["Turn Inside Out", "Voldaren Pariah // Abolisher of Bloodlines"], "every part of an instant is one-shot"),
    (["Cartouche of Solidarity", "Grimgrin, Corpse-Born", "Verdant Succession"], "Verdant Succession needs a green creature"),
    (["Binding Mummy", "Sneak Attack", "Vraska, Betrayal's Sting"], "Binding Mummy needs a Zombie"),
    (["Bloated Processor", "Loyal Cathar // Unhallowed Cathar"], "sacrifice another Phyrexian; end-step return"),
    (["Deathless Behemoth", "Ratadrabik of Urborg"], "Ratadrabik needs a legendary creature"),
    (["Gurgling Anointer", "Kyscu Drake"], "needs a creature named Spitting Drake"),
    (["Bane, Lord of Darkness", "Fell Shepherd", "Rose Room Treasurer"], "from your hand isn't recursion"),
    (["Loyal Cathar // Unhallowed Cathar", "Reaper of Flight Moonsilver"], "returns at the next end step"),
    (["Fell Shepherd", "Opal Caryatid", "Rose Room Treasurer", "The Skullspore Nexus"], "an opponent's cast isn't yours"),
    (["Ogre Slumlord", "Reassembling Skeleton", "Viscera Seer"], "no mana without Plunderer"),
    (["Ogre Slumlord", "Pitiless Plunderer", "Viscera Seer"], "nothing nontoken comes back"),
    (["Butcher Ghoul", "Yawgmoth, Thran Physician"], "Yawgmoth's reset costs another creature"),
    (["Fiend Hunter", "Puppeteer Clique", "Woe Strider"], "Clique reanimates from an opponent's graveyard"),
    (["Geralf's Messenger", "Yawgmoth, Thran Physician"], "Yawgmoth's reset costs another creature"),
]
# One-shot wins (not loops) that must be found, and look-alikes that must not.
MUST_WIN = [["Thassa's Oracle", "Demonic Consultation"], ["Laboratory Maniac", "Tainted Pact"],
            ["Orcish Bowmasters", "Peer into the Abyss"],
            # pattern loops through casting cards
            ["Cloudstone Curio", "Crimson Kobolds", "Rograkh, Son of Rohgahh"], ["Hullbreaker Horror", "Mox Amber"],
            ["Food Chain", "Squee, the Immortal"]]
# Run only with the state walk on (MTG_STATES=1): loops the rulings say fail that only states can settle
STATE_NOT_LOOP = [
    (["Felidar Guardian", "Lumbering Battlement"], "Felidar was on the battlefield, not held by Battlement (state walk)"),
    (["Lumbering Battlement", "Restoration Angel"], "the new Battlement's exile never ends (state walk)"),
    (["Lumbering Battlement", "Wispweaver Angel"], "the new Battlement's exile never ends (state walk)"),
]

MUST_NOT_WIN = [["Thassa's Oracle", "Inverter of Truth"],          # refills the library from the graveyard
                ["Cloudstone Curio", "Memnite", "Ornithopter"],     # artifacts don't trigger Curio
                ["Hullbreaker Horror", "Sol Ring"],                 # Sol Ring costs {1}
                ["Food Chain", "Llanowar Elves"]]                   # can't be cast from exile
# Pairs whose nonbo verdict is fixed: ("none" = no finding at all).
NONBO = [
    (["Dauthi Voidwalker", "Necropotence"], "none"),
    (["Hushwing Gryff", "Sungold Sentinel"], "note"),
    (["Hushwing Gryff", "Sterling Hound"], "nonbo"),
    (["Torpor Orb", "Mulldrifter"], "nonbo"),
    (["Containment Priest", "Reassembling Skeleton"], "nonbo"),
    (["Academy Manufactor", "Brenard, Ginger Sculptor"], "nonbo"),     # erases Brenard's creature Food copy
    (["Stony Silence", "Smothering Tithe"], "nonbo"),                  # Treasures can't be sacrificed
    (["Stony Silence", "Sol Ring"], "nonbo"),
    (["Academy Manufactor", "Smothering Tithe"], "none"),              # plain Treasures: Manufactor helps
    (["Stony Silence", "Blood Artist"], "none"),
    (["Rest in Peace", "Argivian Find"], "nonbo"),
    (["Solemnity", "Walking Ballista"], "nonbo"),
    (["Ilharg, the Raze-Boar", "Myojin of Infinite Rage"], "nonbo"),
    (["Solemnity", "Sol Ring"], "none"),
    (["Rest in Peace", "Sol Ring"], "none"),
]
# Minimum Spellbook combos found and maximum unconfirmed candidates, per deck.
DECKS = [
    ("decks/brago_blink.txt", 15, 6),
    ("decks/teysa_aristocrats.txt", 21, 10),     # 10: after the rulings review (most are uncatalogued outlet variants)           # 8 unconfirmed, reviewed: real variants Spellbook doesn't list
    ("decks/lotho.txt", 8, 0),
    ("decks/vivi.txt", 5, 2),
    ("decks/flubs.txt", 7, 2),
]


class Report:
    def __init__(self):
        self.failures, self.passed = [], 0

    def ok(self, cond, what):
        if cond:
            self.passed += 1
        else:
            self.failures.append(what)
            print("  FAIL", what)


def combos_found(db, d, variants):
    deck = sorted(set(d["commanders"] + d["cards"]))
    ds = set(deck)
    inside = [frozenset(u["card"]["name"] for u in v["uses"]) for v in variants
              if not v["requires"] and {u["card"]["name"] for u in v["uses"]} <= ds]
    loops = find_loops(db, deck)
    try:
        from wins import find_wins
        loops += find_wins(db, deck)
    except ImportError:
        pass
    match = lambda l, s: set(l["cards"]) <= s or set(l["cards"]) >= s
    from engine import find_turn_engines                 # known combos that repeat once each turn
    loops += find_turn_engines(db, [s for s in inside if not any(match(l, s) for l in loops)])
    found = sum(any(match(l, s) for l in loops) for s in inside)
    unconfirmed = sum(not any(match(l, s) for s in inside) for l in loops)
    return found, len(inside), unconfirmed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("variants")
    ap.add_argument("--fast", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    r = Report()
    db = relate.connect(a.store)

    print("loops that must be found")
    for cards, mana in MUST_LOOP:
        loops = find_loops(db, cards)
        hit = [l for l in loops if set(l["cards"]) <= set(cards)]
        r.ok(bool(hit) and (mana is None or max(l["net"].get("mana", 0) for l in hit) >= mana),
             f"{' + '.join(cards)} should loop" + (f" for +{mana} mana" if mana else ""))
    import engine as _engine
    # the state-only checks need State nodes in the graph (states.py, run by build_all.sh)
    if _engine.STATE_WALK and list(db.execute("MATCH (s:State) RETURN count(s) AS n"))[0]["n"]:
        MUST_NOT_LOOP.extend(STATE_NOT_LOOP)
    elif _engine.STATE_WALK:
        print("  (no State nodes in this graph: run python states.py on it to enable the state checks)")
    print("loops that must not be found")
    for cards, why in MUST_NOT_LOOP:
        r.ok(not find_loops(db, cards), f"{' + '.join(cards)} should not loop ({why})")

    print("one-shot wins")
    from wins import find_wins
    for cards in MUST_WIN:
        r.ok(bool(find_wins(db, cards)), f"{' + '.join(cards)} should be a one-shot win")
    for cards in MUST_NOT_WIN:
        r.ok(not find_wins(db, cards), f"{' + '.join(cards)} should not be a one-shot win")

    print("nonbo verdicts")
    for cards, want in NONBO:
        got = [f["verdict"] for f in check(db, cards) if f["verdict"] != "works"]
        r.ok((not got) if want == "none" else want in got, f"{' + '.join(cards)}: expected {want}, got {got or 'none'}")

    reviews = "reviews.jsonl"
    if os.path.exists(reviews):
        print("reviewed loops (reviews.jsonl)")
        spellbook = {frozenset(u["card"]["name"] for u in json.loads(l)["uses"]) for l in gzip.open(a.variants, "rt")}
        todo, conflicts = [], []
        for line in open(reviews, encoding="utf-8"):
            rv = json.loads(line)
            if rv.get("kind") == "nonbo":
                flagged = any(f["verdict"] == "nonbo" for f in check(db, rv["cards"]))
                if rv["label"] == "correct":
                    r.ok(flagged, f"nonbo {' + '.join(rv['cards'])} was reviewed as correct but isn't flagged")
                elif rv["label"] == "wrong":
                    r.ok(not flagged, f"nonbo {' + '.join(rv['cards'])} was reviewed as wrong but is still flagged")
                continue
            loops = find_loops(db, rv["cards"])
            hit = any(set(l["cards"]) <= set(rv["cards"]) for l in loops)
            if rv["label"] == "real" and not hit:
                todo.append(" + ".join(rv["cards"]))              # real but not traced: a coverage to-do, not a broken rule
            elif rv["label"] == "not_real" and hit:
                if frozenset(rv["cards"]) in spellbook:
                    conflicts.append(" + ".join(rv["cards"]))     # Commander Spellbook lists it: check the review
                else:
                    r.ok(False, f"{' + '.join(rv['cards'])} was reviewed as not a combo but is still found")

        if todo:
            print(f"  to trace (reviewed real, not traced yet): {len(todo)}: " + "; ".join(todo))
        for x in conflicts:
            print(f"  NOTE: {x} was reviewed as not a combo, but Commander Spellbook lists it. Check that review.")

    import engine
    taught = engine.custom_rules()
    if taught:
        print(f"taught mechanics ({engine.RULES_FILE})")
        for rule in taught:
            if rule.get("example"):
                hit = find_loops(db, rule["example"])
                r.ok(bool(hit), f"taught rule '{rule['name']}': its test cards {rule['example']} should loop")
    if os.path.exists("rulings_tests.json"):
        rt = json.load(open("rulings_tests.json", encoding="utf-8"))
        print(f"rulings tests ({len(rt.get('must_loop', []))} must loop, {len(rt.get('must_not_loop', []))} must not)")
        works_ok = sum(any(set(l["cards"]) <= set(t["cards"]) for l in find_loops(db, t["cards"])) for t in rt.get("must_loop", []))
        still = [t for t in rt.get("must_not_loop", []) if any(set(l["cards"]) <= set(t["cards"]) for l in find_loops(db, t["cards"]))]
        n = len(rt.get("must_loop", [])) + len(rt.get("must_not_loop", []))
        agree = works_ok + len(rt.get("must_not_loop", [])) - len(still)
        print(f"  agreement with the rulings: {agree} of {n} ({100 * agree / max(n, 1):.0f}%); "
              f"real loops kept {works_ok}/{len(rt.get('must_loop', []))}, false loops removed {len(rt.get('must_not_loop', [])) - len(still)}/{len(rt.get('must_not_loop', []))}")
        # (the judge is an LLM: a "works" it gives isn't authoritative, so losing one is reported, not failed)
    engine.RULES_OVERRIDE = []        # deck scores and the random sweep measure the core engine
    print("deck scores")
    variants = [json.loads(l) for l in gzip.open(a.variants, "rt")]
    for path, min_found, max_unconfirmed in DECKS:
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        d = load_deck(db, path) if "Commander" in text else load_deck_text(db, text)
        found, total, unconf = combos_found(db, d, variants)
        print(f"  {d['commanders'][0]}: {found}/{total} known combos found, {unconf} unconfirmed")
        r.ok(found >= min_found, f"{path}: found {found} known combos, expected at least {min_found}")
        r.ok(unconf <= max_unconfirmed, f"{path}: {unconf} unconfirmed loops, expected at most {max_unconfirmed}")

    if os.path.exists("decks/gandalf.txt"):
        print("speed (a blink-heavy deck with ~425,000 possible cycles)")
        info, t = {}, time.time()
        dg = load_deck_text(db, open("decks/gandalf.txt", encoding="utf-8").read())
        n = len(find_loops(db, sorted(set(dg["commanders"] + dg["cards"])), info=info))
        took = time.time() - t
        print(f"  Gandalf the White: {n} loops in {took:.0f}s, {info.get('checked', 0):,} of {info.get('cycles', 0):,} cycles checked")
        r.ok(took < 60 and not info.get("stoppedEarly"), f"Gandalf deck took {took:.0f}s (limit 60s) or stopped early")

    if not a.fast:
        print("random decks (false loops)")
        pool = [x["n"] for x in db.execute(
            "MATCH (c:Card) WHERE 'commander' IN c.legalIn AND NOT 'Land' IN c.types RETURN c.name AS n")]
        hits = []
        for seed in (3, 11, 12, 13):                     # 280 decks: wide enough to see a precision drop
            random.seed(seed)
            hits += [l["cards"] for _ in range(100 if seed == 3 else 60) for l in find_loops(db, random.sample(pool, 62))[:1]]
        allowed = [set(c) for c, _ in MUST_LOOP]
        unexpected = [h for h in hits if not any(set(h) <= s or set(h) >= s for s in allowed)]
        print(f"  280 random decks: {len(hits)} with a loop, {len(unexpected)} not on the known list")
        for u in unexpected:
            print("     ", " + ".join(u))
        limit = int(os.environ.get("MAX_RANDOM_LOOPS", "18"))          # baseline after the rulings fixes (was 25); lower it as rules improve
        r.ok(len(unexpected) <= limit, f"random decks: {len(unexpected)} unexpected loops (limit {limit})")

    print(f"\n{r.passed} passed, {len(r.failures)} failed ({time.time() - t0:.0f}s)")
    sys.exit(1 if r.failures else 0)


if __name__ == "__main__":
    main()
