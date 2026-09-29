/* modforge build index UI.
 * Reads index.json (produced by tools/build_index.py in CI) and renders one
 * card per mod: description, a split install button (install latest | ">"),
 * and a dropdown of particular builds. "Show non-release builds" adds the
 * rolling `ci` prerelease builds to the dropdowns and as install fallbacks.
 *
 * Install links use modl://<game_id>?url=<encoded asset URL> — Amethyst's
 * generic mod-link protocol. Every build also keeps a plain https link, since
 * modl:// only works where a handler is registered.
 */
"use strict";

const state = { index: null, showPrereleases: false };

function modlUrl(gameId, assetUrl) {
  return `modl://${gameId}?url=${encodeURIComponent(assetUrl)}`;
}

/* The FOMOD zip is the installable one; fall back to any zip, then any asset. */
function preferredAsset(build) {
  const assets = build.assets || [];
  return (
    assets.find((a) => /fomod.*\.zip$/i.test(a.name)) ||
    assets.find((a) => /\.zip$/i.test(a.name)) ||
    assets.find((a) => /\.7z$/i.test(a.name)) ||
    assets[0] ||
    null
  );
}

function visibleBuilds(mod) {
  return state.showPrereleases
    ? mod.builds
    : mod.builds.filter((b) => !b.prerelease);
}

function latestBuild(mod) {
  const builds = visibleBuilds(mod);
  return builds.find((b) => !b.prerelease) || builds[0] || null;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildRow(mod, build) {
  const asset = preferredAsset(build);
  const row = el("div", "build-row");
  const loginNote = mod.private ? " (private asset: GitHub login required)" : "";
  if (!asset) {
    row.appendChild(el("span", "build-label", `${build.tag} (no assets)`));
    return row;
  }
  const label = el("a", "build-label");
  label.href = modlUrl(mod.game_id, asset.url);
  label.title = `Install ${asset.name} via Amethyst${loginNote}`;
  label.textContent = build.tag;
  if (build.prerelease) label.appendChild(el("span", "badge", "CI"));
  const meta = el("span", "build-meta", fmtDate(build.published_at));
  const dl = el("a", "build-dl", "⤓");
  dl.href = asset.url;
  dl.target = "_blank";
  dl.rel = "noopener";
  dl.title = `Download ${asset.name} (${(asset.size / 1048576).toFixed(1)} MB)${loginNote}`;
  row.append(label, meta, dl);
  return row;
}

function modCard(mod) {
  const card = el("article", "mod-card");
  const head = el("div", "mod-head");
  const title = el("a", "mod-name", mod.name);
  title.href = mod.homepage;
  head.append(title, el("span", `kind kind-${mod.kind}`, mod.kind));
  if (mod.private) {
    const badge = el("span", "kind kind-private", "private");
    badge.title =
      "Private repository: download links only work while you are logged in to " +
      "GitHub. If Amethyst cannot fetch the file, use the ⤓ link and " +
      "Install mod from file…";
    head.append(badge);
  }
  card.append(head);
  card.appendChild(el("p", "mod-desc", mod.description || "(no description)"));

  const builds = visibleBuilds(mod);
  const latest = latestBuild(mod);
  const controls = el("div", "controls");

  const installWrap = el("div", "split");
  const install = el("a", "install-btn");
  if (latest && preferredAsset(latest)) {
    install.href = modlUrl(mod.game_id, preferredAsset(latest).url);
    install.title = `Install ${preferredAsset(latest).name} via Amethyst${mod.private ? " (private asset: GitHub login required in the downloader; prefer ⤓ + Install from file… if it fails)" : ""}`;
    install.textContent = `Install ${latest.tag}`;
  } else {
    install.classList.add("disabled");
    install.textContent = builds.length ? "No installable asset" : "No builds";
  }

  const chevron = el("button", "chevron", ">");
  chevron.setAttribute("aria-haspopup", "true");
  chevron.setAttribute("aria-expanded", "false");
  chevron.title = "Choose a particular build";

  const dropdown = el("div", "dropdown hidden");
  if (!builds.length) {
    dropdown.appendChild(el("div", "build-row", "no published builds"));
    chevron.disabled = true;
    chevron.classList.add("disabled");
  }
  for (const b of builds) dropdown.appendChild(buildRow(mod, b));

  chevron.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const open = dropdown.classList.toggle("hidden");
    chevron.setAttribute("aria-expanded", String(!open));
    closeAllExcept(dropdown);
  });

  installWrap.append(install, chevron, dropdown);
  controls.appendChild(installWrap);
  card.appendChild(controls);
  return card;
}

function closeAllExcept(keep) {
  for (const d of document.querySelectorAll(".dropdown:not(.hidden)")) {
    if (d !== keep) {
      d.classList.add("hidden");
      const btn = d.parentElement.querySelector(".chevron");
      if (btn) btn.setAttribute("aria-expanded", "false");
    }
  }
}

function render() {
  const root = document.getElementById("mod-list");
  root.textContent = "";
  const mods = (state.index && state.index.mods) || [];
  const withBuilds = mods.filter((m) => visibleBuilds(m).length);
  const rest = mods.filter((m) => !visibleBuilds(m).length);
  for (const mod of [...withBuilds, ...rest]) root.appendChild(modCard(mod));
  if (!mods.length) root.appendChild(el("p", "loading", "No repositories found."));
  const stamp = document.getElementById("index-stamp");
  if (state.index && state.index.generated_at) {
    stamp.textContent = `Build index generated ${state.index.generated_at} · ${mods.length} repositories`;
  }
}

async function boot() {
  try {
    const res = await fetch("index.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`index.json: HTTP ${res.status}`);
    state.index = await res.json();
  } catch (err) {
    document.getElementById("mod-list").textContent = "";
    document.getElementById("mod-list").appendChild(
      el("p", "loading", `Could not load the build index: ${err.message}`));
    return;
  }
  render();
}

document.getElementById("show-prereleases").addEventListener("change", (ev) => {
  state.showPrereleases = ev.target.checked;
  render();
});
document.addEventListener("click", () => closeAllExcept(null));
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeAllExcept(null);
});
boot();