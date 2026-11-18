# MTGraphRAG: MTG synergy graph on Relate

Upload a Commander deck and get an instant analysis of its combos, nonbos, synergies and the cards one
step away from a combo, all from a labeled property graph that runs locally in [Relate](https://relatedb.ai).

## Quick start
    git clone https://github.com/<owner>/<repo>.git && cd <repo>
    make start

Needs Python 3.10+ and `make` (macOS and Linux have both; on Windows use WSL). `make start` creates a
virtual environment, installs three packages (relate-db, numpy, scipy), downloads the prebuilt graph and
Commander Spellbook combos from the GitHub Release (checksum-verified, once), and opens
http://localhost:8000. No API key, and nothing leaves your machine: the graph runs in memory on your device.

Choose "Analyse your deck" and paste a decklist or upload an export from Moxfield, Archidekt or MTG Arena.
Names are matched in any case and by front face, sideboards are skipped, and unknown cards come back with
a suggestion ("Muldrifter (did you mean Mulldrifter?)"). Six sample decks are built in.

    make start PORT=8080     # another port
    make test                # the regression checks
    make help                # everything else

## Rebuild the graph yourself (make build)
Download Scryfall's Default Cards bulk file and Commander Spellbook's variants export into this folder, with
the LLM readings (extracted_*.jsonl), then:

    make build CARDS=default-cards-YYYYMMDD.jsonl VARIANTS=variants.json.gz
    # later runs: make start (keeps the graph you built; `make data` never overwrites it)

To share a rebuilt graph: stop the server, then `make release` packages mtg_live.rl and variants_slim.jsonl.gz,
uploads them to a GitHub Release (needs the gh CLI) and writes data_manifest.json. Commit that file and push.

## Setup (virtual environment)
    bash setup_venv.sh               # creates .venv with the core packages (relate-db, scipy, anthropic, pypdf...)
    bash setup_venv.sh embeddings    # also sentence-transformers, for local rule embeddings
    source .venv/bin/activate        # in each new terminal
Keeps this project's packages out of Anaconda's base environment. The Comprehensive Rules text is included
(comprules_20260807.txt), so nothing needs pdftotext.

## Test suite
    python checks.py mtg_live.rl variants_slim.jsonl.gz          # ~30 s, exit code 0 = all pass
    python checks.py mtg_live.rl variants_slim.jsonl.gz --fast   # skip the random-deck sweep

66 checks: 20 real combos that must be found, 30 look-alikes that must not (each named with the rule
it guards), one-shot wins, nonbo verdicts, per-deck minimum scores and maximum unconfirmed loops,
100 random decks, and every review in reviews.jsonl. Results are deterministic (fixed search order).

## One-shot wins (wins.py)
Non-loop wins by wording: empty-library wins (Thassa's Oracle / Laboratory Maniac / Jace + Demonic
Consultation / Tainted Pact) and draw punishers (Orcish Bowmasters + Peer into the Abyss). Across the
card pool: 24 pairs, 21 listed by Commander Spellbook; Inverter of Truth is excluded (it refills the library).

## Balance engine (multi-use loops)
Some loops fire an ability twice per lap: sacrifice Reassembling Skeleton and Ogre Slumlord's Rat,
Pitiless Plunderer makes two Treasures, pay {1}{B}. Cycles short on a resource become seeds for one
small integer program each (SciPy milp): whole-number counts per ability and per trigger link, with
targeted events (one card per activation, two for Ghostly Flicker) separate from broadcast ones ("a
creature dies"), nontoken triggers limited to nontoken creatures, spells and tap abilities once per
loop, and a counter reset's own cost charged to the loop. Results then pass every usual rule.
Finds Ogre Slumlord, Pawn of Ulamog and Requiem Angel + Phyrexian Altar + Reassembling Skeleton loops.

## Pattern loops and damage triggers
Loops through casting cards rather than abilities are matched by wording (wins.py, find_patterns):
Cloudstone Curio + two zero-cost nonartifact creatures, Hullbreaker Horror + a {0} permanent,
Food Chain + a creature you can cast from exile (Squee). Damage triggers: "whenever enchanted /
paired creature or a source you control deals damage" fires from any non-combat damage; "whenever
this creature deals damage" only from its own (or, if granted, its bearer's); combat damage never loops.
Fresh decks: Vivi Ornitier 0 -> 5 of 7 known combos traced, Flubs, the Fool 0 -> 7 of 12.
Known combos the graph can't trace are still listed as combos, marked as such.

## Coverage benchmark
    python coverage.py mtg_live.rl variants_slim.jsonl.gz [--show 30 --category counters]
A fixed sample of 300 Commander Spellbook combos (2-4 named cards). The app lists every known combo in a
deck; this measures how many the graph can also trace step by step. 12.0% -> 13.0% after adding: counters
firing "counters are put on" (Herd Baloth), token copies (Kiki-Jiki + Pestermite), casting from the graveyard
with "as long as you control a Zombie" (Gravecrawler + Phyrexian Altar + a Zombie), and creatures that
re-enter coming back untapped (Kiki-Jiki + Restoration Angel). Cost: false loops in 280 random decks rose
from 14 to 25 (full enrichment), so these mechanics need a precision pass before they're "production".
Misses concentrate in counters, untapping, library tricks, storm and cost reduction; each mechanic adds
roughly 0.3-1% of random combos, so closing the gap means dozens of mechanics (see Teach a mechanic).

## Rulings as tests
    python comprules.py MagicCompRules_<date>.pdf                          # keyword sections -> comprules_keywords.json
    python load_rulings.py mtg_live.rl rulings-<date>.jsonl comprules_keywords.json
    python rulings_tests.py build  mtg_live.rl rulings-<date>.jsonl          # every loop the graph finds
    python rulings_tests.py run                                              # Claude judges each (API key)
    python rulings_tests.py report mtg_live.rl                               # agreement rate + fixes
The graph gains (Card)-[:HAS_RULING]->(Ruling) for Scryfall's 77,646 rulings and (Keyword)-[:DEFINED_BY]->(Rule)
for the Comprehensive Rules' 248 keyword sections. Each loop the graph finds is judged against the cards' text,
their official rulings and the relevant rules: "works" becomes a must-loop check, "fails" a must-not-loop check
with its citation (rulings_tests.json, run by checks.py).

## Rules embeddings (Relate vector search)
    pip install openai && export OPENAI_API_KEY=...                      # or --backend st (local) / tfidf (no download)
    python embed_rules.py build mtg_live.rl MagicCompRules_<date>.pdf      # ~3,000 numbered rules -> (:RuleText), < $0.01
    python embed_rules.py ask   mtg_live.rl "a token copy is exiled and returned"
Each numbered rule is a RuleText node with an embedding and a vector index (rule_text_vec); keyword sections link to
their parts. rulings_tests.py gives the judge the closest rules plus core interaction rules (400.7 new objects,
111.7 tokens, 701.21a sacrifice, 122.3 counters...). Vectors find rules to show; they never decide a combo.
Re-judge with the richer context into a new file to compare:  python rulings_tests.py run --fresh --out rulings_verdicts_v2.jsonl

## States in the graph
    python states.py mtg_live.rl          # run by build_all.sh after load_llm.py
(:State) nodes (battlefield, graveyard, exile, hand, held) and (Ability)-[:REQUIRES_STATE|CAUSES_STATE|CLEARS_STATE
{who, by, via}]->(State): who is self, target, that card, held cards, sacrificed or copy; by names a holder.
    MATCH (c:Card {name: "Fiend Hunter"})-[:HAS_ABILITY]->(a)-[r]->(s:State) RETURN a.text, type(r), s.name, r.who, r.by

## The state walk (on by default; MTG_STATES=0 turns it off)
    python diag_states.py mtg_live.rl "Card A+Card B"     # why a loop is kept or rejected, step by step
Each step of a loop REQUIRES, CAUSES and CLEARS states: a card's zone (battlefield, graveyard) and "held in exile
by X". The loop is walked in order from every starting arrangement of its few cards, exploring which card each
effect targets; it's real only if every step holds and it ends where it started. Settles Felidar Guardian +
Lumbering Battlement (and the Battlement variants) with no special rule, keeps Felidar + Restoration Angel,
Abdel Adrian + Animate Dead, Fiend Hunter + Phyrexian Altar + Sun Titan, Karmic Guide + Reveillark + Viscera Seer.
Gandalf deck: 166 -> 94 loops. Still to fix before it's the default: Leonin Relic-Warder + Dance of the Dead,
Kaya's Ghostform + Sun Titan + Viscera Seer, two Lotho combos, Oblivion Ring target timing (603.3d), and speed.

## State traces of known combos
    python trace_combos.py fixtures                                   # 4 hand-written examples: verify the walker
    python trace_combos.py build mtg_live.rl variants_slim.jsonl.gz   # the sample decks' known combos -> requests
    python trace_combos.py run                                        # an LLM turns Spellbook's steps into State steps
    python trace_combos.py verify                                     # walk every translation -> combo_traces.json
    python learn_mechanics.py mtg_live.rl                             # mechanics behind verified combos the graph can't trace
Starting zones come from Commander Spellbook (slim_variants.py keeps each card's zone and state, prerequisites, mana and
steps). Each step requires zones and minimum counts and causes moves and count changes; three laps give infinite,
near-infinite (a finite pool runs out), not sustainable, or fails (which step, why). The combo view shows the trace.

## Teach by drawing (the Teach tab, local app)
Pick any cards, then draw what their abilities do: click an ability, then the Event, Resource or State it connects to,
and choose how in plain words ("opponent loses life sets off Mindcrank's ability"). Columns or Cycle layout, draggable,
hover to highlight. Check in the graph (a Cypher query per connection), Test the loop, Save. Three guided tutorials:
See a cycle (Sanguine Bond + Exquisite Blood), Teach a new combo (Bloodchief Ascension + Mindcrank, a mill loop the
graph can't trace until you add the missing event), Teach by wording (cast from the graveyard).
What you teach comes back into the graph:
  * a saved model keeps the loop its test found; deck analysis counts that combo as "confirmed by your model"
  * python bake_models.py mtg_live.rl (run by build_all.sh) writes models into Relate: relationships with
    source "user", and (:Combo {source: "user"})-[:USES]->(:Card) for each learned loop
  * extract.py shows saved models to the LLM as worked examples, so similar wording is read right next time

## Reviews
"Real combo / Not a combo" and "Correct / Wrong" buttons in the app. Locally they append to
reviews.jsonl and checks.py turns each into a permanent test; on the shared link they stay in the browser.

## Next best card (suggest.py, eval_suggest.py)
score = 3*(1 + log2 known combos completed) [+4 for a two-card combo] + 4*new graph loops (capped at 5)
        + 0.5*links to the deck - 6*rules conflicts
Leave-one-out test (remove a combo piece from a sample deck; where does it rank?), 32 trials:

| Ranking | top 1 | top 5 | top 10 | median rank |
|---|---|---|---|---|
| Spellbook count only | 6% | 22% | 34% | 14 |
| Spellbook, weighted | 6% | 31% | 44% | 13.5 |
| Graph only | 0% | 25% | 28% | 38 |
| Combined | 12% | 38% | 59% | 8 |

Two-card combo pieces always get the loop search: Sanguine Bond and Exquisite Blood moved from about
#170 to about #70. Still not top 10: a deck like Teysa has dozens of cards that complete many combos.

## Full-pool enrichment
    python enrich_all.py mtg_live.rl --have extracted_sonnet.jsonl extracted_sonnet_lotho.jsonl
    python extract.py submit  --model haiku --requests extract_requests_all.jsonl --tag all
    python extract.py collect --model haiku --requests extract_requests_all.jsonl --tag all --wait
29,959 cards, about $2.55 with Haiku (which ties Sonnet once the rules are applied).

## Enriching a new deck
Only cards the LLM hasn't read yet cost anything. For a new deck:

    python enrich_deck.py mtg_live.rl variants_slim.jsonl.gz decks/lotho.txt --have extracted_sonnet.jsonl --name lotho
    python extract.py submit  --model sonnet --requests extract_requests_lotho.jsonl --tag lotho
    python extract.py collect --model sonnet --requests extract_requests_lotho.jsonl --tag lotho --wait
    bash build_all.sh default-cards-YYYYMMDD.json variants.json.gz extracted_sonnet.jsonl,extracted_sonnet_lotho.jsonl

enrich_deck.py picks the deck's cards, the cards one away from a known combo with it (in the
commander's colours), and functional neighbours; skips anything already in the --have files; and
writes a batch request file for the rest. Lotho, Corrupt Shirriff: 215 relevant cards, 83 already
enriched, 132 new (about $0.28 with Sonnet). Decklists whose commander sits alone at the top
(Moxfield-style) are recognised without a "Commander" line.

Rules added while bringing the Lotho deck from 0 to 7 of its 9 Spellbook combos (rules alone,
before new enrichment), each prompted by a real deck or a random-deck false loop:
- exiling a permanent makes it leave the battlefield (firing its own "leaves" trigger)
- "exile ... until this leaves the battlefield" implies a return when the exiler leaves
- exile, flicker and sacrifice effects only reach the card types they name, and never your own
  cards when they say "an opponent controls"
- returns "at the next end step" or "when this leaves" are too late to keep a loop going
- every triggered ability in a loop must be fired by the loop; a combo takes at least two cards
- loyalty abilities and "only once each turn" can't loop; Broodmoth-style returns and cards that
  come back as something else work once
- "you may pay {4}" inside a trigger is a cost; returning a creature to hand means paying its
  mana value to cast it again (Palinchron + Panharmonicon: +3 mana per loop)
- mana colours: a loop must make the colours its costs need (Ashnod's Altar's colourless mana
  can't pay Reassembling Skeleton's {B}); untapping lands makes the deck's colours
- dying is leaving the battlefield (Viscera Seer sacrificing Reveillark fires its "leaves" trigger)
- returns check printed limits: Reveillark only returns power 2 or less, Sun Titan mana value 3 or less;
  Karmic Guide only creature cards
- every ability on an instant or sorcery is part of a one-shot spell (Village Rites isn't an outlet)
- nonbos: preventions only flag triggers about the players they affect (Dauthi Voidwalker vs
  Necropotence is fine), catch-all "other" events never link cards, and an ability that still
  triggers another way gets a note, not a nonbo
- loops through trigger chains are searched up to six abilities (three cards exiling and
  returning each other), other loops up to four
Random 62-card decks: 1 loop in 200 (Lumbering Battlement + Wispweaver Angel, a real combo core
that needs a flicker to start).

## Build
    pip install -r requirements.txt
    python slim_cards.py default-cards-YYYYMMDD.json cards.jsonl.gz   # 538 MB -> 4.7 MB
    python build_graph.py cards.jsonl.gz mtg.rl                       # ~20 s
    python tag_functions.py mtg.rl                                    # ~10 s, card-level tags (v1)
    python abilities.py mtg.rl                                        # ~20 s, ability graph (v2)
    python rules_facts.py mtg.rl                                      # ~2 s, rules distinctions (rerun after abilities.py)
    python engine.py mtg.rl                                           # loop acceptance checks
    python nonbo.py mtg.rl                                            # nonbo paired checks

Input: a Scryfall bulk file (Default Cards or Oracle Cards) from https://scryfall.com/docs/api/bulk-data.
Card data is not committed: Scryfall's terms ask that it not simply be republished.

## Graph (33,680 playable cards)
    (:Card)-[:HAS_SUBTYPE]->(:Subtype)       511 subtypes (tribal synergy)
    (:Card)-[:HAS_KEYWORD]->(:Keyword)       730 keywords
    (:Card)-[:CREATES]->(:Token)             717 token kinds, 3,802 links
    (:Card)-[:USES_MECHANIC]->(:Mechanic)    14 rules-helper mechanics (Morph, Foretell, The Monarch...)
    (:Card)-[:REFERENCES]->(:Card)           1,598 named references

Types, supertypes, colour identity and legality are list properties on Card.
`canBeCommander` marks legendary creatures and cards that say they can be your commander.

## Function tags
    (:Card)-[:PRODUCES {rule}]->(:Resource)<-[:USES {rule}]-(:Card)

13 resources: creature, death, token, artifact, etb, flicker, mana, card_draw, graveyard,
counters, lifegain, spells, landfall. Tags come from regexes over rules text (reminder
text removed, card name replaced by ~), plus keywords, card types and CREATES edges.
Every edge stores the rule that made it. Effects aimed at opponents ("that player
discards") and non-creature self-sacrifice are filtered out.

Result: 20,872 of 33,680 cards tagged, 37,995 edges. Precision on a fresh random
sample of 40 tags: 38/40 correct before the last fixes. Recall is not yet measured.

Find 3-card engines in a deck:

    MATCH (a:Card)-[:PRODUCES]->(r1:Resource)<-[:USES]-(b:Card)
          -[:PRODUCES]->(r2:Resource)<-[:USES]-(c:Card)
    WHERE a.name IN $deck AND b.name IN $deck AND c.name IN $deck
      AND a <> b AND b <> c AND a <> c AND r1 <> r2
    RETURN a.name, r1.name, b.name, r2.name, c.name

## Flicker and mana loops
Flicker edges carry the details combos depend on:
- `PRODUCES flicker`: `kind` (activated / triggered / one-shot / on-enter), `activationCost`,
  `tapCost`, `scope` (self / other / granted, e.g. soulbond), `immediate` (vs. end-step return)
- `USES flicker` (a card's own enters effect): `yields` (what the effect produces), `manaYield`

Infinite-mana flicker loops (finds Deadeye Navigator + Peregrine Drake / Palinchron / Great Whale):

    MATCH (a:Card)-[p:PRODUCES]->(:Resource {name: 'flicker'})<-[u:USES]-(b:Card)
    WHERE p.kind = 'activated' AND NOT p.tapCost AND p.immediate AND p.scope <> 'self'
      AND u.manaYield > p.activationCost AND a <> b
    RETURN a.name, b.name

Not yet modelled: mana colours (Priest of Gix makes black, Deadeye needs blue) and
conditional enters effects ("if you cast it from your hand").

## Ability graph (v2)
Cards don't produce things; their abilities do. 49,392 abilities, labelled by kind:

    (:Card)-[:HAS_ABILITY]->(:Ability:Activated|Triggered|Static|Spell)
    (:Card)-[:GRANTS]->(:Ability)                   abilities in quotes (soulbond, "creatures have ...")
    (:Ability)-[:TRIGGERED_BY]->(:Event)            self_enters, self_leaves, self_dies, creature_enters,
                                                    creature_dies, creature_leaves, cast_spell, each_turn ...
    (:Ability)-[:COSTS {amount}]->(:Resource)       mana, creature (sacrifice), artifact
    (:Ability)-[:PRODUCES {amount, net, ...}]->(:Resource)
    (:Ability)-[:RETRIGGERS {scope, immediate}]->(:Event)   flicker, reanimation, sacrifice, return-exiled
    (:Ability:Static)-[:MODIFIES]->(:Event)         Panharmonicon-style doublers
    (:Ability)-[:RETURNS_FROM]->(:Ability)          old two-trigger exile wording (Fiend Hunter)

Activated abilities carry `costText`, `manaValue`, `tapCost`. Card draw carries `amount`,
`net` (after put-backs and discards), `discardsHand`, `opponentChoice`. Damage is split into
`damage_players` (can win) and `removal` (creatures only).

`engine.py` pulls a deck's abilities, links them by cause and effect (respecting self/other
scope), finds cycles, rejects loops that need tapping or can't pay for themselves, and reports
what each loop produces, including observers like Impact Tremors.

Acceptance checks (all pass):
1. Mulldrifter / Cloudblazer outrank "draw three, discard your hand" and put-back draws
2. Deadeye Navigator + Peregrine Drake + Mulldrifter -> infinite mana + card draw
3. Fiend Hunter's flicker-in-response trick detected; Banishing Light correctly excluded
4. Worldgorger Dragon + Animate Dead -> infinite loop
5. Deadeye Navigator + Peregrine Drake + Impact Tremors -> infinite damage

Negative controls (all correctly rejected): Conjurer's Closet, Restoration Angel, Aetherling,
Distinguished Conjurer and Angel of Condemnation with Peregrine Drake; Deadeye + Akki Rockspeaker.
Random 62-spell decks: 0/20 spurious loops. A 63-card deck analyses in ~0.01 s.

## Validation against Commander Spellbook
    curl -LO https://json.commanderspellbook.com/variants.json.gz      # ~32 MB, 735 MB unpacked
    python slim_variants.py variants.json.gz variants_slim.jsonl.gz   # streamed -> 3 MB
    python validate.py mtg.rl variants_slim.jsonl.gz --sample 2000

121,892 variants; 108,496 Commander-legal with all cards in the graph. Same 2,000-variant
sample (seed 0) for every run:

| Version | Specific cards found | Needs a generic card | Outcome agreement |
|---|---|---|---|
| v2.0 ability graph | 0.5% | 0.0% | 7/9 |
| v2.1 + tokens, self-recursion, persist/undying, loop-validity rules | 1.9% | 3.4% | 39/39 |

v2.1 loop-validity rules (each found by a false positive in random decks):
- a card's own enters/leaves/dies ability repeats only if the loop moves that card again
- "nontoken" triggers can't be fed by tokens alone
- persist/undying loop only with a real counter reset (Melira, etc.)
- abilities a card *gains* until end of turn don't survive a zone change, so they can't loop

Diagnosis of misses: in 400 sampled misses, only 1 was a loop rejected for cost; the rest
had no cycle at all, i.e. missing edges, not over-strict checks.

## Nonbos: look-alike synergies that don't work
Popularity data reflects what players *believe* works. Most nonbos are near misses between
two things players merge into one, so `rules_facts.py` splits each pair apart:

| Players merge | The graph keeps separate | Example nonbo | Works instead |
|---|---|---|---|
| "comes back from the graveyard" | enters from graveyard vs cast from graveyard | Flayer of the Hatebound + Chainer | Flayer + Animate Dead |
| "makes a token" | token created vs spell copy that becomes a token | Doubling Season + Volo | Doubling Season + Bitterblossom |
| "copies" | spell copy vs card copy | Twinning Staff + Shiko | Twinning Staff + Reverberate |
| "targets my creature" | targets only X vs also targets a player | Zada + Sazacap's Brew | Zada + Expedite / Searing Wind |
| "triggers on X" | X happens vs X is prevented (PREVENTS edges) | Notion Thief + Scrawling Crawler | Scrawling Crawler alone |
| "any colour" | commander colour identity | Arcane Signet + colourless commander | Arcane Signet + Atraxa |
| "exile it" | until-leaves wording vs two separate triggers | Banishing Light + flicker (note) | Fiend Hunter + flicker |

`nonbo.py` reports each finding as works / nonbo / note with a plain-English reason.
23/23 paired checks pass. Fresh random 62-spell decks produce only correct flags
(e.g. Tocatli Honor Guard, which has Torpor Orb's text, shutting off creatures' enters abilities).

Each false alarm along the way was another rule: Torpor Orb only stops *creatures* entering;
"any target" and modal spells can target only Zada; "up to one other target" is optional;
"if you cast it from your graveyard" is a condition, not permission to cast.

Known limits: timing-based nonbos (costs paid before triggers resolve, as in Shadowheart +
Haunted One; simultaneous deaths, as in Reyhan + Elenda) aren't modelled. "Doesn't trigger"
can be good news when the trigger is a drawback (Treacherous Pit-Dweller).

## Sample decks
`decks/brago_blink.txt` (Brago, King Eternal: flicker) and `decks/teysa_aristocrats.txt`
(Teysa Karlov: sacrifice loops). Both validated with `python decks.py mtg.rl decks/*.txt`:
100 cards, Commander-legal, in colour identity, singleton.

| Deck | Loops found (minimal) | Confirmed by Spellbook | Spellbook combos in deck | Found |
|---|---|---|---|---|
| Brago | 9 | 8 | 21 | 8 |
| Teysa | 47 | 7 | 32 | 7 |

The nonbo checker flags Containment Priest in the Teysa deck: it exiles what Karmic Guide,
Reveillark, Sun Titan, Unearth, Victimize and the undying/persist creatures bring back.

Loop rules added from these decks: minimal loops only; the counter-reset card (Melira,
Yawgmoth) and doubler (Teysa, Panharmonicon) are named in the combo, but only when the loop
needs them; "another non-Spirit creature" can't be fed by its own Spirit tokens.

Known gaps these decks expose: mana colours (Reassembling Skeleton needs black; Ashnod's Altar,
Scions and Rats make colourless), one outlet used twice per cycle, recast loops (Ghostly
Flicker + Archaeomancer), bounce-and-replay (Whitemane Lion + Panharmonicon), counter arithmetic
(Mikaeus + Walking Ballista), life-total events (Sanguine Bond + Exquisite Blood).

## Enrichment subset
    python enrich_subset.py mtg.rl variants_slim.jsonl.gz decks/*.txt --neighbors 100

586 cards, each with its reason: deck cards (121), one-card-away combo pieces within the
commander's colours (314), and functional neighbours with the same ability pattern (151).
Estimated LLM extraction cost: ~$0.06 (Haiku 5.5 batch) to ~$1.26 (Sonnet 5.5 batch).
The 38 deck combos the regex graph misses are the before/after test set for extraction.

## LLM extraction (586-card subset, Haiku 5.5 vs Sonnet 5.5)
    python extract.py submit --model haiku     # forced tool call
    python extract.py submit --model sonnet    # Sonnet 5.5 rejects forced tool_choice: auto + instruction
    python extract.py collect --model haiku --wait
    python load_llm.py mtg.rl extracted_haiku.jsonl mtg_haiku_union.rl --mode union
    python compare.py variants_slim.jsonl.gz mtg.rl mtg_haiku_union.rl

| Version | Brago combos | Teysa combos | Total | Loop checks | Nonbo checks | Random-deck loops | Cost (586 cards) |
|---|---|---|---|---|---|---|---|
| Regex only | 9 / 21 | 13 / 32 | 22 / 53 | 5/5 | 23/23 | 0/30 | free |
| Haiku only | 13 / 21 | 17 / 32 | 30 / 53 | 3/5 | 23/23 | 0/30 | ~$0.06 |
| Sonnet only | 14 / 21 | 21 / 32 | 35 / 53 | 3/5 | 23/23 | 0/30 | ~$1 |
| **Regex + Haiku** | **15 / 21** | **21 / 32** | **36 / 53** | 5/5 | 23/23 | 0/30 | ~$0.06 |
| **Regex + Sonnet** | **15 / 21** | **21 / 32** | **36 / 53** | 5/5 | 23/23 | 0/30 | ~$1 |

(Re-scored after the Lotho-era rules below.) Teysa with rules + Sonnet: 34 loops, 21 confirmed, 13 unconfirmed
(was 77 loops, 62 unconfirmed). Random 62-card decks: 1 loop in 200, a real combo core.

Sonnet reads better alone (29 vs 25), but with rules + cleanup both reach 30: Haiku is the better
value for the full card pool. Neither model should run alone: both fail the same two known-combo
checks. (An earlier draft credited Sonnet with four extra Ogre Slumlord loops; that came from a
loader bug that counted one Treasure twice. Those loops need two deaths per cycle, which the
engine can't express yet.)

Both models read hard wording well but use precise fields loosely, and they shape answers
differently, so `load_llm.py` applies model-neutral cleanup:
- "prevents" only from static abilities saying "instead" / "don't cause abilities to trigger"
  (Haiku: Counterspell "prevents" casting)
- trigger doublers only from static abilities saying "an additional time" (Haiku: Pitiless Plunderer)
- "until this leaves" only if the text says "until" (Haiku: Fiend Hunter)
- "targets only" only if the text says so (Sonnet: Walking Ballista, Deadeye, Palinchron)
- "non-Spirit" / "nontoken" read from the text
- Treasure tokens count as mana, once (Sonnet described the token but not the mana)
- a static ability granting a costed ability is that activated ability (Sonnet's Deadeye shape)
- returning a creature/permanent card to the battlefield produces a creature that enters
- names given as objects ({"resource": "mana"}) are accepted (Sonnet)
Haiku returned one card (Emeritus of Truce) as unparseable text.

Engine additions: gaining life / drawing / making tokens fire the matching "whenever" triggers;
untapping pays for tap costs; returning an instant or sorcery lets a spell be cast again;
an Aura that leaves with its creature repeats only if the loop returns it; a card's own enters
ability repeats only if the loop takes it out and brings it back. Cycle search is capped at
4 abilities (every confirmed combo so far is <= 4), which keeps a deck under a second.
A spell in a loop needs its own returner on another card (Archaeomancer returns one instant per
trigger), and a two-target flicker (Ghostly Flicker) also re-fires a second creature's enters
ability, reported as one combo per creature that makes the loop pay. With rules + Sonnet, the
Brago deck has 20 loops, 14 of them confirmed by Spellbook (down from 47 loops, 12 confirmed).

## Seeing the graph
- In the demo app, the Graph tab draws the schema (8 node labels, 16 relationship patterns, with
  counts) and the subgraph behind any combo or the whole deck (D3, drag and zoom).
- Live: Relate speaks Bolt, so Neo4j Browser can open the real graph.

      curl -sSfL https://relatedb.ai/install.sh | bash
      relate serve --database mtg.rl --bolt 7688      # 7688 avoids a Neo4j Desktop DBMS on 7687

  Connect to bolt://localhost:7688 as user `relate` with the token printed at startup, then run
  `CALL db.schema.visualization()` or the Deadeye Navigator + Peregrine Drake loop query from the app.
- `python graph_data.py mtg_sonnet_union.rl demo_data.json` adds the schema and deck graphs to the app data.

## Notes
- One node per unique card (oracle_id), most recent paper printing.
- Excluded: art series cards, Planechase planes, Vanguard, schemes, emblems.
- Scryfall's `combo_piece` component mostly means "creates or references", not a combo.
  Token links become CREATES; rules-helper links become USES_MECHANIC.

Card data from Scryfall. Unofficial Fan Content permitted under the Wizards of the Coast
Fan Content Policy. Not approved/endorsed by Wizards.
