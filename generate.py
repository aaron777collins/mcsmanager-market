#!/usr/bin/env python3
"""Generate an up-to-date MCSManager market.json.

Takes the official MCSManager market (https://script.mcsmanager.com/market.json)
as the base, keeps every non-generated entry and the language/template links
untouched, and regenerates the Minecraft server entries (Vanilla, Fabric,
Paper, Folia, Purpur, Forge, NeoForge) from the projects' official metadata
APIs.  Entry layout is copied from the upstream entries so the panel treats
them exactly like official ones.

Python 3 standard library only.

Usage:
    python3 generate.py                 # generate, validate, write market.json if changed
    python3 generate.py --check         # only validate the existing market.json
    python3 generate.py --strict        # treat a failed loader as a fatal error
Exit codes: 0 ok (changed or not), 1 failure, nothing written.
"""
import argparse
import concurrent.futures
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UPSTREAM_URL = "https://script.mcsmanager.com/market.json"
UA = "mcsmanager-market-generator (+https://github.com/aaron777collins/mcsmanager-market)"

# How many of the newest Mojang release lines (26.3, 26.2, 26.1.x, ...) to offer.
NEW_SCHEME_LINES = 4
# Older, still popular 1.x versions kept in addition (only if the loader has them).
LEGACY_VERSIONS = ["1.21.11", "1.21.10", "1.21.8", "1.21.4", "1.21.1"]
# Extra versions only offered for specific loaders.
EXTRA_VERSIONS = {"mc-forge": ["1.20.1"]}

JDK_IMAGES = [8, 11, 17, 21, 25]  # eclipse-temurin:<n>-jdk tags that exist


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def _request(url, method="GET", headers=None, timeout=30):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            with _request(url) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def get_json(url):
    return json.loads(fetch(url).decode("utf-8"))


def url_check(url, retries=2):
    """Return (ok, content_length_or_None). HEAD first, tiny ranged GET as fallback."""
    for _ in range(retries):
        for method, hdr in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
            try:
                with _request(url, method=method, headers=hdr) as r:
                    if r.status < 400:
                        size = r.headers.get("Content-Length")
                        if r.status == 206:
                            m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
                            size = m.group(1) if m else None
                        return True, int(size) if size and size.isdigit() else None
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return False, None
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.5)
    return False, None


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def mb(n):
    return f"{max(1, round(n / 1048576))}MB"


def jdk_image(major):
    tag = next((j for j in JDK_IMAGES if j >= major), JDK_IMAGES[-1])
    return f"eclipse-temurin:{tag}-jdk"


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def titlever(title):
    m = re.search(r"Minecraft (\S+)", title)
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# Templates: copy layout/static text from existing (upstream) entries
# ----------------------------------------------------------------------------
class Templates:
    def __init__(self, *sources):
        self.sources = sources  # lists of packages, searched in order

    def get(self, category, platform):
        for pkgs in self.sources:
            for p in pkgs:  # newest first in upstream
                if p.get("category") == category and p.get("platform") == platform and titlever(p["title"]):
                    return p
        raise RuntimeError(f"no template entry for {category}/{platform}")


def make_entry(tmpl, version, **over):
    """Clone a template entry, swapping the Minecraft version in text fields."""
    e = copy.deepcopy(tmpl)
    old = titlever(tmpl["title"])
    e["title"] = re.sub(r"(Minecraft )\S+", lambda m: m.group(1) + version, tmpl["title"])
    e["description"] = tmpl["description"].replace(old, version)
    for k, v in over.items():
        e[k] = v
    return e


def set_java(e, java):
    e["runtime"] = f"Java {java}+"
    img = jdk_image(java)
    e["dockerOptional"] = {"image": img, "updateCommandImage": img}


def set_start_jar(e, jar):
    s = e["setupInfo"]
    s["startCommand"] = re.sub(r"-jar \S+", f"-jar {jar}", s["startCommand"])


# ----------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------
class Mojang:
    def __init__(self):
        m = get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        self.releases = [v["id"] for v in m["versions"] if v["type"] == "release"]
        self.urls = {v["id"]: v["url"] for v in m["versions"]}
        self._cache = {}

    def version_meta(self, v):
        if v not in self._cache:
            self._cache[v] = get_json(self.urls[v])
        return self._cache[v]

    def java(self, v):
        return self.version_meta(v).get("javaVersion", {}).get("majorVersion", 8)

    def pick_versions(self):
        lines = {}
        for v in self.releases:
            m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", v)
            if not m or int(m[1]) < 26:
                continue
            key, patch = (int(m[1]), int(m[2])), int(m[3] or 0)
            if key not in lines or patch > lines[key][0]:
                lines[key] = (patch, v)
        new = [lines[k][1] for k in sorted(lines, reverse=True)[:NEW_SCHEME_LINES]]
        return new + [v for v in LEGACY_VERSIONS if v in self.releases]


def versions_for(mojang, category):
    vs = mojang.pick_versions() + [v for v in EXTRA_VERSIONS.get(category, []) if v in mojang.releases]
    return sorted(set(vs), key=vkey, reverse=True)


