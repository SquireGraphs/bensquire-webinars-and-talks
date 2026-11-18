"""Rulings as tests: put every loop the graph finds in front of the official rulings.

    python rulings_tests.py build  mtg_live.rl rulings-<date>.jsonl     # collect loops -> rulings_requests.jsonl
    python rulings_tests.py run    [--model claude-sonnet-5-5]           # Claude judges each loop (needs ANTHROPIC_API_KEY)
    python rulings_tests.py report mtg_live.rl                           # agreement rate, disagreements, tests

For each loop, Claude sees the cards' Oracle text, the steps the graph claims, every official ruling on
those cards, and the Comprehensive Rules section for each keyword they use, and answers works / fails /
unsure with a citation. "works" becomes a must-loop test and "fails" a must-not-loop test (rulings_tests.json,
read by checks.py). A loop the graph finds but the rulings say fails is a precision bug with a citation.
"""
import argparse
import json
import os
import random
import re
import sys
from collections import defaultdict

SYSTEM = """You are a Magic: The Gathering rules judge. A program claims the cards below form a repeatable loop
(a combo that can repeat indefinitely). Decide whether the loop really works under the official rules, using the
card text, the official rulings and the Comprehensive Rules excerpts given. Judge only these cards: assume the
player controls them and has ordinary lands, nothing else, unless a step says otherwise. Be strict about timing,
targets ("another", "non-Angel"), conditions ("if it was cast", "greater power"), "this creature" vs any creature,
once-per-turn limits, and whether each step can really repeat. Answer only by calling judge_loop."""

CORE_RULES = [   # (what the loop does, rules that always matter for it)
    (r"exile|flicker|return (?:it|that card|those cards) to the battlefield|leaves the battlefield", ["400.7", "610.3"]),
    (r"\btoken", ["111.7", "704.5d"]),
    (r"sacrifice", ["701.21a"]),
    (r"\+1/\+1 counter|-1/-1 counter", ["122.3", "704.5q"]),
    (r"\bdies\b|\bdie\b", ["700.4", "704.5f"]),
    (r"\bcast\b", ["601.2"]),
]
TOOL = {"name": "judge_loop", "description": "Your verdict on whether the claimed loop works.",
        "input_schema": {"type": "object", "properties": {
            "verdict": {"type": "string", "enum": ["works", "fails", "unsure"]},
            "reason": {"type": "string", "description": "One or two sentences: why it works, or the exact step that fails."},
            "citation": {"type": "string", "description": "The ruling or rule number (e.g. 702.100a) the verdict rests on, quoted briefly."}},
            "required": ["verdict", "reason"]}}


def collect_loops(db, max_random=280):
    from decks import load_deck, load_deck_text
    from engine import find_loops
    from demo_data import SAMPLE_DECKS
    loops = {}
    def add(ls, source):
        for l in ls:
            if len(l["cards"]) >= 2 and l.get("kind", "loop") == "loop":
                loops.setdefault(tuple(sorted(l["cards"])), {"cards": sorted(l["cards"]), "path": l["path"], "source": source})
    for p in list(SAMPLE_DECKS) + ["decks/gandalf.txt"]:
        if not os.path.exists(p):
            continue
        t = open(p, encoding="utf-8").read()
        d = load_deck(db, p) if re.search(r"^commander\s*$", t, re.I | re.M) else load_deck_text(db, t)
        add(find_loops(db, sorted(set(d["commanders"] + d["cards"]))), p)
    pool = [x["n"] for x in db.execute("MATCH (c:Card) WHERE 'commander' IN c.legalIn AND NOT 'Land' IN c.types RETURN c.name AS n")]
    for seed in (3, 11, 12, 13):
        random.seed(seed)
        for _ in range(100 if seed == 3 else 60):
            add(find_loops(db, random.sample(pool, 62), budget_s=10)[:1], "random deck")
    return list(loops.values())


