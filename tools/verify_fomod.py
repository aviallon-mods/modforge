#!/usr/bin/env python3
"""Verify a packaged FOMOD zip before it can be shipped.

Spec-driven generalisation of FaceGenGuard's tools/verify-fomod.py (same
checks, same discipline: every check prints exactly one narrow observed claim;
exit 0 only when ALL pass; checks are independent so one failure does not hide
the verdict on the others).

A zip can be well-formed and still be INVALID. The defect this gate exists for:
`fomod/ModuleConfig.xml` whose `<optionalFileGroups>` yields ZERO `<plugin>`
elements (e.g. an empty `<optionalFileGroups/>`). Amethyst's FOMOD wizard
resolves `selected_plugin or first_plugin` and then reads `plugin.description`;
with zero plugins `first_plugin` is None and the wizard dies with
`AttributeError: 'NoneType' object has no attribute 'description'`. Such a zip
is rejected HERE, not on the user's machine.

    tools/verify_fomod.py --spec fomod-package.yaml dist/<zip>

Exit status: 0 all checks pass, 1 any fails, 2 usage error.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
# Minimal PE import-table reader (enough for DLL/EXE import names; no deps).

def pe_imports(d: bytes) -> list:
    import struct
    e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
    if d[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("no PE signature")
    coff = e_lfanew + 4
    nsec = struct.unpack_from("<H", d, coff + 2)[0]
    optsz = struct.unpack_from("<H", d, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", d, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", d, dd + 8)[0]
    if imp_rva == 0:
        return []
    secs = []
    for i in range(nsec):
        s = opt + optsz + i * 40
        vsz, va, rsz, raw = struct.unpack_from("<IIII", d, s + 8)
        secs.append((va, max(vsz, rsz), raw))

    def r2o(rva):
        for va, sz, raw in secs:
            if va <= rva < va + sz:
                return raw + (rva - va)
        raise ValueError(f"RVA {rva:#x} outside sections")

    out, off = [], r2o(imp_rva)
    while True:
        desc = struct.unpack_from("<IIIII", d, off)
        if desc[3] == 0:
            break
        out.append(d[r2o(desc[3]):].split(b"\0")[0].decode("ascii", "replace"))
        off += 20
    return out


# Windows system DLLs an executable may import without shipping them.
SYSTEM_DLLS = {
    "kernel32.dll", "kernelbase.dll", "user32.dll", "gdi32.dll", "advapi32.dll",
    "shell32.dll", "shlwapi.dll", "ole32.dll", "oleaut32.dll", "combase.dll",
    "comdlg32.dll", "version.dll", "bcrypt.dll", "crypt32.dll", "secur32.dll",
    "ws2_32.dll", "winhttp.dll", "wininet.dll", "winmm.dll", "dbghelp.dll",
    "psapi.dll", "setupapi.dll", "powrprof.dll", "userenv.dll", "imm32.dll",
    "d3d11.dll", "d3d12.dll", "d3d9.dll", "d3dcompiler_47.dll", "dxgi.dll",
    "dwrite.dll", "dwmapi.dll", "opengl32.dll", "glu32.dll", "hid.dll",
    "cfgmgr32.dll", "profapi.dll", "wtsapi32.dll", "wldap32.dll", "netapi32.dll",
    "dnsapi.dll", "iphlpapi.dll", "cabinet.dll", "msimg32.dll", "usp10.dll",
    "rpcrt4.dll", "ntdll.dll", "wintrust.dll", "imagehlp.dll", "dbcore.dll",
    # Windows system DLLs observed in real shipped payloads (WinUI/.NET stacks):
    "mscoree.dll", "shcore.dll", "d2d1.dll", "dcomp.dll", "dxcore.dll",
    "bcp47langs.dll", "bcp47mrm.dll", "coremessaging.dll", "xmllite.dll",
    "rometadata.dll", "urlmon.dll", "uxtheme.dll", "comctl32.dll",
    "elscore.dll", "sspicli.dll", "propsys.dll", "twinapi.appcore.dll",
    "inputservice.dll", "wldp.dll", "msasn1.dll", "ncrypt.dll",
    "cryptsp.dll", "rsaenh.dll", "dpx.dll", "mscms.dll", "coloradapterclient.dll",
}
# VC runtime families are shipped by the VC++ Redistributable (a documented
# runtime requirement in meta.ini), never by the mod itself.
VC_RUNTIME_RE = re.compile(
    r"^(msvcp|msvcr|vcruntime|concrt|vccorlib|ucrtbase|api-ms-win-crt|ext-ms-win-)",
    re.I)


def descendants(el, name):
    return [e for e in el.iter() if localname(e.tag) == name]


class Ctx:
    def __init__(self, zip_path: Path, spec: dict, root: Path):
        self.zip_path = zip_path
        self.spec = spec
        self.root = root
        with zipfile.ZipFile(zip_path) as z:
            self.names = z.namelist()
            self.entries = {n: z.read(n) for n in self.names if not n.endswith("/")}
        self.xml_root = None
        self.expected = sorted(
            [item["dest"] for item in spec["files"]] + ["meta.ini"])


def check_zip_entries(ctx: Ctx):
    observed = sorted(ctx.names)
    hostile = [n for n in ctx.names
               if n.startswith("/") or "\\" in n or ".." in n.split("/")]
    ok = observed == ctx.expected and len(ctx.names) == len(ctx.expected) and not hostile
    if ok:
        return True, (f"{ctx.zip_path.name}: exactly {len(ctx.expected)} file entries "
                      f"{observed}; no directory entries, all paths relative")
    return False, (f"{ctx.zip_path.name}: expected exactly {ctx.expected}, observed "
                   f"{len(ctx.names)} entries {observed}"
                   + (f"; hostile path shapes {hostile}" if hostile else ""))


def check_xml_parse(ctx: Ctx):
    name = "fomod/ModuleConfig.xml"
    if ctx.spec.get("fomod", True) is False:
        return True, (f"{name}: not applicable - spec declares fomod = false (plain mod zip)")
    data = ctx.entries.get(name)
    if data is None:
        return False, (f"{name}: absent from {ctx.zip_path.name} "
                       f"(observed entries: {sorted(ctx.names)})")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return False, f"{name} ({len(data)} B): does not parse as XML: {e}"
    ctx.xml_root = root
    return True, (f"{name} ({len(data)} B): parses as XML; root element "
                  f"<{localname(root.tag)}>")


def check_moduleconfig_plugins(ctx: Ctx):
    """THE defect guard: the config must yield >= 1 <plugin>, and if
    <optionalFileGroups> is present it must itself yield >= 1."""
    name = "fomod/ModuleConfig.xml"
    if ctx.spec.get("fomod", True) is False:
        return True, f"{name}: not applicable - spec declares fomod = false (plain mod zip)"
    if ctx.xml_root is None:
        return False, (f"{name}: cannot count <plugin> - the XML did not parse "
                       f"(see the check above)")
    all_plugins = descendants(ctx.xml_root, "plugin")
    groups = descendants(ctx.xml_root, "optionalFileGroups")
    group_plugins = [p for g in groups for p in descendants(g, "plugin")]
    names = [p.get("name") for p in all_plugins]
    if not all_plugins:
        return False, (f"{name}: 0 <plugin> in the whole config (need >= 1) - Amethyst's "
                       f"wizard resolves `selected_plugin or first_plugin` then reads "
                       f"`plugin.description`; with zero plugins it dies with AttributeError")
    if groups and not group_plugins:
        return False, (f"{name}: <optionalFileGroups> yields 0 <plugin> of {len(all_plugins)} "
                       f"total (need >= 1 inside the groups)")
    return True, (f"{name}: {len(all_plugins)} <plugin> {names}; "
                  f"<optionalFileGroups> present={bool(groups)} "
                  f"with {len(group_plugins)} plugins")


def check_payload_bytes(ctx: Ctx):
    """Every payload entry must be byte-identical to the build input it claims
    to be -- a packaging step that copies the wrong file must turn red."""
    mismatches, checked = [], 0
    for item in ctx.spec["files"]:
        dest = item["dest"]
        data = ctx.entries.get(dest)
        if data is None:
            mismatches.append(f"{dest}: absent")
            continue
        src = ctx.root / item["src"]
        want = src.read_bytes()
        checked += 1
        if data != want:
            mismatches.append(
                f"{dest}: {len(data)} B in zip != {len(want)} B from {item['src']} "
                f"(zip sha256 {hashlib.sha256(data).hexdigest()[:12]}, "
                f"src sha256 {hashlib.sha256(want).hexdigest()[:12]})")
    if mismatches:
        return False, "; ".join(mismatches)
    return True, (f"payload: {checked} entries byte-identical to their spec sources "
                  f"({', '.join(i['dest'] for i in ctx.spec['files'])})")


def check_meta_ini(ctx: Ctx):
    data = ctx.entries.get("meta.ini")
    if data is None:
        return False, "meta.ini: absent from the zip root"
    parser = configparser.ConfigParser(strict=False, allow_no_value=True)
    try:
        parser.read_string(data.decode("utf-8"))
    except Exception as e:
        return False, f"meta.ini ({len(data)} B): does not parse: {e}"
    if "General" not in parser:
        return False, "meta.ini: no [General] section"
    g = {k.lower(): (v or "") for k, v in parser["General"].items()}

    # The FULL managers' schema (the set Amethyst writes for a Nexus install):
    # readers tolerate missing keys but then show the mod nameless/versionless
    # (observed: modlist_data.py reads meta.version, meta.category_name, ...).
    schema = [
        "gamename", "modid", "fileid", "version", "author", "uploadedby",
        "nexusname", "nexusfilename", "installationfile", "filesize",
        "installed", "nexusurl", "description", "categoryid", "categoryname",
        "filecategory", "endorsed", "latestfileid", "latestversion",
        "hasupdate", "ignoreupdate", "ignoredversion", "missingrequirements",
        "nexusrequirements", "ignoredrequirements", "fomod", "rootfolder",
        "fromcollection",
    ]
    missing = [k for k in schema if k not in g]
    problems = []
    if missing:
        problems.append(f"{len(missing)} schema key(s) absent: {missing}")
    for key in ("gamename", "version", "author", "nexusname", "nexusurl",
                "description", "installationfile", "filecategory", "fomod"):
        if not g.get(key, "").strip():
            problems.append(f"{key} empty")
    version = g.get("version", "")
    if version and version not in ctx.zip_path.name:
        problems.append(f"version {version!r} does not appear in zip name {ctx.zip_path.name!r}")
    if g.get("installationfile", "") != ctx.zip_path.name:
        problems.append(f"installationfile={g.get('installationfile')!r} != {ctx.zip_path.name!r}")
    reqs = g.get("nexusrequirements", "")
    if reqs and not re.fullmatch(r"\d+:[^;]+(;\d+:[^;]+)*", reqs):
        problems.append(f"nexusRequirements not `modId:name(;modId:name)*`: {reqs!r}")
    if g.get("filesize", "") and not g["filesize"].strip().isdigit():
        problems.append(f"filesize not numeric: {g['filesize']!r}")
    if problems:
        return False, "meta.ini [General]: " + "; ".join(problems)
    req_n = len(reqs.split(";")) if reqs else 0
    return True, (f"meta.ini [General]: all {len(schema)} schema keys present; "
                  f"version={version}; installationfile={ctx.zip_path.name}; "
                  f"filesize={g.get('filesize')}; nexusRequirements: {req_n} entries")


def check_import_coverage(ctx: Ctx):
    """Every non-system DLL import of every shipped PE must be shipped too.

    The defect this exists for: a plugin that BUILDS and whose zip passes every
    shape check, but dies in game with SKSE's `couldn't load plugin
    (00000007E)` = ERROR_MOD_NOT_FOUND because an import (spdlog.dll, fmt.dll
    via a dynamic vcpkg triplet) was never packaged. Nothing else can see it:
    byte-identity proves the zip copies its inputs, and off-game tests link
    their own copies of the dependency."""
    shipped = {n.split("/")[-1].lower() for n in ctx.entries}
    problems, checked = [], 0
    for name, blob in sorted(ctx.entries.items()):
        if not name.lower().endswith((".dll", ".exe")) or not blob.startswith(b"MZ"):
            continue
        try:
            imports = pe_imports(blob)
        except Exception as e:
            return False, f"{name}: PE import table unreadable: {e}"
        for imp in imports:
            low = imp.lower()
            if (low in shipped or low in SYSTEM_DLLS
                    or low.startswith("api-ms-win-") or VC_RUNTIME_RE.match(low)):
                continue
            problems.append(f"{name} imports {imp} (not shipped, not system/redist)")
        checked += 1
    if problems:
        return False, "import coverage: " + "; ".join(problems)
    return True, (f"import coverage: {checked} PE files, every non-system import "
                  f"ships inside the zip (or is OS/VC-redist)")


CHECKS = [check_zip_entries, check_xml_parse, check_moduleconfig_plugins,
          check_payload_bytes, check_meta_ini, check_import_coverage]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", action="append", required=True,
                    help="fomod-package.toml (repeat for several zips)")
    ap.add_argument("zips", nargs="+", type=Path)
    args = ap.parse_args()

    specs = []
    for spec_path in args.spec:
        try:
            specs.append((Path(spec_path).resolve(), tomllib.loads(Path(spec_path).read_text())))
        except tomllib.TOMLDecodeError as e:
            print(f"FATAL: {spec_path}: does not parse as TOML: {e}", file=sys.stderr)
            return 2

    # Each spec owns exactly one zip (by its resolved zip_name); the set of
    # given zips must match exactly -- a missing or unexpected zip is an error,
    # not something to skip past.
    by_name = {z.name: z for z in args.zips}
    pairs, problems = [], []
    for spec_path, spec in specs:
        import re as _re
        version = _resolve_for_verify(spec_path.parent, spec)
        zip_name = spec["zip_name"].format(version=version)
        z = by_name.pop(zip_name, None)
        if z is None:
            problems.append(f"{spec_path.name}: expected zip {zip_name!r} not among the given zips")
        else:
            pairs.append((z, spec))
    problems.extend(f"unexpected zip {n!r} (no spec claims it)" for n in sorted(by_name))
    if problems:
        for p in problems:
            print(f"[FAIL] {p}")
        return 1

    failed = 0
    checks_run = 0
    for z, spec in pairs:
        ctx = Ctx(z, spec, specs[0][0].parent)
        ctx.root = [sp for sp, s in specs if s is spec][0].parent
        for check in CHECKS:
            ok, claim = check(ctx)
            print(f"[{'PASS' if ok else 'FAIL'}] {z.name}: {claim}")
            if not ok:
                failed += 1
            checks_run += 1
    print(f"== {checks_run - failed}/{checks_run} checks passed across {len(pairs)} zip(s)")
    return 1 if failed else 0


def _resolve_for_verify(root: Path, spec: dict) -> str:
    """Same version resolution as the packager (duplicated on purpose: a verify
    that asked the packager what version it used could only ever agree)."""
    ver = spec.get("version")
    if isinstance(ver, str):
        return ver
    if isinstance(ver, list):
        ver = {"regex": ver}
    kind, arg = next(iter(ver.items()))
    if kind == "cmake":
        m = re.search(r"^project\s*\(\s*\S+\s+VERSION\s+([0-9][0-9.]*)",
                      (root / arg).read_text(), re.M)
        return m.group(1) if m else ""
    if kind == "file":
        return (root / arg).read_text().strip()
    if kind == "regex":
        m = re.search(arg[1], (root / arg[0]).read_text())
        return m.group(1) if m else ""
    return ""


if __name__ == "__main__":
    sys.exit(main())