# ----------------------------------------------------------------------------
# Loader generators. Each returns a list of entries (newest version first).
# ----------------------------------------------------------------------------
def gen_vanilla(ctx):
    t = ctx.tmpl.get("mc-vanilla", "ALL")
    out = []
    for v in versions_for(ctx.mojang, "mc-vanilla"):
        srv = ctx.mojang.version_meta(v).get("downloads", {}).get("server")
        if not srv:
            continue
        e = make_entry(t, v, targetLink=srv["url"], size=mb(srv["size"]))
        set_java(e, ctx.mojang.java(v))
        out.append(e)
    return out


def gen_fabric(ctx):
    t = ctx.tmpl.get("mc-fabric", "ALL")
    games = {g["version"] for g in get_json("https://meta.fabricmc.net/v2/versions/game") if g["stable"]}
    inst = next(i for i in get_json("https://meta.fabricmc.net/v2/versions/installer") if i["stable"])
    out = []
    for v in versions_for(ctx.mojang, "mc-fabric"):
        if v not in games:
            continue
        e = make_entry(t, v, targetLink=inst["url"])
        e["setupInfo"]["updateCommand"] = (
            f"java -jar fabric-installer-{inst['version']}.jar server -mcversion {v} -downloadMinecraft -noprofile"
        )
        set_java(e, ctx.mojang.java(v))
        out.append(e)
    return out


def _fill(project, ctx, category):
    t = ctx.tmpl.get(category, "ALL")
    avail = set()
    for group in get_json(f"https://fill.papermc.io/v3/projects/{project}")["versions"].values():
        avail.update(group)
    out = []
    for v in versions_for(ctx.mojang, category):
        if v not in avail:
            continue
        builds = [b for b in get_json(f"https://fill.papermc.io/v3/projects/{project}/versions/{v}/builds")
                  if b.get("channel") in ("STABLE", "BETA") and "server:default" in b.get("downloads", {})]
        if not builds:
            continue
        d = max(builds, key=lambda b: b["id"])["downloads"]["server:default"]
        e = make_entry(t, v, targetLink=d["url"], size=mb(d["size"]))
        set_start_jar(e, d["name"])
        set_java(e, ctx.mojang.java(v))
        out.append(e)
    return out


def gen_paper(ctx):
    return _fill("paper", ctx, "mc-paper")


def gen_folia(ctx):
    return _fill("folia", ctx, "mc-folia")


def gen_purpur(ctx):
    t = ctx.tmpl.get("mc-purpur", "ALL")
    avail = set(get_json("https://api.purpurmc.org/v2/purpur")["versions"])
    out = []
    for v in versions_for(ctx.mojang, "mc-purpur"):
        if v not in avail:
            continue
        build = get_json(f"https://api.purpurmc.org/v2/purpur/{v}")["builds"]["latest"]
        # MCSManager saves the download under the last URL path segment ("download"),
        # but decides zip-vs-jar from the whole URL's extension, hence the ?file=...jar suffix.
        url = f"https://api.purpurmc.org/v2/purpur/{v}/{build}/download?file=purpur-{v}-{build}.jar"
        ok, size = url_check(url)
        if not ok:
            log(f"  skip Purpur {v}: download not reachable")
            continue
        e = make_entry(t, v, targetLink=url, size=mb(size) if size else t["size"])
        set_start_jar(e, "download")
        set_java(e, ctx.mojang.java(v))
        out.append(e)
    return out


def _installer_entries(ctx, category, v, url, installer):
    out = []
    for platform in ("Linux", "Windows"):
        t = ctx.tmpl.get(category, platform)
        e = make_entry(t, v, targetLink=url)
        e["setupInfo"]["updateCommand"] = f"java -jar {installer} --installServer"
        set_java(e, ctx.mojang.java(v))
        out.append(e)
    return out


def gen_forge(ctx):
    promos = get_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")["promos"]
    out = []
    for v in versions_for(ctx.mojang, "mc-forge"):
        fv = promos.get(f"{v}-latest") or promos.get(f"{v}-recommended")
        if not fv:
            continue
        full = f"{v}-{fv}"
        url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar"
        if not url_check(url)[0]:
            log(f"  skip Forge {v}: installer not reachable")
            continue
        out += _installer_entries(ctx, "mc-forge", v, url, f"forge-{full}-installer.jar")
    return out


