"""Embed the Comprehensive Rules into the graph, so the judge and the miner get the rules that matter.

    python embed_rules.py build mtg_live.rl MagicCompRules_<date>.pdf            # OpenAI text-embedding-3-small (default)
    python embed_rules.py build mtg_live.rl MagicCompRules_<date>.pdf --backend st      # local sentence-transformers
    python embed_rules.py build mtg_live.rl MagicCompRules_<date>.pdf --backend tfidf   # no download: a TF-IDF stand-in
    python embed_rules.py ask   mtg_live.rl "a token copy is exiled and returned"     # top rules for any text

Each numbered rule (702.100a, 400.7, 704.5d ...) becomes (:RuleText {number, section, text, embedding}), with a
vector index `rule_text_vec`, and keyword sections link to their parts: (:Rule)-[:HAS_PART]->(:RuleText).
Vectors are for FINDING rules to show the judge, never for deciding whether a combo works.
"""
import argparse
import json
import os
import pickle
import re
import subprocess

import relate

MODEL = "BAAI/bge-small-en-v1.5"       # small (384 dimensions), strong on retrieval, runs on a laptop CPU
OPENAI_MODEL = "text-embedding-3-small" # 1536 dimensions; the whole rule book costs well under a cent
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def pdf_text(path):
    """The PDF's text: pdftotext (Poppler) if installed, else pypdf, else a clear message."""
    import shutil
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True, check=True).stdout
    try:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except ImportError:
        raise SystemExit("Can't read the PDF: install pypdf (pip install pypdf) or Poppler (brew install poppler),\n"
                         "or pass the extracted text instead: comprules_20260807.txt (included in the project).")


def rule_chunks(pdf_or_txt):
    text = open(pdf_or_txt, encoding="utf-8").read() if pdf_or_txt.endswith(".txt") else \
        pdf_text(pdf_or_txt)
    body = text[text.find("100. General"):]
    gl = [m.start() for m in re.finditer(r"\nGlossary\n", body)]
    body = body[:gl[-1]] if gl else body
    starts = list(re.finditer(r"(?m)^(\d{3}\.\d+[a-z]?)\.? ", body))
    if len(starts) < 1000:                     # pypdf's layout: rule numbers aren't at line starts; find them in the flow,
        starts = list(re.finditer(r"(?<![\w.])(?<!rule )(?<!rules )(?<!and )(?<!see )(\d{3}\.\d+[a-z]?)\.? (?=[A-Z“\"])", body))
    titles, chunks = {}, []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        num = m.group(1)
        txt = re.sub(r"\s+", " ", body[m.start():end]).strip()
        if re.fullmatch(r"\d{3}\.\d+", num):
            titles[num] = txt[len(num) + 2:][:60]
        parent = re.match(r"\d{3}\.\d+", num).group(0)
        section = titles.get(parent, "")
        chunks.append({"number": num, "section": section, "text": txt[:1500],
                       "embed_text": (f"{parent}. {section}: " if section and num != parent else "") + txt[:1500]})
    return chunks


class Embedder:
    def __init__(self, backend, cache="rules_tfidf.pkl"):
        self.backend, self.cache = backend, cache
        if backend == "openai":
            if not os.environ.get("OPENAI_API_KEY"):
                raise SystemExit("OPENAI_API_KEY isn't set in this terminal. Run:  export OPENAI_API_KEY=your-key")
            from openai import OpenAI
            self.client = OpenAI()
        elif backend == "st":
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(MODEL)
        elif os.path.exists(cache):
            self.vec, self.svd = pickle.load(open(cache, "rb"))

    def fit(self, texts):
        if self.backend == "tfidf":
            from sklearn.decomposition import TruncatedSVD
            from sklearn.feature_extraction.text import TfidfVectorizer
            self.vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=2, sublinear_tf=True)
            X = self.vec.fit_transform(texts)
            self.svd = TruncatedSVD(n_components=256, random_state=0).fit(X)
            pickle.dump((self.vec, self.svd), open(self.cache, "wb"))

    def embed(self, texts, query=False):
        if self.backend == "openai":                   # vectors come back normalized; batches stay well under the limits
            out = []
            for i in range(0, len(texts), 200):
                r = self.client.embeddings.create(model=OPENAI_MODEL, input=[t[:6000] for t in texts[i:i + 200]])
                out += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
            return out
        if self.backend == "st":
            return self.model.encode([QUERY_PREFIX + t if query else t for t in texts], normalize_embeddings=True).tolist()
        import numpy as np
        X = self.svd.transform(self.vec.transform(texts))
        X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
        return X.tolist()


def build(store, source, backend):
    chunks = rule_chunks(source)
    emb = Embedder(backend)
    emb.fit([c["embed_text"] for c in chunks])
    vecs = emb.embed([c["embed_text"] for c in chunks])
    db = relate.connect(store)
    db.execute("MATCH (r:RuleText) DETACH DELETE r")
    rows = [{"number": c["number"], "section": c["section"], "text": c["text"], "v": v} for c, v in zip(chunks, vecs)]
    for i in range(0, len(rows), 500):
        db.execute("UNWIND $rows AS x CREATE (:RuleText {number: x.number, section: x.section, text: x.text, embedding: x.v})", {"rows": rows[i:i + 500]})
    try:                                            # a rebuild may change dimensions (switching backends): recreate the index
        db.execute("DROP INDEX rule_text_vec IF EXISTS")
    except Exception:
        pass
    db.execute(f"CREATE VECTOR INDEX rule_text_vec IF NOT EXISTS FOR (r:RuleText) ON (r.embedding) "
               f"OPTIONS {{indexConfig: {{`vector.dimensions`: {len(vecs[0])}, `vector.similarity_function`: 'cosine'}}}}")
    linked = list(db.execute("""MATCH (k:Rule), (t:RuleText) WHERE t.number STARTS WITH k.number
                                MERGE (k)-[:HAS_PART]->(t) RETURN count(*) AS n"""))[0]["n"]
    json.dump({"backend": backend, "dimensions": len(vecs[0]), "rules": len(rows)}, open("rules_embedding.json", "w"))
    print(f"{len(rows)} rules embedded ({backend}, {len(vecs[0])} dimensions); {linked} linked to keyword sections; index rule_text_vec")
    db.close()


_emb = None


def retrieve(db, text, k=4):
    """Top-k rules for a piece of text, from the vector index. Returns [] if rules haven't been embedded."""
    global _emb
    if not os.path.exists("rules_embedding.json"):
        return []
    if _emb is None:
        _emb = Embedder(json.load(open("rules_embedding.json"))["backend"])
    v = _emb.embed([text], query=True)[0]
    return [dict(r) for r in db.execute("""CALL db.index.vector.queryNodes('rule_text_vec', $k, $v) YIELD node, score
                                           RETURN node.number AS number, node.section AS section, node.text AS text, score""", {"k": k, "v": v})]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "ask"])
    ap.add_argument("store")
    ap.add_argument("arg")
    ap.add_argument("--backend", choices=["openai", "st", "tfidf"], default="openai")
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.store, a.arg, a.backend)
    else:
        db = relate.connect(a.store)
        for r in retrieve(db, a.arg, 6):
            print(f"{r['score']:.3f}  {r['number']:9} {r['text'][:150]}")