def build(store, rulings_path, rules_path="comprules_keywords.json", out="rulings_requests.jsonl"):
    import relate
    db = relate.connect(store)
    loops = collect_loops(db)
    names = sorted({c for l in loops for c in l["cards"]})
    info = {r["n"]: r for r in db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.oracleId AS id, c.oracleText AS t, c.typeLine AS tl", {"n": names})}
    want = {r["id"] for r in info.values()}
    rulings = defaultdict(list)
    for line in open(rulings_path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("oracle_id") in want:
            rulings[r["oracle_id"]].append(r.get("comment", ""))
    rules = json.load(open(rules_path, encoding="utf-8"))
    with open(out, "w", encoding="utf-8") as f:
        for l in loops:
            texts = " ".join((info.get(c, {}).get("t") or "") for c in l["cards"]).lower()
            kws = [k for k in rules if len(k) > 3 and re.search(rf"\b{re.escape(k)}\b", texts)][:4]
            card_part = "\n".join(f"- {c} ({info.get(c, {}).get('tl', '')}): {info.get(c, {}).get('t', '')}" for c in l["cards"])
            steps = "\n".join(f"{i + 1}. {p['card']} ({p['kind']}): {p['text']}" for i, p in enumerate(l["path"]))
            rul = "\n".join(f"- {c}: " + " | ".join(r[:300] for r in rulings.get(info.get(c, {}).get("id"), [])[:12])
                            for c in l["cards"] if rulings.get(info.get(c, {}).get("id")))
            cr = "\n\n".join(rules[k]["text"][:1500] for k in kws)
            # Core interaction rules for what the loop does, plus the closest rules by meaning (vector index).
            want_nums = [n for rx, ns in CORE_RULES for n in ns if re.search(rx, texts + " " + steps.lower())]
            extra = [dict(r) for r in db.execute("MATCH (t:RuleText) WHERE t.number IN $n RETURN t.number AS number, t.text AS text", {"n": want_nums})]
            try:
                from embed_rules import retrieve
                extra += retrieve(db, card_part + "\n" + steps, 4)
            except Exception:
                pass
            seen, more = set(), []
            for r in extra:
                if r["number"] not in seen and not any(r["number"].startswith(rules[k]["rule"]) for k in kws):
                    seen.add(r["number"]); more.append(f"{r['text'][:600]}")
            if more:
                cr = (cr + "\n\n" if cr else "") + "Other relevant rules:\n" + "\n".join(more[:7])
            prompt = (f"Cards:\n{card_part}\n\nThe loop the program claims:\n{steps}\n\n"
                      f"Official rulings:\n{rul or '(none)'}\n\nComprehensive Rules:\n{cr or '(no keywords involved)'}")
            f.write(json.dumps({"cards": l["cards"], "source": l["source"], "prompt": prompt}) + "\n")
    print(f"{len(loops)} loops to judge -> {out} ({sum(1 for l in loops if l['source'] == 'random deck')} from random decks)")


def run(model, path="rulings_requests.jsonl", out="rulings_verdicts.jsonl", fresh=False):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY isn't set in this terminal. Run:  export ANTHROPIC_API_KEY=your-key")
    import anthropic
    client = anthropic.Anthropic()
    # Keep real verdicts; re-judge loops that errored or came back without one.
    done = {}
    if os.path.exists(out) and not fresh:
        for l in open(out, encoding="utf-8"):
            v = json.loads(l)
            if v.get("verdict") in ("works", "fails"):
                done[tuple(v["cards"])] = v
    reqs = [json.loads(l) for l in open(path, encoding="utf-8")]
    todo = [r for r in reqs if tuple(r["cards"]) not in done]
    print(f"{len(done)} loops already judged; {len(todo)} to go (model {model})")
    errors_in_a_row = 0
    with open(out, "a", encoding="utf-8") as f:
        for i, r in enumerate(todo):
            try:
                msg = client.messages.create(model=model, max_tokens=4000, system=SYSTEM, tools=[TOOL], tool_choice={"type": "auto"},
                                             messages=[{"role": "user", "content": r["prompt"] + "\n\nAnswer only by calling judge_loop."}])
                use = next((b for b in msg.content if b.type == "tool_use"), None)
                if use:
                    v = dict(use.input)
                    if v.get("verdict") not in ("works", "fails", "unsure"):      # a reply without a usable verdict
                        found = re.search(r"\b(works|fails|unsure)\b", json.dumps(v).lower())
                        v["verdict"] = found.group(1) if found else "unsure"
                else:                                      # answered in plain text: recover the verdict if it's clear
                    text = " ".join(getattr(b, "text", "") for b in msg.content)
                    m = re.search(r"\b(works|fails|unsure)\b", text.lower())
                    if m:
                        v = {"verdict": m.group(1), "reason": f"(from text) {text[:300]}"}
                    else:                                  # cut off before answering: not a judgment, re-ask next run
                        v = {"verdict": "no_verdict", "reason": f"stopped: {getattr(msg, 'stop_reason', '?')}"}
                errors_in_a_row = 0
            except Exception as err:
                errors_in_a_row += 1
                print(f"  error on {' + '.join(r['cards'])}: {err.__class__.__name__}: {str(err)[:300]}")
                if errors_in_a_row >= 3:
                    sys.exit("Stopped after 3 errors in a row (see above). Fix the cause, then rerun: finished loops are kept.")
                continue                                   # not recorded, so a rerun retries it
            f.write(json.dumps({"cards": r["cards"], "source": r["source"], **v}) + "\n")
            f.flush()
            print(f"[{i + 1}/{len(todo)}] {v.get('verdict', '?'):6} {' + '.join(r['cards'])}")


def report(store, path="rulings_verdicts.jsonl", out="rulings_tests.json"):
    latest = {}
    for l in open(path, encoding="utf-8"):                 # the latest verdict per loop wins
        v = json.loads(l)
        latest[tuple(v["cards"])] = v
    vs = list(latest.values())
    by = defaultdict(list)
    for v in vs:
        k = v.get("verdict")
        by[k if k in ("works", "fails", "unsure") else "no_verdict"].append(v)
    judged = len(by["works"]) + len(by["fails"])
    print(f"{len(vs)} loops: {len(by['works'])} work, {len(by['fails'])} fail, {len(by['unsure'])} unsure"
          + (f", {len(by['no_verdict'])} got no verdict (rerun 'run' to re-ask them)" if by["no_verdict"] else ""))
    if judged:
        print(f"the graph agrees with the rulings on {len(by['works'])} of {judged} loops it finds ({100 * len(by['works']) / judged:.0f}%)")
    for source in ("sample", "random deck"):
        sel = [v for v in vs if (v["source"] == "random deck") == (source == "random deck") and v.get("verdict") in ("works", "fails")]
        if sel:
            print(f"  {source}: {sum(v['verdict'] == 'works' for v in sel)} of {len(sel)} work")
    if by["unsure"]:
        from collections import Counter
        why = Counter((v.get("reason") or "")[:60] for v in by["unsure"])
        print("why loops are unsure (most common):")
        for k, n in why.most_common(5):
            print(f"  {n:4}  {k}")
    print("\nfails (each is a fix to make, with its citation):")
    for v in by["fails"]:
        print(f"  {' + '.join(v['cards'])}\n      {v.get('reason', '')}\n      [{v.get('citation', '')}]")
    json.dump({"must_loop": [{"cards": v["cards"], "why": v.get("reason"), "citation": v.get("citation")} for v in by["works"]],
               "must_not_loop": [{"cards": v["cards"], "why": v.get("reason"), "citation": v.get("citation")} for v in by["fails"]]},
              open(out, "w"), indent=1, ensure_ascii=False)
    print(f"\ntests written to {out}: checks.py runs them as rulings checks")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "run", "report"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--out", default="rulings_verdicts.jsonl", help="verdicts file (use a new one to compare runs)")
    ap.add_argument("--fresh", action="store_true", help="re-judge every loop, ignoring earlier verdicts in --out")
    a = ap.parse_args()
    if a.cmd == "build":
        build(*a.args[:2])
    elif a.cmd == "run":
        run(a.model, out=a.out, fresh=a.fresh)
    else:
        report(*a.args[:1], path=a.out)