def gen_neoforge(ctx):
    root = ET.fromstring(fetch("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"))
    allv = [x.text for x in root.iter("version")]
    out = []
    for v in versions_for(ctx.mojang, "mc-neoforge"):
        parts = v.split(".")
        if parts[0] == "1":      # 1.21.4 -> 21.4.*, 1.21 -> 21.0.*
            prefix = f"{parts[1]}.{parts[2] if len(parts) > 2 else 0}."
        else:                    # 26.3 -> 26.3.0.*, 26.1.2 -> 26.1.2.*
            prefix = ".".join((parts + ["0"])[:3]) + "."
        cands = [x for x in allv if x.startswith(prefix) and re.fullmatch(r"[\d.]+(-beta)?", x)]
        if not cands:
            continue
        stable = [x for x in cands if not x.endswith("-beta")]
        nv = max(stable or cands, key=lambda x: vkey(x.replace("-beta", "")))
        url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{nv}/neoforge-{nv}-installer.jar"
        if not url_check(url)[0]:
            log(f"  skip NeoForge {v}: installer not reachable")
            continue
        out += _installer_entries(ctx, "mc-neoforge", v, url, f"neoforge-{nv}-installer.jar")
    return out


GENERATORS = {
    "mc-paper": gen_paper,
    "mc-purpur": gen_purpur,
    "mc-forge": gen_forge,
    "mc-neoforge": gen_neoforge,
    "mc-fabric": gen_fabric,
    "mc-vanilla": gen_vanilla,
    "mc-folia": gen_folia,
}


class Ctx:
    pass


# ----------------------------------------------------------------------------
# Build + validate
# ----------------------------------------------------------------------------
def build(upstream, previous, strict):
    ctx = Ctx()
    ctx.mojang = Mojang()
    ctx.tmpl = Templates(upstream["packages"], (previous or {}).get("packages", []))
    generated = {}
    for cat, fn in GENERATORS.items():
        log(f"generating {cat} ...")
        try:
            entries = fn(ctx)
            if not entries:
                raise RuntimeError("no entries produced")
            generated[cat] = entries
            log(f"  {len(entries)} entries")
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            log(f"WARNING: {cat} failed ({e}); keeping previous entries")
            prev = [p for p in (previous or {}).get("packages", []) if p.get("category") == cat]
            if prev:
                generated[cat] = prev

    pkgs, done = [], set()
    for p in upstream["packages"]:
        c = p.get("category")
        if c in generated:
            if c not in done:
                pkgs += generated[c]
                done.add(c)
            continue
        pkgs.append(p)
    for c, entries in generated.items():  # loaders upstream does not list at all
        if c not in done:
            pkgs += entries
    doc = {k: v for k, v in upstream.items() if k != "packages"}
    doc["packages"] = pkgs
    return doc, set(generated)


REQUIRED = {"platform", "language", "gameType", "image", "description", "title", "category", "runtime",
            "hardware", "size", "remark", "targetLink", "author", "dockerOptional", "setupInfo"}
SETUP_REQUIRED = {"type", "startCommand", "stopCommand", "updateCommand", "ie", "oe"}


def validate(doc, gen_categories):
    errs = []
    if not isinstance(doc.get("packages"), list) or not doc["packages"]:
        errs.append("no packages")
        return errs
    if not doc.get("languages"):
        errs.append("languages missing")
    seen, ids, urls = set(), set(), set()
    for p in doc["packages"]:
        k = (p.get("title"), p.get("platform"), p.get("language"))
        if k in seen:
            errs.append(f"duplicate entry {k}")
        seen.add(k)
        # The panel identifies the package to install by title + description.
        i = (p.get("title"), p.get("description"))
        if i in ids:
            errs.append(f"duplicate title+description {i}")
        ids.add(i)
        if p.get("category") not in gen_categories:
            continue
        if not REQUIRED <= set(p):
            errs.append(f"{p.get('title')}: missing keys {sorted(REQUIRED - set(p))}")
            continue
        if not SETUP_REQUIRED <= set(p["setupInfo"]):
            errs.append(f"{p['title']}: setupInfo keys missing")
        if not re.fullmatch(r"Java \d+\+", p["runtime"]):
            errs.append(f"{p['title']}: bad runtime {p['runtime']}")
        urls.add(p["targetLink"])
    log(f"checking {len(urls)} download URLs ...")
    ordered = sorted(urls)
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for url, (ok, _) in zip(ordered, ex.map(url_check, ordered)):
            if not ok:
                errs.append(f"unreachable: {url}")
    return errs


def render(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.json"))
    ap.add_argument("--upstream", default=UPSTREAM_URL)
    ap.add_argument("--check", action="store_true", help="only validate the existing output file")
    ap.add_argument("--strict", action="store_true", help="fail if any loader cannot be generated")
    a = ap.parse_args()

    previous = None
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            previous = json.load(f)

    if a.check:
        errs = validate(previous, set(GENERATORS))
        for e in errs:
            log(f"ERROR: {e}")
        return 1 if errs else 0

    upstream = get_json(a.upstream)
    if not upstream.get("packages"):
        log("ERROR: upstream market has no packages")
        return 1
    doc, cats = build(upstream, previous, a.strict)
    errs = validate(doc, cats)
    if errs:
        for e in errs:
            log(f"ERROR: {e}")
        log("validation failed; nothing written")
        return 1
    new = render(doc)
    old = None
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            old = f.read()
    if new == old:
        log("market.json unchanged")
        return 0
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    os.replace(tmp, a.out)
    log(f"market.json written ({len(doc['packages'])} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
