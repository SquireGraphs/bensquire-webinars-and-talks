"""Publish the prebuilt data as GitHub Release files, so others can `make start` without building (maintainers only).

    python release_data.py                  # package into dist/ and write data_manifest.json
    python release_data.py --upload         # ...and upload to the GitHub Release (needs the gh CLI, logged in)
    python release_data.py --upload --tag data-v2   # a new data version

Stop the local server first: the graph file should not be changing while it's packaged.
Commit data_manifest.json afterwards, so `make data` downloads exactly these files.
"""
import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHUNK = 1 << 20
FILES = [  # (file the app uses, file to upload)
    ("mtg_live.rl", "mtg_live.rl.gz"),            # the Relate graph, gzipped for the download
    ("variants_slim.jsonl.gz", "variants_slim.jsonl.gz"),  # Commander Spellbook combos (already compressed)
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="data-v1")
    ap.add_argument("--repo", help="owner/name; default: this folder's GitHub repo")
    ap.add_argument("--upload", action="store_true")
    a = ap.parse_args()

    dist = HERE / "dist"
    dist.mkdir(exist_ok=True)
    entries = []
    for name, asset in FILES:
        src = HERE / name
        if not src.exists():
            raise SystemExit(f"{name} not found: build it first (make build).")
        out = dist / asset
        if name == asset:
            shutil.copyfile(src, out)
        else:
            print(f"compressing {name}…")
            with open(src, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
                shutil.copyfileobj(f, g, CHUNK)
        entries.append({"name": name, "asset": asset, "sha256": sha256(src),
                        "bytes": src.stat().st_size, "download_bytes": out.stat().st_size})
        print(f"  {asset}: {out.stat().st_size / 1e6:,.0f} MB")

    repo = a.repo
    if not repo:
        try:
            repo = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        except (OSError, subprocess.CalledProcessError):
            repo = "<owner>/<repo>"
            print("Couldn't find the GitHub repo (is gh installed and this folder pushed?): pass --repo owner/name.")
    manifest = {"base_url": f"https://github.com/{repo}/releases/download/{a.tag}/", "tag": a.tag, "files": entries}
    (HERE / "data_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote data_manifest.json -> {manifest['base_url']}")

    if a.upload:
        files = [str(dist / e["asset"]) for e in entries]
        try:
            gh("release", "view", a.tag, "--repo", repo)
            gh("release", "upload", a.tag, *files, "--clobber", "--repo", repo)
        except subprocess.CalledProcessError:
            gh("release", "create", a.tag, *files, "--repo", repo, "--title", f"Prebuilt data ({a.tag})",
               "--notes", "The Relate graph and Commander Spellbook combos for the local app. `make start` downloads these.")
        print(f"uploaded to https://github.com/{repo}/releases/tag/{a.tag}")
        print("Now commit data_manifest.json and push.")


if __name__ == "__main__":
    main()
