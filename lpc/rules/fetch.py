"""Cross-platform equivalent of scripts/fetch_rules.ps1.

    python -m lpc.rules.fetch [--out rules/sources]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

import httpx

MANIFEST = Path(__file__).resolve().parent / "sources.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="rules/sources")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    sources = json.loads(MANIFEST.read_text(encoding="utf-8"))["sources"]
    lock, manual = [], []
    with httpx.Client(follow_redirects=True, timeout=60, headers={"User-Agent": "LawPracticeConsole/0.1"}) as client:
        for src in sources:
            url = src.get("download")
            if not url:
                manual.append(src)
                continue
            dest = out / (src["id"] + (".pdf" if url.lower().endswith(".pdf") else ".html"))
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"FAILED {src['id']}: {exc}")
                manual.append(src)
                continue
            dest.write_bytes(resp.content)
            lock.append({"id": src["id"], "file": dest.name, "url": url,
                         "sha256": hashlib.sha256(resp.content).hexdigest(),
                         "downloaded": dt.date.today().isoformat()})
            print(f"saved  {dest}")
    (out / "manifest.lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    if manual:
        print("\nSave these by hand from their landing pages:")
        for src in manual:
            print(f"  {src['id']:<24} {src['landing']}")


if __name__ == "__main__":
    main()
