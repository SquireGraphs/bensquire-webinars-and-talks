"""LLM extraction of card abilities into the graph's ability schema, via the Message Batches API.

    python extract.py build   mtg.rl enrich_subset.csv            # writes requests, no API calls
    python extract.py submit  --model haiku                        # needs ANTHROPIC_API_KEY
    python extract.py submit  --model sonnet
    python extract.py status  --model haiku
    python extract.py collect --model haiku                        # -> extracted_haiku.jsonl

One request per card. The model must answer by calling the `record_abilities` tool,
whose input schema mirrors the ability graph, so every result is structured JSON.
Batches run asynchronously at half price and usually finish well inside 24 hours.
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}
REQUESTS = Path("extract_requests.jsonl")

EVENTS = ["self_enters", "self_leaves", "self_dies", "self_tapped", "creature_enters", "creature_leaves",
          "creature_dies", "enters_from_graveyard", "cast_from_graveyard", "land_enters", "cast_spell",
          "you_gain_life", "opponent_loses_life", "you_draw", "opponent_draws", "token_created",
          "each_turn", "attacks", "combat_damage", "artifact_enters", "artifact_leaves", "other"]
RESOURCES = ["creature", "token", "artifact", "mana", "card_draw", "graveyard", "plus_counters",
             "minus_counters", "lifegain", "life_loss_opponents", "damage_players", "removal", "landfall",
             "untap", "spell_copy", "card_copy", "return_to_hand", "cast_from_graveyard", "extra_turn",
             "win_game", "other"]
SCOPES = ["self", "other", "granted", "any", "copy"]

TOOL = {
    "name": "record_abilities",
    "description": "Record every ability of one Magic card in the graph schema.",
    "input_schema": {
        "type": "object",
        "required": ["abilities"],
        "properties": {"abilities": {"type": "array", "items": {
            "type": "object",
            "required": ["text", "kind"],
            "properties": {
                "text": {"type": "string", "description": "The ability's rules text, verbatim."},
                "kind": {"enum": ["Activated", "Triggered", "Static", "Spell"]},
                "granted": {"type": "boolean", "description": "Ability given to other objects (in quotes)."},
                "temporary": {"type": "boolean", "description": "Granted only until end of turn (\"gains\")."},
                "trigger": {"type": "object", "properties": {
                    "events": {"type": "array", "items": {"enum": EVENTS}},
                    "nontoken": {"type": "boolean"},
                    "excludesType": {"type": ["string", "null"], "description": "e.g. 'spirit' for 'another non-Spirit creature'."},
                    "condition": {"type": ["string", "null"]}}},
                "cost": {"type": "object", "properties": {
                    "mana": {"type": "integer", "description": "Mana value of the cost; 0 if none."},
                    "colors": {"type": "array", "items": {"enum": ["W", "U", "B", "R", "G", "C"]}},
                    "tap": {"type": "boolean"},
                    "sacrifice": {"enum": ["none", "self", "creature", "artifact", "other"]},
                    "other": {"type": ["string", "null"]}}},
                "produces": {"type": "array", "items": {"type": "object", "required": ["resource"], "properties": {
                    "resource": {"enum": RESOURCES},
                    "amount": {"type": ["integer", "null"]},
                    "variable": {"type": "boolean", "description": "Amount depends on X, devotion, power, etc."},
                    "manaColors": {"type": "array", "items": {"enum": ["W", "U", "B", "R", "G", "C", "any"]}},
                    "net": {"type": ["integer", "null"], "description": "Card draw net of discards/put-backs."},
                    "token": {"type": "boolean"},
                    "tokenDesc": {"type": ["string", "null"]},
                    "duration": {"type": ["string", "null"], "description": "'until_source_leaves' for O-Ring-style exile."}}}},
                "uses": {"type": "array", "items": {"enum": RESOURCES}},
                "retriggers": {"type": "array", "items": {"type": "object", "required": ["event", "scope"], "properties": {
                    "event": {"enum": EVENTS}, "scope": {"enum": SCOPES}, "immediate": {"type": "boolean"}}}},
                "prevents": {"type": "array", "items": {"type": "object", "required": ["event"], "properties": {
                    "event": {"enum": EVENTS}, "whose": {"enum": ["all", "opponents", "you", "creatures"]},
                    "reason": {"type": "string"}}}},
                "amplifies": {"type": "array", "items": {"enum": RESOURCES + EVENTS}},
                "targets": {"type": "array", "items": {"type": "string"},
                            "description": "Required targets only (skip 'up to'), e.g. ['creature'] or ['player','creature']."},
                "requiresTargetsOnlySelf": {"type": "boolean"},
                "needsCommanderIdentity": {"type": "boolean"},
                "returnsExiledFrom": {"type": ["integer", "null"],
                                      "description": "Index of the ability whose exiled card this one returns (old Fiend Hunter wording)."},
            }}}},
    },
}

def user_examples(path="custom_model.json"):
    """Readings users taught by drawing (custom_model.json), as worked examples: cards worded like these should be
    read the same way, so the next extraction gets them right without anyone drawing them again."""
    import json
    try:
        models = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    by_text = {}
    for m in models:
        for e in m.get("edges", []):
            if e.get("text"):
                key = (e.get("card", ""), e["text"])
                t = str(e.get("t", ""))
                by_text.setdefault(key, []).append(f"{e.get('type')} {t[2:] if t.startswith('s:') else t}")
    if not by_text:
        return ""
    lines = [f'- {card}: "{text}" -> ' + "; ".join(rels) for (card, text), rels in list(by_text.items())[:40]]
    return "\n\nReadings taught by users (read cards with similar wording the same way):\n" + "\n".join(lines)


SYSTEM = """You convert Magic: The Gathering cards into a graph of abilities for a combo and nonbo analyser.
Record every ability of the card, one per entry, in the order printed. Keyword abilities with reminder
text (persist, undying, flashback, storm...) are recorded as the ability they stand for.

