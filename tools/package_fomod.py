#!/usr/bin/env python3
"""Assemble an installable FOMOD zip from a `fomod-package.yaml` spec.

This is the shared, spec-driven version of the per-repo package-fomod.sh
scripts (FaceGenGuard's is the original). The zip is only ASSEMBLED here;
`verify_fomod.py` is the gate that decides whether the result is a package
Amethyst/MO2 can actually install. CI runs the two together, and so should you.

    tools/package_fomod.py --spec fomod-package.yaml --out dist

Spec format is TOML (stdlib `tomllib`, zero dependencies), all paths relative
to the spec file's directory = repo root:

    name = "FaceGenGuard"
    zip_name = "FaceGenGuard-{version}-fomod.zip"
    version = { cmake = "CMakeLists.txt" }   # or { file = "VERSION" } or { regex = [file, pattern] }
    game_name = "skyrimspecialedition"
    author = "aviallon"
    description = "One-line summary shown in the mod manager."
    nexus_name = "FaceGenGuard"              # display name (may differ from repo)
    nexus_url = "https://github.com/aviallon-mods/FaceGenGuard"
    file_category = "MAIN"
    requirements = [                         # -> nexusRequirements=modId:name pairs
      "0:Address Library for SKSE Plugins", # 0: = external (no Nexus mod id)
      "0:SKSE64",
    ]
    fomod = true                             # false = plain mod zip (no ModuleConfig checks)
    [[files]]                                # explicit list: exactly these entries
    src = "fomod/ModuleConfig.xml"
    dest = "fomod/ModuleConfig.xml"
    [[files]]
    src = "build/out/FaceGenGuard.dll"
    dest = "SKSE/Plugins/FaceGenGuard.dll"

The zip gets exactly: every `files` entry + `meta.ini` at the root (stamped:
version, installationFile, fileSize), and nothing else -- no directory entries,
no parent directory. Deterministic output: fixed timestamps, fixed ordering.

Pass --spec several times to build several zips from one repo (e.g. a FOMOD
zip and a plain zip).

Version is a single source of truth read from the repo (CMakeLists/VERSION
file), never hardcoded here: a version duplicated in a script produced an asset
whose name lied about its contents.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import re
import sys
import tomllib
import zipfile
from pathlib import Path


def fail(msg: str) -> "None":
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def load_spec(path: Path) -> dict:
    try:
        spec = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        fail(f"{path}: does not parse as TOML: {e}")
    if not isinstance(spec, dict):
        fail(f"{path}: spec must be a mapping")
    return spec


def resolve_version(root: Path, spec: dict) -> str:
    ver = spec.get("version")
    if isinstance(ver, str):
        return ver
    if isinstance(ver, list):  # {regex = [file, pattern]} arrives as a list
        ver = {"regex": ver}
    if not isinstance(ver, dict) or len(ver) != 1:
        fail("spec `version:` must be a string or a single-key mapping "
             "({cmake: file} | {file: file} | {regex: [file, pattern]})")
    kind, arg = next(iter(ver.items()))
    if kind == "cmake":
        text = (root / arg).read_text()
        m = re.search(r"^project\s*\(\s*\S+\s+VERSION\s+([0-9][0-9.]*)", text, re.M)
        if not m:
            fail(f"{arg}: no `project(<name> VERSION x.y.z)` line")
        return m.group(1)
    if kind == "file":
        return (root / arg).read_text().strip()
    if kind == "regex":
        fname, pattern = arg[0], arg[1] if isinstance(arg, list) else arg
        m = re.search(pattern, (root / fname).read_text())
        if not m:
            fail(f"{fname}: pattern {pattern!r} did not match")
        return m.group(1)
    fail(f"unknown version source {kind!r}")


def stamp_meta_ini(root: Path, spec: dict, version: str, zip_name: str, entries: list,
                   file_size: int | None = None) -> str:
    """Build the meta.ini text: the repo's committed meta.ini (accurate facts,
    hand-curated) completed into the FULL MO2/Amethyst [General] schema (28
    keys, the shape the managers themselves write on install), with the
    build-stamped keys overwritten. Keys are lowercase like the managers'
    own output; readers are case-insensitive (configparser's default
    optionxform lowercases on read, verified against Amethyst's read_meta).

    The schema below is exactly the key set Amethyst writes for a Nexus
    install (observed on Save Unbaker's meta.ini): missing keys are tolerated
    by readers but leave the mod nameless/versionless in the UI."""
    # (key, default) in the managers' canonical order.
    schema = [
        ("gamename", ""), ("modid", ""), ("fileid", ""), ("version", ""),
        ("author", ""), ("uploadedby", ""), ("nexusname", ""),
        ("nexusfilename", ""), ("installationfile", ""), ("filesize", ""),
        ("installed", ""), ("nexusurl", ""), ("description", ""),
        ("categoryid", ""), ("categoryname", ""), ("filecategory", ""),
        ("endorsed", "false"), ("latestfileid", "0"), ("latestversion", ""),
        ("hasupdate", "false"), ("ignoreupdate", "false"),
        ("ignoredversion", ""), ("missingrequirements", ""),
        ("nexusrequirements", ""), ("ignoredrequirements", ""),
        ("fomod", "true"), ("rootfolder", "false"), ("fromcollection", ""),
    ]

    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # keep curated case while reading
    if meta_path := (root / "meta.ini"):
        if meta_path.exists():
            parser.read_string(meta_path.read_text())
    if "General" not in parser:
        parser["General"] = {}
    g = parser["General"]
    # Curated keys may be written in any case (gameName vs gamename): fold the
    # existing ones onto lowercase names before stamping.
    folded = {k.lower(): v for k, v in g.items()}
    g.clear()
    g.update(folded)

    g["gamename"] = str(spec.get("game_name", g.get("gamename", "")))
    g["version"] = version
    g["author"] = str(spec.get("author", g.get("author", "")))
    g["uploadedby"] = str(spec.get("uploaded_by", g.get("uploadedby", g["author"])))
    g["nexusname"] = str(spec.get("nexus_name", g.get("nexusname", spec.get("name", ""))))
    g["nexusurl"] = str(spec.get("nexus_url", g.get("nexusurl", "")))
    g["description"] = str(spec.get("description", g.get("description", ""))).replace("\n", " ")
    g["installationfile"] = zip_name
    if file_size is not None:
        g["filesize"] = str(file_size)
    g["filecategory"] = str(spec.get("file_category", g.get("filecategory", "MAIN")))
    if spec.get("category_name") is not None:
        g["categoryname"] = str(spec["category_name"])
    if spec.get("category_id") is not None:
        g["categoryid"] = str(spec["category_id"])
    if spec.get("requirements") is not None:
        g["nexusrequirements"] = ";".join(str(r) for r in spec["requirements"])
    g["fomod"] = "false" if spec.get("fomod", True) is False else "true"
    g["rootfolder"] = "true" if spec.get("root_folder", False) else "false"

    out = ["[General]"]
    for key, default in schema:
        out.append(f"{key} = {g.get(key, default) or ''}")
    # Preserve any curated key outside the schema instead of dropping it.
    for key, value in g.items():
        if key.lower() not in {k for k, _ in schema}:
            out.append(f"{key} = {value}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", action="append", required=True,
                    help="fomod-package.toml (repeat for several zips)")
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    for spec_arg in args.spec:
        if build_one(Path(spec_arg).resolve(), outdir) != 0:
            return 1
    return 0


def build_one(spec_path: Path, outdir: Path) -> int:
    root = spec_path.parent
    spec = load_spec(spec_path)

    for key in ("name", "zip_name", "files"):
        if key not in spec:
            fail(f"spec is missing `{key}:`")
    files = spec["files"]
    if not files:
        fail("spec `files:` is empty")

    version = resolve_version(root, spec)
    zip_name = spec["zip_name"].format(version=version)

    # Explicit file list -> exactly these entries plus meta.ini. A directory
    # walk would smuggle in stray files; the release must be the file list.
    entries = []
    for item in files:
        src, dest = root / item["src"], item["dest"]
        if not src.is_file():
            fail(f"missing build input: {item['src']}")
        # Spaces are legitimate in mod filenames ('Devious Devices - Assets.esm',
        # 'res/Maya FBX Skyrim Fix.txt'); reject only genuinely hostile shapes:
        # absolute paths, backslashes, '..' segments, control chars.
        if (not dest or dest.startswith("/") or "\\" in dest or ".." in dest.split("/")
                or any(ord(c) < 32 or ord(c) == 127 for c in dest)
                or not re.fullmatch(r"[\w.\- /]+", dest)):
            fail(f"hostile archive path: {dest!r}")
        entries.append((str(src), dest))

    zpath = outdir / zip_name

    def write_zip(meta_text: str) -> None:
        # Deterministic zip: fixed member order, fixed timestamps.
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for src, dest in sorted(entries, key=lambda e: e[1]):
                z.writestr(zipfile.ZipInfo(dest, date_time=(1980, 1, 1, 0, 0, 0)),
                           Path(src).read_bytes())
            z.writestr(zipfile.ZipInfo("meta.ini", date_time=(1980, 1, 1, 0, 0, 0)),
                       meta_text)

    # Two-pass filesize: the managers' meta.ini schema carries the archive's
    # own size, which is only known once the zip exists. Rebuild until the
    # stamped size equals the real one (converges in 2-3 passes).
    file_size = None
    for _ in range(3):
        meta_text = stamp_meta_ini(root, spec, version, zip_name, entries, file_size)
        if not meta_text.startswith("[General]"):
            fail("meta.ini does not start with [General]")
        write_zip(meta_text)
        size = zpath.stat().st_size
        if file_size is not None and size == file_size:
            break
        file_size = size

    print(f"== packaged: {zpath}")
    h = hashlib.sha256(zpath.read_bytes()).hexdigest()
    print(f"   sha256 {h}")
    with zipfile.ZipFile(zpath) as z:
        for i in z.infolist():
            print(f"   {i.file_size:>8}  {i.filename}")
    print(zpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())