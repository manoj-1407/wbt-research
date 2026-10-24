#!/usr/bin/env python3
"""
scripts/verify_github_api.py
----------------------------
Queries the public GitHub Actions API without authentication for the three
matched QEMU benchmark runs (40002470957, 40004718895, 40004771234) and
prints run status, conclusion, commit SHA, artifact IDs, sizes, and digests.
"""

import urllib.request
import json
import sys

RUNS = ["40002470957", "40004718895", "40004771234"]

def main():
    print("=" * 80)
    print("GitHub Actions Public API Query (manoj-1407/wbt-research)")
    print("=" * 80)
    for r_id in RUNS:
        req = urllib.request.Request(
            f"https://api.github.com/repos/manoj-1407/wbt-research/actions/runs/{r_id}",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read().decode())
                print(f"\nRun {r_id}: status={d.get('status')}, conclusion={d.get('conclusion')}, "
                      f"head_sha={d.get('head_sha')}, created_at={d.get('created_at')}")
        except Exception as e:
            print(f"Error fetching run {r_id}: {e}")
            continue

        req_art = urllib.request.Request(
            f"https://api.github.com/repos/manoj-1407/wbt-research/actions/runs/{r_id}/artifacts",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        try:
            with urllib.request.urlopen(req_art, timeout=10) as resp:
                d_art = json.loads(resp.read().decode())
                for a in d_art.get("artifacts", []):
                    print(f"   Artifact ID: {a.get('id')}, name: {a.get('name')}, size: {a.get('size_in_bytes')} bytes, "
                          f"expired: {a.get('expired')}\n   digest: {a.get('digest')}")
        except Exception as e:
            print(f"Error fetching artifacts for {r_id}: {e}")

if __name__ == "__main__":
    main()
