"""MTGraphRAG local server: the demo app plus live analysis of any pasted decklist.

    python server.py                          # http://localhost:8000
    python server.py --store mtg_sonnet_union.rl --port 8000

Opens the Relate graph once, in memory, and keeps it open. Relate allows one
process per store, so this is a simple single-threaded server: one analysis at
a time, a few seconds each. Binds to localhost only.
"""
import argparse
import gzip
import os
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import relate

from decks import load_deck_text
from demo_data import deck_report
from graph_data import deck_graph, load_events
import engine

MAX_INPUT = 30_000          # characters: a 100-card list is ~2,000
REVIEWS = "reviews.jsonl"   # your "real combo / not a combo" labels: checks.py turns them into tests


class App:
    def __init__(self, store, variants, template, data):
        t0 = time.time()
        self.db = relate.connect(store)
        load_events(self.db)
        self.variants = [json.loads(l) for l in gzip.open(variants, "rt")]
        self.template_path = Path(template)
        self.template = self.template_path.read_text(encoding="utf-8")
        self.data = json.load(open(data, encoding="utf-8"))
        print(f"graph {store} open, {len(self.variants):,} Spellbook combos loaded ({time.time() - t0:.1f}s)")

    def page(self):
        # Re-read the template on every page load: an updated app_template.html shows up on refresh,
        # without restarting the server.
        try:
            self.template = self.template_path.read_text(encoding="utf-8")
        except OSError:
            pass
        # The How it works figures come straight from validation.json, so they always match this template.
        if os.path.exists("validation.json"):
            try:
                self.data["validation"] = json.load(open("validation.json", encoding="utf-8"))
            except ValueError:
                pass
        payload = json.dumps(self.data, ensure_ascii=False).replace("</", "<\\/")
        return self.template.replace("__DATA__", payload)

    # ---- taught mechanics (custom_rules.json) ----
    def rules(self):
        try:
            return json.load(open(engine.RULES_FILE, encoding="utf-8")).get("rules", [])
        except (OSError, ValueError):
            return []

    def save_rules(self, rules):
        json.dump({"rules": rules}, open(engine.RULES_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    def clean_rule(self, r):
        rule = {"name": str(r.get("name", "")).strip()[:80], "match": str(r.get("match", "")).strip()[:300],
                "description": str(r.get("description", ""))[:300], "needs": str(r.get("needs", "")).strip()[:40],
                "kind": r.get("kind") if r.get("kind") in ("Activated", "Triggered", "Static") else "Activated",
                "trigger": [e for e in r.get("trigger", []) if isinstance(e, str)][:3],
                "costs": [x for x in r.get("costs", []) if isinstance(x, dict) and x.get("resource")][:3],
                "produces": [x for x in r.get("produces", []) if isinstance(x, dict) and x.get("resource")][:3],
                "causes": [x for x in r.get("causes", []) if isinstance(x, dict) and x.get("event")][:4],
                "example": [str(c) for c in r.get("example", [])][:6]}
        if not rule["name"] or not rule["match"]:
            raise ValueError("A rule needs a name and the card wording to match.")
        return rule

    def lookup_cards(self, names):
        """Typed card names -> what the graph has for them (for previews), with close matches for unknown names."""
        out = []
        for raw in [n.strip() for n in names if n and n.strip()][:12]:
            rows = list(self.db.execute("""MATCH (c:Card) WHERE toLower(c.name) = toLower($n)
                RETURN c.name AS name, c.typeLine AS type, c.manaCost AS cost, c.oracleText AS text, c.imageUrl AS image""", {"n": raw}))
            if rows:
                out.append({"query": raw, "found": True, **rows[0]})
            else:
                close = [r["n"] for r in self.db.execute("""MATCH (c:Card) WHERE toLower(c.name) CONTAINS toLower($n)
                    RETURN c.name AS n ORDER BY size(c.name) LIMIT 3""", {"n": (raw[:-2] if len(raw) > 6 else raw)[:24]})]   # trimmed: catches a typo at the end
                out.append({"query": raw, "found": False, "suggestions": close})
        return out

    def check_edges(self, edges):
        """Is each drawn relationship already in the graph? A Cypher query per edge against the Relate store."""
        out = []
        for e in edges[:100]:
            typ = str(e.get("type", ""))
            if typ == "FIRES":                       # "counts as": no ability, a resource or event into an event
                src, name = str(e.get("s", "")), str(e.get("t", ""))
                src = src.split(":", 1)[1] if src[:2] in ("n:", "r:", "e:") else src
                q = "MATCH (r)-[f:FIRES]->(t:Event {name: $n}) WHERE r.name = $s RETURN count(f) AS c"
                rel = list(self.db.execute(q, {"s": src, "n": name}))[0]["c"]
                tgt = list(self.db.execute("MATCH (t:Event {name: $n}) RETURN count(t) AS c", {"n": name}))[0]["c"]
                out.append({"exists": rel > 0, "target": tgt > 0,
                            "cypher": f"MATCH (r {{name: '{src}'}})-[f:FIRES]->(t:Event {{name: '{name}'}}) RETURN count(f)"})
                continue
            if typ not in ("TRIGGERED_BY", "RETRIGGERS", "PRODUCES", "COSTS", "REQUIRES_STATE", "CAUSES_STATE", "CLEARS_STATE"):
                out.append({"exists": False, "target": False}); continue
            t = str(e.get("t", "")); name = t[2:] if t.startswith("s:") else t
            label = "State" if t.startswith("s:") else ("Resource" if typ in ("PRODUCES", "COSTS") else "Event")
            # the rules and LLM readings of one ability are shown as one node: either reading counts
            ids = [e.get("s")] + [i for i in (e.get("ids") or []) if isinstance(i, str)][:6]
            rel = sum(list(self.db.execute(f"MATCH (a:Ability {{id: $a}})-[r:{typ}]->(t:{label} {{name: $n}}) RETURN count(r) AS c",
                                           {"a": i, "n": name}))[0]["c"] for i in dict.fromkeys(ids))
            tgt = list(self.db.execute(f"MATCH (t:{label} {{name: $n}}) RETURN count(t) AS c", {"n": name}))[0]["c"]
            out.append({"exists": rel > 0, "target": tgt > 0,
                        "cypher": f"MATCH (a:Ability {{id: '{e.get('s')}'}})-[r:{typ}]->(t:{label} {{name: '{name}'}}) RETURN count(r)"})
        return out

    def test_model(self, cards, edges):
        """Run the engine on the chosen cards with the drawn relationships added: the loops it finds (and what it
        found without them), or, if none, why the cycles among those cards fail."""
        names = [c for c in cards if c][:6]
        summarise = lambda ls: [{"cards": l["cards"], "net": {k: v for k, v in l["net"].items() if v}, "kind": l.get("kind", "loop"),
                                 "path": [{"card": p["card"], "text": p["text"]} for p in l["path"]]} for l in ls]
        before = summarise(engine.find_loops(self.db, names, budget_s=15)) if names else []
        engine.MODEL_DRAFT, engine.MODEL_DRAFT_CARDS = edges[:100], names
        try:
            after = summarise(engine.find_loops(self.db, names, budget_s=15)) if names else []
            why = [] if after else engine.explain_no_loop(self.db, names)
        finally:
            engine.MODEL_DRAFT, engine.MODEL_DRAFT_CARDS = None, []
        return {"before": before, "loops": after, "why": why}

    def test_rule(self, r, cards):
        rule = self.clean_rule(r)
        if not hasattr(self, "_texts"):
            from tag_functions import normalise
            self._texts = [(x["n"], engine.SELF_WORDS.sub("~", normalise({"name": x["n"], "text": x["t"] or ""})))
                           for x in self.db.execute("MATCH (c:Card) WHERE 'commander' IN c.legalIn RETURN c.name AS n, c.oracleText AS t")]
        rx = engine.rule_regex(rule)
        matches = [n for n, t in self._texts if rx.search(t)]
        names = [c for c in cards if c][:8]
        saved = [x for x in self.rules() if x["name"] != rule["name"]]
        try:
            engine.RULES_OVERRIDE = saved
            before = [l["cards"] for l in engine.find_loops(self.db, names)] if names else []
            engine.RULES_OVERRIDE = saved + [rule]
            after = [{"cards": l["cards"], "mana": l["net"].get("mana")} for l in engine.find_loops(self.db, names)] if names else []
        finally:
            engine.RULES_OVERRIDE = None
        return {"matches": len(matches), "examples": matches[:12], "before": before, "after": after}

    def analyze(self, text, commander):
        d = load_deck_text(self.db, text, commander or None)
        if not d["commanders"]:
            return 400, {"error": "No commander found.", "problems": d["problems"]}
        known = [c for c in set(d["commanders"] + d["cards"]) if c in d["info"]]
        if len(known) < 10:
            return 400, {"error": "Too few cards were recognised to analyse this deck.", "problems": d["problems"]}
        t0 = time.time()
        report = deck_report(self.db, d, self.variants)
        report["graph"] = deck_graph(self.db, d)
        report["seconds"] = round(time.time() - t0, 1)
        return 200, {"deck": report}


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, kind="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, app.page(), "text/html")
            elif self.path == "/api/reviews":
                rows = [json.loads(l) for l in open(REVIEWS, encoding="utf-8")] if os.path.exists(REVIEWS) else []
                self._send(200, {"reviews": rows})
            elif self.path == "/api/model":
                return self._send(200, {"models": engine.saved_models()})
            elif self.path == "/api/rules":
                self._send(200, {"rules": app.rules()})
            elif self.path == "/api/health":
                self._send(200, {"ok": True, "live": True})
            else:
                self._send(404, {"error": "Not found"})

        def do_POST(self):
            if self.path == "/api/model/graph":
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 50_000)) or b"{}")
                from graph_data import deck_graph
                names = [n for n in body.get("names", []) if n][:8]
                G = deck_graph(app.db, {"commanders": [], "cards": names}) if names else {"nodes": [], "edges": []}
                # Teaching needs every ability, including those with no links yet (the deck view leaves them out):
                # an ability the graph can't connect is exactly what you'll be drawing for.
                have = {n["id"] for n in G["nodes"]}
                for r in app.db.execute("""MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability) WHERE c.name IN $n
                                           RETURN c.name AS card, a.id AS id, a.text AS text, labels(a) AS labels""", {"n": names}):
                    if r["id"] not in have:
                        G["nodes"].append({"id": r["id"], "type": "Ability", "card": r["card"], "text": r["text"], "labels": r["labels"],
                                           "name": (r["text"] or "")[:40]})
                        have.add(r["id"])
                for c in names:
                    if "card:" + c not in have and not any(n["type"] == "Card" and n.get("name") == c for n in G["nodes"]):
                        G["nodes"].append({"id": "card:" + c, "type": "Card", "name": c})
                return self._send(200, G)
            if self.path in ("/api/cards/lookup", "/api/model/check"):
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 100_000)) or b"{}")
                if self.path == "/api/cards/lookup":
                    return self._send(200, {"cards": app.lookup_cards(body.get("names", []))})
                return self._send(200, {"edges": app.check_edges(body.get("edges", []))})
            if self.path in ("/api/model/test", "/api/model/save", "/api/model/delete"):
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 200_000)) or b"{}")
                    if self.path == "/api/model/test":
                        return self._send(200, app.test_model(body.get("cards", []), body.get("edges", [])))
                    models = [m for m in engine.saved_models() if m.get("name") != body.get("name")]
                    if self.path == "/api/model/save":
                        name = str(body.get("name") or "").strip()[:80]
                        if not name or not body.get("edges"):
                            raise ValueError("Give the model a name and at least one relationship.")
                        loop = body.get("loop") if isinstance(body.get("loop"), dict) else None
                        models.append({"name": name, "cards": body.get("cards", [])[:8], "edges": body.get("edges", [])[:100],
                                       **({"loop": {"cards": loop.get("cards", [])[:8], "net": loop.get("net", {})}} if loop else {})})
                    with open(engine.MODEL_FILE, "w", encoding="utf-8") as f:
                        json.dump(models, f, indent=1, ensure_ascii=False)
                    return self._send(200, {"models": models})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if self.path in ("/api/rules", "/api/rules/delete", "/api/rules/test"):
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 50_000)) or b"{}")
                    if self.path == "/api/rules/test":
                        return self._send(200, app.test_rule(body.get("rule", {}), body.get("cards", [])))
                    rules = app.rules()
                    if self.path == "/api/rules":
                        rule = app.clean_rule(body.get("rule", {}))
                        rules = [x for x in rules if x["name"] != rule["name"]] + [rule]
                    else:
                        rules = [x for x in rules if x["name"] != body.get("name")]
                    app.save_rules(rules)
                    return self._send(200, {"rules": rules})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/review":
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20_000)) or b"{}")
                    row = {"kind": body.get("kind"), "cards": [str(c) for c in body.get("cards", [])][:8],
                           "label": body.get("label"), "deck": str(body.get("deck", ""))[:120],
                           "note": str(body.get("note", ""))[:500], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    if row["kind"] not in ("loop", "nonbo") or row["label"] not in ("real", "not_real", "correct", "wrong") or not row["cards"]:
                        return self._send(400, {"error": "Not a valid review."})
                    with open(REVIEWS, "a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    return self._send(200, {"ok": True})
                except (ValueError, OSError) as e:
                    return self._send(400, {"error": f"Couldn't save the review: {e}"})
            if self.path != "/api/analyze":
                return self._send(404, {"error": "Not found"})
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_INPUT * 4:
                return self._send(413, {"error": "That decklist is too long."})
            try:
                body = json.loads(self.rfile.read(size) or b"{}")
                text = str(body.get("decklist", ""))[:MAX_INPUT]
                code, out = app.analyze(text, str(body.get("commander", "")).strip())
            except (ValueError, KeyError) as e:
                code, out = 400, {"error": f"Couldn't read the request: {e}"}
            except Exception as e:                       # keep the server up; report the failure
                code, out = 500, {"error": f"Analysis failed: {e}"}
            self._send(code, out)

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="mtg_live.rl")
    ap.add_argument("--variants", default="variants_slim.jsonl.gz")
    ap.add_argument("--template", default="app_template.html")
    ap.add_argument("--data", default="demo_data.json")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open the app in your browser once it's ready")
    a = ap.parse_args()
    for f, what in ((a.store, "the graph"), (a.variants, "the Commander Spellbook combos")):
        if not Path(f).exists():
            raise SystemExit(f"{f} not found ({what}). Run `make data` to download it, or `make build` to build it.")
    app = App(a.store, a.variants, a.template, a.data)
    server = HTTPServer(("127.0.0.1", a.port), handler_for(app))
    print(f"MTGraphRAG running at http://localhost:{a.port}  (Ctrl+C to stop)")
    if a.open:
        import threading, webbrowser
        threading.Timer(0.5, webbrowser.open, (f"http://localhost:{a.port}",)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
