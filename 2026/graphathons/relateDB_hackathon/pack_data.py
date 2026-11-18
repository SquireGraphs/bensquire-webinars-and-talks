"""Put a copy of the saved graph into the repo, so it ships with the code (maintainers; `make pack`).

    python pack_data.py              # data/: mtg_live.rl gzipped (in parts under GitHub's size limits), the combos,
                                     # and data_manifest.json with every checksum
    python pack_data.py --release    # ...and also attach the same files to a GitHub Release (needs the gh CLI)

Stop the local server first, so the graph file isn't changing while it's copied. Then commit data/ and
data_manifest.json. Each pack adds a full copy to the git history, so pack the graph you mean to submit,
not every rebuild. `make start` (fetch_data.py) unpacks these files; it never overwrites a graph you built.
"""
import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CHUNK = 1 << 20
PART = 45 * (1 << 20)        # under GitHub's 50 MiB warning (files over 100 MiB are refused)
FILES = [("mtg_live.rl", True),                 # the Relate graph: gzipped
         ("variants_slim.jsonl.gz", False)]     # the Commander Spellbook combos: already compressed


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def split(path, size=PART):
    """path -> path.001, path.002, ... (only when it's bigger than one part)."""
    if path.stat().st_size <= size:
        return [path]
    parts = []
    with open(path, "rb") as f:
        for i, block in enumerate(iter(lambda: f.read(size), b""), 1):
            p = path.with_name(f"{path.name}.{i:03d}")
            p.write_bytes(block)
            parts.append(p)
    path.unlink()
    return parts


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", action="store_true", help="also attach the files to a GitHub Release")
    ap.add_argument("--tag", default="data-v1")
    ap.add_argument("--repo", help="owner/name; default: this folder's GitHub repo")
    a = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    for old in DATA.glob("*"):               # replace the previous pack
        if old.name.startswith(tuple(n for n, _ in FILES)):
            old.unlink()
    entries = []
    for name, compress in FILES:
        src = HERE / name
        if not src.exists():
            raise SystemExit(f"{name} not found: build it first (make build).")
        out = DATA / (name + ".gz" if compress else name)
        if compress:
            print(f"compressing {name} ({src.stat().st_size / 1e6:,.0f} MB)…")
            with open(src, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
                shutil.copyfileobj(f, g, CHUNK)
        else:
            shutil.copyfile(src, out)
        parts = split(out)
        entries.append({"name": name, "gzip": compress, "sha256": sha256(src), "bytes": src.stat().st_size,
                        "parts": [{"path": p.relative_to(HERE).as_posix(), "sha256": sha256(p), "bytes": p.stat().st_size}
                                  for p in parts]})
        print(f"  {name}: {len(parts)} file(s), {sum(p.stat().st_size for p in parts) / 1e6:,.0f} MB in data/")

    manifest = {"files": entries}
    if a.release:
        repo = a.repo or gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        files = [str(HERE / p["path"]) for e in entries for p in e["parts"]]
        try:
            gh("release", "view", a.tag, "--repo", repo)
            gh("release", "upload", a.tag, *files, "--clobber", "--repo", repo)
        except subprocess.CalledProcessError:
            gh("release", "create", a.tag, *files, "--repo", repo, "--title", f"Saved graph ({a.tag})",
               "--notes", "The Relate graph (mtg_live.rl, gzipped and split) and the Commander Spellbook combos. "
                          "The same files are in the repo's data/ folder; `make start` unpacks them.")
        manifest["release_url"] = f"https://github.com/{repo}/releases/download/{a.tag}/"
        print(f"attached to https://github.com/{repo}/releases/tag/{a.tag}")
    (HERE / "data_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("wrote data_manifest.json. Now: git add data data_manifest.json && git commit -m \"Add the saved graph\"")


if __name__ == "__main__":
    main()
