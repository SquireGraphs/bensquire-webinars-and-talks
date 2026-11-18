"""Download the prebuilt graph and combo data for the local app (run by `make data` and `make start`).

    python fetch_data.py            # download what's missing, check each file's SHA-256
    python fetch_data.py --force    # download again even if the files are there

Files already present are kept as they are, so a graph you built yourself is never overwritten.
What to download, and from where, is in data_manifest.json (written by release_data.py).
Standard library only, so it runs before any package is installed.
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


def download(url, dest, size=None):
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or size or 0)
        done = 0
        while True:
            block = r.read(CHUNK)
            if not block:
                break
            out.write(block)
            done += len(block)
            if total:
                sys.stdout.write(f"\r  {dest.name}: {done / 1e6:,.0f} of {total / 1e6:,.0f} MB")
                sys.stdout.flush()
    sys.stdout.write("\n")
    tmp.replace(dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="download again even if the files exist")
    ap.add_argument("--manifest", default=str(HERE / "data_manifest.json"))
    a = ap.parse_args()

    manifest = json.loads(Path(a.manifest).read_text())
    base = manifest["base_url"].rstrip("/") + "/"
    needed = [f["name"] for f in manifest["files"]] or ["mtg_live.rl", "variants_slim.jsonl.gz"]
    if not a.force and all((HERE / n).exists() for n in needed):
        print("Data ready: " + ", ".join(needed) + " already here (kept as they are).")
        return
    if "<" in base or not manifest["files"]:
        raise SystemExit("data_manifest.json has no download address yet: run `make release` first "
                         "(or build the graph yourself with `make build`).")
    for f in manifest["files"]:
        target = HERE / f["name"]
        if target.exists() and not a.force:
            print(f"  {f['name']}: already here, keeping it")
            continue
        asset = HERE / f["asset"]
        print(f"Downloading {f['asset']} ({f.get('download_bytes', 0) / 1e6:,.0f} MB)…")
        try:
            download(base + f["asset"], asset, f.get("download_bytes"))
        except Exception as e:  # noqa: BLE001 - say what failed and how to retry
            raise SystemExit(f"Couldn't download {base + f['asset']}: {e}\nCheck your connection and run `make data` again.")
        if asset != target:                       # compressed for the download: unpack it
            print(f"  unpacking {f['asset']}…")
            with gzip.open(asset, "rb") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            asset.unlink()
        got = sha256(target)
        if got != f["sha256"]:
            target.unlink()
            raise SystemExit(f"{f['name']} didn't match its checksum (got {got[:12]}…, expected {f['sha256'][:12]}…). "
                             "Run `make data` again.")
        print(f"  {f['name']}: ok ({target.stat().st_size / 1e6:,.0f} MB, checksum verified)")
    print("Data ready.")


if __name__ == "__main__":
    main()
