#!/usr/bin/env python3
"""Collect build/release results across the aviallon-mods organisation into
one index for the modforge page.

For every repository in the org (plus the curated `mods.yaml` metadata), gather
all GitHub releases -- stable and prerelease -- with their downloadable assets,
so the page can offer a per-build install link (`modl://`) without the browser
needing a token. Actions artifacts are deliberately NOT indexed: they expire
and require auth, so they can never be a clickable install target. The rolling
`ci` prerelease written by build-fomod.yml is what fills the "non-release
builds" slot instead.

    tools/build_index.py --mods mods.toml --out site/index.json

Reads GITHUB_TOKEN from the environment when present (raises rate limits).
Writes deterministic output (sorted keys) so the Pages artifact diffs cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"


def gh(path: str, token: str | None):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r), dict(r.headers)


def gh_paged(path: str, token: str | None):
    out, page = [], 1
    while True:
        batch, headers = gh(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}", token)
        out.extend(batch)
        if len(batch) < 100 or page > 20:
            return out
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="aviallon-mods")
    ap.add_argument("--mods", default="mods.toml")
    ap.add_argument("--out", default="site/index.json")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    mods_path = Path(args.mods)
    curated = tomllib.loads(mods_path.read_text())["mods"]
    by_repo = {m["repo"]: m for m in curated}

    org_repos = gh_paged(f"/orgs/{args.org}/repos", token)
    known = {r["name"] for r in org_repos}

    missing = [r for r in by_repo if r not in known]
    if missing:
        print(f"::warning::mods.toml lists repos absent from {args.org}: {missing}")

    mods = []
    for repo_meta in sorted(org_repos, key=lambda r: r["name"]):
        name = repo_meta["name"]
        cur = by_repo.get(name, {})
        builds = []
        try:
            releases = gh_paged(f"/repos/{args.org}/{name}/releases", token)
        except Exception as e:
            print(f"::warning::{name}: releases fetch failed: {e}")
            releases = []
        for rel in releases:
            if rel.get("draft"):
                continue
            assets = [{
                "name": a["name"],
                "size": a["size"],
                "url": a["browser_download_url"],
            } for a in rel.get("assets", []) if a.get("browser_download_url")]
            if not assets:
                continue
            builds.append({
                "tag": rel["tag_name"],
                "prerelease": bool(rel.get("prerelease")),
                "published_at": rel.get("published_at") or "",
                "notes_url": rel.get("html_url") or "",
                "assets": assets,
            })
        builds.sort(key=lambda b: b["published_at"], reverse=True)
        mods.append({
            "repo": name,
            "name": cur.get("name", name),
            "description": cur.get("description") or repo_meta.get("description") or "",
            "game_id": cur.get("game_id", "skyrimspecialedition"),
            "kind": cur.get("kind", "mod"),
            "order": cur.get("order", 100),
            "homepage": repo_meta.get("html_url") or f"https://github.com/{args.org}/{name}",
            "archived": bool(repo_meta.get("archived")),
            "builds": builds,
        })

    mods.sort(key=lambda m: (m["order"], m["name"].lower()))
    index = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "org": args.org,
        "mods": mods,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")

    total_builds = sum(len(m["builds"]) for m in mods)
    print(f"== index: {len(mods)} repos, {total_builds} published builds -> {out}")
    for m in mods:
        rel = sum(1 for b in m["builds"] if not b["prerelease"])
        pre = len(m["builds"]) - rel
        print(f"   {m['repo']:<35} {rel} release / {pre} prerelease builds")
    return 0


if __name__ == "__main__":
    sys.exit(main())