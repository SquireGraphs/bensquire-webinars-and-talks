"""Why does the state walk reject a loop? Shows each candidate cycle's steps with their State rows from the graph."""
import sys
import relate
import engine



def show(db, cards, limit=3):
    engine.STATE_WALK = False
    g = engine.deck_subgraph(db, cards); A = g["abilities"]
    loops = [l for l in engine.find_loops(db, cards) if set(l["cards"]) <= set(cards)]
    print(f"### {' + '.join(cards)}: {len(loops)} loop(s) without states")
    for l in loops[:limit]:
        ids = [next(x["id"] for x in A.values() if x["card"] == p["card"] and (x["text"] or "")[:40] == (p["text"] or "")[:40]) for p in l["path"]]
        print(f"  cycle -> walk accepts: {engine._state_walk(g, ids)}")
        for a in ids:
            trig = sorted(ev for ev, _ in g["TRIGGERED_BY"].get(a, []))
            print(f"    {A[a]['card'][:20]:20} {A[a]['kind'][:4]} trig={trig} | {(A[a]['text'] or '')[:70]}")
            for r in g["STATES"].get(a, []):
                print(f"        {r[0]:14} {r[1]:11} who={r[2]} by={r[3]}")


if __name__ == "__main__":
    db = relate.connect(sys.argv[1])
    for combo in sys.argv[2:]:
        show(db, combo.split("+"))
