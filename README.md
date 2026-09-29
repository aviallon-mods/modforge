# modforge

One place to **build, package and install everything under
[aviallon-mods](https://github.com/aviallon-mods)**:

- a shared CI pipeline that turns each repo's build output into a *validated*
  installable FOMOD zip (and keeps a rolling `ci` prerelease of the newest
  build);
- a GitHub page listing every mod with its builds, each one click to install
  into [Amethyst Mod Manager](https://github.com/ChrisDKN/Amethyst-Mod-Manager)
  via the `modl://` protocol.

## Layout

| Path | What |
|---|---|
| `tools/package_fomod.py` | assembles installable zip(s) from a repo's `fomod-package.toml` (explicit file list, deterministic zip, stamped `meta.ini`) |
| `tools/verify_fomod.py` | the shipping gate: exact entry list, ModuleConfig resolves to ≥1 `<plugin>`, payload byte-identity, `meta.ini` completeness. Mutation-tested |
| `tools/build_index.py` | collects releases + prereleases from **all** org repos into `site/index.json` |
| `.github/workflows/build-fomod.yml` | reusable workflow: build → package → verify → artifact → tag release → rolling `ci` prerelease |
| `.github/workflows/pages.yml` | hourly + on push: rebuild the index, deploy the page |
| `mods.toml` | curated page metadata (display name, short description, `modl://` game id) |
| `site/` | the page itself (vanilla JS: mod list, split install `>` button with a per-build dropdown, "show non-release builds") |

## Using the shared pipeline in a mod repo

```yaml
# .github/workflows/build.yml
jobs:
  package:
    uses: aviallon-mods/modforge/.github/workflows/build-fomod.yml@main
    with:
      mod_name: MyMod
      build_command: tools/build.sh          # your build; or pwsh + runs_on: windows-latest
      extra_artifact: build/out/MyMod.dll    # optional raw outputs
```

plus a `fomod-package.toml` (see `tools/package_fomod.py`'s docstring for the
full schema) and a committed `meta.ini` carrying the accurate, hand-curated
facts. CI stamps `version` / `installationFile` and ships `meta.ini` at the zip
root in the MO2/Amethyst `[General]` format, with dependencies in
`nexusRequirements=modId:name` pairs (`0:Name` for non-Nexus requirements).

Release convention: pushing a tag `vX.Y.Z` attaches the verified zip(s) to that
tag's GitHub release; every green push to `main` refreshes the rolling `ci`
prerelease that powers the page's "non-release builds" checkbox.

## Conventions the gate enforces

- the zip contains **exactly** the spec's file list + `meta.ini` — no directory
  entries, no parent directory, no stray files;
- `fomod/ModuleConfig.xml` parses and yields at least one `<plugin>` (an
  `optionalFileGroups` with zero plugins crashes Amethyst's FOMOD wizard with
  `AttributeError: 'NoneType' object has no attribute 'description'` — the
  mutation evidence is in `FaceGenGuard`'s `research/fomod-packaging-verification.md`);
- every payload entry is byte-identical to its build input;
- `meta.ini` has all eight core keys non-empty and agrees with the zip name.

## Adding a mod to the page

Add an entry to `mods.toml` (`repo`, `name`, `description`, `game_id`,
`order`). The page picks up the repo's releases and `ci` prereleases
automatically on the next hourly run.