The analyser finds loops (what one ability produces, another uses or is triggered by) and nonbos
(look-alikes that don't work). Be exact about the distinctions players get wrong:
- Entering FROM the graveyard (put/return onto the battlefield) is not being CAST from the graveyard.
  A creature cast from the graveyard enters from the stack.
- Creating a token is not copying a spell. A copy of a creature spell becomes a token but is not "created".
- Copying a spell is not copying a card (Shiko copies a card, then you cast the copy).
- Copies of spells are not cast.
- "Gains" an ability until end of turn: temporary=true. "Has" / "have": permanent.
- Costs ({T}, sacrifice, mana) are paid before any trigger they cause resolves.
- Flicker (exile then return) retriggers self_leaves and self_enters of the card it moves.
  Returning at "the beginning of the next end step" is immediate=false.
- Untapping a permanent produces "untap"; a {T} cost needs one untap to be repeated.
- "Return ... to its owner's hand" produces return_to_hand (it can be recast).
- Damage to "any target"/"each opponent"/"target player" can kill: damage_players.
  Damage only to creatures is removal.
- Replacement or static effects that stop events (Torpor Orb, Rest in Peace, Notion Thief) go in prevents.
- Effects that change how much of something you get (Doubling Season, Panharmonicon, Teysa Karlov,
  Twinning Staff) go in amplifies.
- For mana, record the amount and colours produced; for costs, the mana value and colours required.
Use "other" only when nothing else fits. Never invent abilities the card doesn't have."""

EXAMPLE = """Example. Card: Peregrine Drake | Creature — Drake | {4}{U}
Flying
When this creature enters, untap up to five lands.
-> abilities: [
 {"text": "Flying", "kind": "Static"},
 {"text": "When this creature enters, untap up to five lands.", "kind": "Triggered",
  "trigger": {"events": ["self_enters"]},
  "produces": [{"resource": "untap", "amount": 5}, {"resource": "mana", "amount": 5, "manaColors": ["any"]}]}]"""


def card_prompt(c):
    return (f"Card: {c['name']} | {c['typeLine']} | {c['manaCost'] or ''}\n"
            f"Keywords: {', '.join(c['keywords'] or []) or 'none'}\n{c['oracleText'] or ''}")


def build(store, subset):
    import relate
    names = [r["card"] for r in csv.DictReader(open(subset, encoding="utf-8"))]
    db = relate.connect(store)
    cards = list(db.execute("""MATCH (c:Card) WHERE c.name IN $n
        RETURN c.oracleId AS id, c.name AS name, c.typeLine AS typeLine, c.manaCost AS manaCost,
               c.oracleText AS oracleText, c.keywords AS keywords""", {"n": names}))
    with REQUESTS.open("w", encoding="utf-8") as f:
        for c in cards:
            f.write(json.dumps({"custom_id": c["id"], "card": c["name"], "prompt": card_prompt(c)}) + "\n")
    chars = sum(len(card_prompt(c)) for c in cards)
    print(f"wrote {len(cards)} requests to {REQUESTS} (of {len(names)} subset cards); "
          f"~{chars // 4:,} card tokens + a shared, cached instruction prompt")


# Models that reject a forced tool call ("tool_choice: type tool and any are not supported").
AUTO_TOOL_CHOICE = {"sonnet"}


def params(model, prompt):
    system = SYSTEM + "\n\n" + EXAMPLE + user_examples()
    if model in AUTO_TOOL_CHOICE:
        system += "\n\nAnswer only by calling the record_abilities tool, exactly once. Do not reply in text."
    return {
        "model": MODELS[model],
        "max_tokens": 8000 if model in AUTO_TOOL_CHOICE else 4000,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "tools": [TOOL],
        "tool_choice": ({"type": "auto"} if model in AUTO_TOOL_CHOICE
                        else {"type": "tool", "name": "record_abilities"}),
        "messages": [{"role": "user", "content": prompt}],
    }


def batch_file(model, tag=""):
    return Path(f"batch_{model}{'_' + tag if tag else ''}.json")


def submit(model, requests=REQUESTS, tag=""):
    import anthropic
    client = anthropic.Anthropic()
    reqs = [json.loads(l) for l in Path(requests).open(encoding="utf-8")]
    batch = client.messages.batches.create(requests=[
        {"custom_id": r["custom_id"], "params": params(model, r["prompt"])} for r in reqs])
    batch_file(model, tag).write_text(json.dumps({"id": batch.id, "model": MODELS[model], "n": len(reqs)}))
    print(f"submitted {len(reqs)} requests as batch {batch.id} ({MODELS[model]})")


def status(model, tag=""):
    import anthropic
    info = anthropic.Anthropic().messages.batches.retrieve(json.loads(batch_file(model, tag).read_text())["id"])
    print(f"{info.id}: {info.processing_status}  {info.request_counts}")
    return info.processing_status


def collect(model, wait, requests=REQUESTS, tag=""):
    import anthropic
    client = anthropic.Anthropic()
    batch_id = json.loads(batch_file(model, tag).read_text())["id"]
    while wait:
        info = client.messages.batches.retrieve(batch_id)
        print(f"{time.strftime('%H:%M:%S')} {info.processing_status} {info.request_counts}", flush=True)
        if info.processing_status == "ended":
            break
        time.sleep(60)
    names = {r["custom_id"]: r["card"] for r in map(json.loads, Path(requests).open(encoding="utf-8"))}
    ok, failed, usage = 0, [], {"input": 0, "output": 0, "cache_read": 0}
    out_path = f"extracted_{model}{'_' + tag if tag else ''}.jsonl"
    with open(out_path, "w", encoding="utf-8") as out:
        for r in client.messages.batches.results(batch_id):
            if r.result.type != "succeeded":
                err = getattr(r.result, "error", None)
                detail = getattr(getattr(err, "error", err), "message", None) or str(err or "")
                failed.append((names.get(r.custom_id), r.result.type, detail))
                continue
            msg = r.result.message
            tool = next((b for b in msg.content if b.type == "tool_use"), None)
            if tool is None:
                failed.append((names.get(r.custom_id), "no tool call"))
                continue
            usage["input"] += msg.usage.input_tokens
            usage["output"] += msg.usage.output_tokens
            usage["cache_read"] += getattr(msg.usage, "cache_read_input_tokens", 0) or 0
            out.write(json.dumps({"oracleId": r.custom_id, "card": names.get(r.custom_id),
                                  "abilities": tool.input.get("abilities", [])}) + "\n")
            ok += 1
    print(f"collected {ok} cards into {out_path}; {len(failed)} failed")
    if failed:
        reasons = {}
        for _, kind, detail in failed:
            reasons[(kind, detail)] = reasons.get((kind, detail), 0) + 1
        for (kind, detail), n in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
            print(f"   {n} x {kind}: {detail}")
    print(f"tokens: {usage}")


def require_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY isn't set in this terminal. Run:  export ANTHROPIC_API_KEY=your-key\n"
                         "(it lasts only for this terminal window; add it to ~/.zshrc to keep it)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "submit", "status", "collect"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--model", choices=list(MODELS), default="haiku")
    ap.add_argument("--wait", action="store_true", help="collect: poll until the batch ends")
    ap.add_argument("--requests", default=str(REQUESTS), help="request file (enrich_deck.py writes one per deck)")
    ap.add_argument("--tag", default="", help="name for this batch, e.g. the deck: keeps files separate")
    a = ap.parse_args()
    if a.cmd == "build":
        build(*(a.args or ["mtg.rl", "enrich_subset.csv"]))
    elif a.cmd in ("submit", "status", "collect"):
        require_key()
    if a.cmd == "build":
        pass
    elif a.cmd == "submit":
        submit(a.model, a.requests, a.tag)
    elif a.cmd == "status":
        status(a.model, a.tag)
    else:
        collect(a.model, a.wait, a.requests, a.tag)
