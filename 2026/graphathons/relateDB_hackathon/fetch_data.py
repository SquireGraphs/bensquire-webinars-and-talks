"""Get the saved graph and combo data ready for the app (run by `make data` and `make start`).

    python fetch_data.py            # unpack data/ (or download it from the release), check every SHA-256
    python fetch_data.py --force    # unpack again even if the files are there

The saved graph ships in the repo's data/ folder (written by pack_data.py): mtg_live.rl gzipped and split under
GitHub's size limits, plus the Commander Spellbook combos. Files already in the project folder are kept as they
are, so a graph you built yourself is never overwritten. Standard library only, so it runs before any install.
"""
import argparse
import gzip
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHUNK = 1 << 20


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download(url, dest):
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as out:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        for block in iter(lambda: r.read(CHUNK), b""):
            out.write(block)
            done += len(block)
            if total:
                sys.stdout.write(f"\r  {dest.name}: {done / 1e6:,.0f} of {total / 1e6:,.0f} MB")
                sys.stdout.flush()
    sys.stdout.write("\n")
    tmp.replace(dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="unpack again even if the files exist")
    a = ap.parse_args()

    mpath = HERE / "data_manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {"files": []}
    needed = [f["name"] for f in manifest["files"]] or ["mtg_live.rl", "variants_slim.jsonl.gz"]
    if not a.force and all((HERE / n).exists() for n in needed):
        print("Data ready: " + ", ".join(needed) + " already here (kept as they are).")
        return
    if not manifest["files"]:
        raise SystemExit("No saved graph in this copy (data_manifest.json is empty): run `make pack` after building it, "
                         "or build it with `make build`.")

    for f in manifest["files"]:
        target = HERE / f["name"]
        if target.exists() and not a.force:
            print(f"  {f['name']}: already here, keeping it")
            continue
        parts = [HERE / p["path"] for p in f["parts"]]
        for p, info in zip(parts, f["parts"]):
            if not p.exists():
                if not manifest.get("release_url"):
                    raise SystemExit(f"{info['path']} is missing. Pull the repo again (git pull), or rebuild with `make build`.")
                p.parent.mkdir(exist_ok=True)
                print(f"Downloading {p.name}…")
                download(manifest["release_url"] + p.name, p)
            if sha256(p) != info["sha256"]:
                raise SystemExit(f"{info['path']} is damaged (checksum mismatch). Pull the repo again, or delete it and rerun.")
        print(f"  unpacking {f['name']} from {len(parts)} file(s)…")
        tmp = target.with_name(target.name + ".tmp")
        joined = target.with_name(target.name + ".join")
        with open(joined, "wb") as out:                      # put the parts back together
            for p in parts:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, out, CHUNK)
        if f.get("gzip"):
            with gzip.open(joined, "rb") as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            joined.unlink()
        else:
            joined.replace(tmp)
        if sha256(tmp) != f["sha256"]:
            tmp.unlink()
            raise SystemExit(f"{f['name']} didn't match its checksum after unpacking. Pull the repo again.")
        tmp.replace(target)
        print(f"  {f['name']}: ok ({target.stat().st_size / 1e6:,.0f} MB, checksum verified)")
    print("Data ready.")


if __name__ == "__main__":
    main()
