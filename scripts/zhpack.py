#!/usr/bin/env python3
"""Android Studio Chinese localization toolkit.

Android Studio resolves UI strings through three unrelated mechanisms, so
``build-all`` writes two files:

* the plugin jar - a language pack (``META-INF/plugin.xml`` declares
  ``<languageBundle locale="zh-CN"/>`` plus ``messages/*.properties``, consulted
  by ``com.intellij.DynamicBundle``) that also relabels actions by id at startup
  (``applicationInitializedListener``), because AS inlines most menu texts in
  ``META-INF/*.xml`` or hardcodes them in AnAction constructors.

* the native resource jar - Android Studio's own bundles read
  ``ResourceBundle.getBundle(name, Locale.getDefault(), pluginClassLoader)``
  (``MessageBundleReference``), which never consults a language pack; the same
  translations shipped as ``messages/*_zh_CN.properties`` on the boot class path
  (``-Xbootclasspath/a:``) are visible to every plugin class loader.  Needs
  ``-Duser.language/-Duser.country`` so ``Locale.getDefault()`` is zh, and must
  live outside the plugins directory: a jar both installed as a plugin and
  referenced from ``-Xbootclasspath/a:`` is loaded as a bundled plugin without a
  class loader (``jarFiles is not set``), which silently kills the language pack.

Commands:
  fetch            locate or download the upstream (JetBrains) language pack
  audit            translation coverage report for an Android Studio install
  audit-actions    list action ids and their inline English texts
  locate           find where a UI string lives (bundle / plugin.xml / class)
  build-all        build the plugin jar and the native resource jar
  install-pack     copy the plugin jar into the IDE plugins directory
  install-native   copy the resource jar outside it and update the vmoptions
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import urllib.request
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import props  # noqa: E402

PACK_ENTRY = "plugins/localization-zh/lib/localization-zh.jar"
MARK_BEGIN = "# >>> AndroidStudioChinese native overlay >>>"
MARK_END = "# <<< AndroidStudioChinese native overlay <<<"
MARKETPLACE_PLUGIN = 13710
LOCALE_SUFFIX = re.compile(r"_[a-z]{2,3}(?:_[A-Z]{2})?$")
UA = {"User-Agent": "AndroidStudioChinese/1.0"}


# --------------------------------------------------------------------------- io


def http(url: str, *, start: int | None = None, end: int | None = None,
         method: str | None = None) -> tuple[bytes, dict[str, str]]:
    headers = dict(UA)
    if start is not None:
        if end is None:
            headers["Range"] = f"bytes={start}" if start < 0 else f"bytes={start}-"
        else:
            headers["Range"] = f"bytes={start}-{end}"
    req = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read(), dict(resp.headers)


def read_zip_text(zf: zipfile.ZipFile, name: str) -> str:
    return zf.read(name).decode("utf-8")


def write_zip(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


# --------------------------------------------------------------- ide scanning


def ide_jars(ide_home: Path):
    seen: set[Path] = set()
    for sub in ("lib", "plugins"):
        base = ide_home / sub
        if not base.is_dir():
            continue
        for jar in sorted(base.rglob("*.jar")):
            real = jar.resolve()
            if real in seen:
                continue
            seen.add(real)
            yield jar


def collect_bundles(ide_home: Path) -> dict[str, dict]:
    """messages/*.properties found in an IDE install -> keys + owning jars."""
    out: dict[str, dict] = {}
    for jar in ide_jars(ide_home):
        try:
            with zipfile.ZipFile(jar) as zf:
                names = zf.namelist()
                for name in names:
                    if not name.startswith("messages/") or not name.endswith(".properties"):
                        continue
                    if LOCALE_SUFFIX.search(Path(name).stem):
                        continue
                    entry = out.setdefault(name, {"jars": [], "keys": {}})
                    entry["jars"].append(jar)
                    try:
                        entry["keys"].update(props.values(read_zip_text(zf, name)))
                    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError):
                        pass
        except (zipfile.BadZipFile, OSError):
            continue
    return out


def overlay(root: Path) -> dict[str, str]:
    return props.read_tree(root)


def combined(name: str, overlay_map: dict[str, str], upstream: zipfile.ZipFile | None) -> str:
    base = ""
    if upstream is not None and name in upstream.namelist():
        base = read_zip_text(upstream, name)
    if name in overlay_map:
        return props.merge(base, overlay_map[name]) if base else overlay_map[name]
    return base


# --------------------------------------------------------------------- fetch


def fetch_from_archive(source: str, entry: str) -> bytes:
    """Pull one entry out of a huge remote (or local) distribution zip."""
    if not source.startswith(("http://", "https://")):
        with zipfile.ZipFile(source) as zf:
            return zf.read(entry)

    tail, headers = http(source, start=-65536, end=None)
    total = int(headers.get("Content-Range", "bytes 0-0/0").split("/")[-1])
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit(f"cannot find zip end-of-central-directory in {source}")
    _, _, _, _, _, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[eocd : eocd + 22])
    if cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        raise SystemExit("zip64 archives are not supported")
    cd, _ = http(source, start=cd_off, end=cd_off + cd_size - 1)

    found = None
    pos = 0
    while pos < len(cd) and cd[pos : pos + 4] == b"PK\x01\x02":
        nlen, elen, clen = struct.unpack("<HHH", cd[pos + 28 : pos + 34])
        method = struct.unpack("<H", cd[pos + 10 : pos + 12])[0]
        comp, uncomp = struct.unpack("<II", cd[pos + 20 : pos + 28])
        name = cd[pos + 46 : pos + 46 + nlen].decode("utf-8")
        if name == entry:
            lho = struct.unpack("<I", cd[pos + 42 : pos + 46])[0]
            found = (method, comp, uncomp, lho, name)
        pos += 46 + nlen + elen + clen
    if not found:
        raise SystemExit(f"{entry} not present in {source}")

    method, comp, uncomp, lho, _ = found
    head, _ = http(source, start=lho, end=lho + 29)
    nlen, elen = struct.unpack("<HH", head[26:30])
    blob, _ = http(source, start=lho + 30 + nlen + elen, end=lho + 30 + nlen + elen + comp - 1)
    data = zlib.decompress(blob, -15) if method == 8 else blob
    if len(data) != uncomp:
        raise SystemExit(f"short read: {len(data)} != {uncomp}")
    return data


def marketplace_latest() -> tuple[str, bytes]:
    raw, _ = http(f"https://plugins.jetbrains.com/api/plugins/{MARKETPLACE_PLUGIN}/updates?size=1")
    update = json.loads(raw)[0]
    version, update_id = update["version"], update["id"]
    blob, _ = http(f"https://plugins.jetbrains.com/plugin/download?updateId={update_id}")
    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
        if "META-INF/plugin.xml" in zf.namelist():
            return version, blob
        jars = sorted((n for n in zf.namelist() if n.endswith(".jar")), key=len)
        if not jars:
            raise SystemExit("unexpected marketplace archive layout")
        return version, zf.read(jars[-1])


def idea_candidates():
    """IDEA release versions to try, newest first (2027.3, 2027.2, ... 2024.1)."""
    from datetime import date

    year = date.today().year
    for candidate_year in range(year + 1, year - 3, -1):
        for minor in (3, 2, 1):
            yield f"{candidate_year}.{minor}"


def idea_zip_url(version: str) -> str:
    return f"https://download.jetbrains.com/idea/idea-{version}.win.zip"


def probe_latest_idea() -> tuple[str, str]:
    """Newest IntelliJ IDEA distribution that exists on the CDN.

    The products/releases API lags behind the CDN (it reports 2025.3 while 2026.x
    zips are served), so probe the version URLs directly, newest first.
    """
    for version in idea_candidates():
        url = idea_zip_url(version)
        try:
            _, headers = http(url, start=0, end=0)
        except Exception:
            continue
        total = int(headers.get("Content-Range", "bytes 0-0/0").split("/")[-1])
        if total > 100_000_000:
            return version, url
    raise SystemExit("no IntelliJ IDEA distribution found; pass --from-archive explicitly")


def normalize_build(raw: str) -> str:
    """``IU-262.8665.258`` -> ``262.8665.258``."""
    return re.sub(r"^[A-Z]+-", "", raw.strip())


def idea_series(version: str) -> str:
    """``2026.2.2.9`` / ``2026.2`` -> ``2026.2`` (Android Studio and IDEA share the series)."""
    parts = version.strip().split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise SystemExit(f"无法从 {version!r} 推断 IDEA 版本，应为 2026.2 或 2026.2.2.9 形式")
    return f"{parts[0]}.{parts[1]}"


def expected_build_prefix(series: str) -> str:
    """``2026.2`` -> ``262`` (last two digits of the year + release cycle)."""
    year, cycle = series.split(".")[:2]
    return f"{year[2:]}{cycle}"


def probe_idea_series(series: str) -> tuple[str, str]:
    """URL and build of the IDEA distribution for a series such as ``2026.2``."""
    url = idea_zip_url(series)
    try:
        build = normalize_build(fetch_from_archive(url, "build.txt").decode("utf-8", "replace"))
    except (Exception, SystemExit):
        raise SystemExit(f"{series} 对应的 IDEA 分发不存在：{url}")
    return url, build


def probe_idea_for_build(prefix: str) -> tuple[str, str, str]:
    """Newest IDEA distribution whose build matches the platform prefix (e.g. 262).

    build.txt is a few bytes at the zip root, so each candidate costs one range read.
    """
    prefix = str(prefix)
    for version in idea_candidates():
        url = idea_zip_url(version)
        try:
            build = normalize_build(fetch_from_archive(url, "build.txt").decode("utf-8", "replace"))
        except (Exception, SystemExit):
            continue
        if build == prefix or build.startswith(prefix + "."):
            return version, url, build
    raise SystemExit(f"no IntelliJ IDEA distribution with build {prefix}.x found")


def cmd_fetch(args) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    version = None
    if args.from_jar:
        shutil.copyfile(args.from_jar, out)
        source = args.from_jar
    elif args.ide_home:
        candidate = Path(args.ide_home) / PACK_ENTRY
        if not candidate.is_file():
            cands = sorted((Path(args.ide_home) / "plugins").glob("localization-*/lib/*.jar"))
            if not cands:
                raise SystemExit(
                    f"{args.ide_home} bundles no language pack - Android Studio never did. "
                    f"Use --from-archive with an IntelliJ IDEA distribution instead."
                )
            candidate = cands[0]
        shutil.copyfile(candidate, out)
        source = str(candidate)
    elif args.for_version:
        series = idea_series(args.for_version)
        url, build = probe_idea_series(series)
        expected = expected_build_prefix(series)
        print(f"IDEA {series} (build {build})  <- AS {args.for_version}")
        if not build.startswith(expected + "."):
            print(f"warning : IDEA 构建号 {build} 与按系列推算的 {expected} 不一致")
        out.write_bytes(fetch_from_archive(url, args.entry))
        source = f"{url}!{args.entry}"
    elif args.latest or args.latest_for:
        if args.latest_for:
            idea_version, url, matched = probe_idea_for_build(args.latest_for)
            print(f"IntelliJ IDEA {idea_version} (build {matched})")
        else:
            idea_version, url = probe_latest_idea()
            print(f"latest IntelliJ IDEA release: {idea_version}")
        out.write_bytes(fetch_from_archive(url, args.entry))
        source = f"{url}!{args.entry}"
    elif args.from_archive:
        out.write_bytes(fetch_from_archive(args.from_archive, args.entry))
        source = f"{args.from_archive}!{args.entry}"
    elif args.marketplace:
        version, blob = marketplace_latest()
        out.write_bytes(blob)
        source = "marketplace (legacy: JetBrains stopped publishing after 242.152)"
    else:
        raise SystemExit("pick one of --for-version / --latest / --latest-for / --from-jar / "
                         "--ide-home / --from-archive / --marketplace")

    with zipfile.ZipFile(out) as zf:
        xml = read_zip_text(zf, "META-INF/plugin.xml")
        m = re.search(r"<version>(.*?)</version>", xml)
        version = version or (m.group(1) if m else "?")
        messages = sum(1 for n in zf.namelist() if n.startswith("messages/"))
    print(f"upstream: {source}")
    print(f"written : {out}  (version {version}, {messages} message bundles)")
    if args.expect_build and not str(version).startswith(args.expect_build):
        print(f"warning : 语言包版本 {version} 与目标平台 {args.expect_build} 不一致，"
              f"新增/改名的键可能对不上，建议改用同平台的 IDEA 分发")
    return 0


# --------------------------------------------------------------------- audit


def cmd_audit(args) -> int:
    ide_home = Path(args.ide_home)
    bundles = collect_bundles(ide_home)
    up = zipfile.ZipFile(args.pack) if args.pack else None
    ov = overlay(Path(args.translations))

    rows = []
    for name, info in sorted(bundles.items()):
        en = info["keys"]
        zh: dict[str, str] = {}
        if up is not None and name in up.namelist():
            zh.update(props.values(read_zip_text(up, name)))
        if name in ov:
            zh.update(props.values(ov[name]))
        missing = sorted(k for k in en if k not in zh)
        covered = len(en) - len(missing)
        scope = "plugin" if any("plugins" in str(j) for j in info["jars"]) else "platform"
        rows.append((name, scope, len(en), covered, missing))

    total_en = sum(r[2] for r in rows)
    total_zh = sum(r[3] for r in rows)
    total_miss = sum(len(r[4]) for r in rows)
    print(f"IDE        : {ide_home}")
    print(f"upstream   : {args.pack or '(none)'}")
    print(f"overlay    : {args.translations}")
    print(f"bundles    : {len(rows)}   keys: {total_en}   covered: {total_zh}"
          f"   missing: {total_miss}  ({(total_zh / total_en * 100 if total_en else 0):.1f}%)\n")
    print(f"{'bundle':52} {'scope':9} {'en':>6} {'zh':>6} {'missing':>8}")
    for name, scope, en, zh, missing in sorted(rows, key=lambda r: -len(r[4]))[:args.top]:
        print(f"{name:52} {scope:9} {en:6} {zh:6} {len(missing):8}")

    if args.show_missing:
        print()
        for name, _, _, _, missing in sorted(rows, key=lambda r: -len(r[4])):
            if not missing:
                continue
            print(f"### {name}")
            for key in missing:
                print(f"    {key}")

    if args.json:
        report = Path(args.json)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                [
                    {"bundle": n, "scope": s, "keys": e, "translated": z, "missing": m}
                    for n, s, e, z, m in rows
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\njson report -> {args.json}")

    if args.stubs:
        stub_root = Path(args.stubs)
        written = 0
        for name, _, _, _, missing in rows:
            if not missing:
                continue
            en = bundles[name]["keys"]
            lines = [
                f"# {name} - {len(missing)} untranslated key(s)",
                "# translate the values below and keep the same file name under translations/",
                "",
            ]
            for key in missing:
                lines.append(f"{key}={en[key]}")
            path = stub_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written += 1
        print(f"stubs     -> {stub_root} ({written} files)")
    return 0


# ---------------------------------------------------------------- build pack


def patch_plugin_xml(xml: str, args) -> str:
    if args.id:
        xml = re.sub(r"<id>.*?</id>", f"<id>{args.id}</id>", xml, count=1)
    if getattr(args, "vendor", None):
        xml = re.sub(r"<vendor\b[^>]*>.*?</vendor>", f"<vendor>{args.vendor}</vendor>",
                     xml, count=1, flags=re.S)
    if args.version:
        xml = re.sub(r"<version>.*?</version>", f"<version>{args.version}</version>", xml, count=1)
    if args.since or args.until:
        def fix(tag: re.Match) -> str:
            text = tag.group(0)
            if args.since:
                text = re.sub(r'since-build="[^"]*"', f'since-build="{args.since}"', text)
            if args.until:
                text = re.sub(r'until-build="[^"]*"', f'until-build="{args.until}"', text)
            return text

        xml = re.sub(r"<idea-version\b[^>]*/>", fix, xml, count=1)
    return xml


# ---------------------------------------------------------- install / helpers


def plugins_dir(ide_home: Path) -> Path:
    info = json.loads((ide_home / "product-info.json").read_text(encoding="utf-8"))
    vendor = info.get("productVendor") or "JetBrains"
    data = info.get("dataDirectoryName") or ide_home.name
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return base / vendor / data


def plugin_id_of(jar: Path) -> str | None:
    try:
        with zipfile.ZipFile(jar) as zf:
            xml = zf.read("META-INF/plugin.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    match = re.search(r"<id>(.*?)</id>", xml)
    return match.group(1) if match else None


def cmd_install_pack(args) -> int:
    """Copy the language pack straight into the plugins directory.

    The GUI installer updates in place (unload -> delete old jar -> copy new
    one); if that sequence dies halfway a retry fails with
    FileAlreadyExistsException on the destination name.  Doing the copy here is
    deterministic and idempotent.
    """
    jar = Path(args.jar).resolve()
    plugin_id = plugin_id_of(jar)
    if plugin_id is None:
        raise SystemExit(f"{jar}: no META-INF/plugin.xml with <id>")
    target = Path(args.plugins_dir) if args.plugins_dir else plugins_dir(Path(args.ide_home))
    if not target.is_dir():
        raise SystemExit(f"plugins directory not found: {target}")

    same_id = [p for p in sorted(target.glob("*.jar")) if plugin_id_of(p) == plugin_id]
    if args.remove:
        for old in same_id:
            old.unlink()
            print(f"removed {old}")
        return 0
    if args.print_only:
        print(f"plugin id   : {plugin_id}")
        print(f"plugins dir : {target}")
        print(f"would drop  : {[p.name for p in same_id if p != jar]}")
        print(f"would copy  : {jar.name}")
        return 0

    for old in same_id:
        if old != jar:
            old.unlink()
            print(f"replaced {old.name}")
    dest = target / jar.name
    if dest != jar:
        shutil.copyfile(jar, dest)
    print(f"installed {plugin_id} -> {dest}")
    print("restart the IDE for the language pack to load")
    return 0


# -------------------------------------------------------------------- locate


def cmd_locate(args) -> int:
    """Where does a given UI string actually live?"""
    ide_home = Path(args.ide_home)
    up = zipfile.ZipFile(args.pack) if args.pack else None
    ov = overlay(Path(args.translations))
    needles = args.strings
    happy: dict[str, list[str]] = {s: [] for s in needles}

    for jar in ide_jars(ide_home):
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if name.endswith(".properties"):
                        if LOCALE_SUFFIX.search(Path(name).stem):
                            continue
                        try:
                            text = zf.read(name).decode("utf-8", "replace")
                        except (zipfile.BadZipFile, KeyError):
                            continue
                        for key, value in props.values(text).items():
                            if value in happy:
                                zh = None
                                if name in ov:
                                    zh = props.values(ov[name]).get(key)
                                if zh is None and up is not None and name in up.namelist():
                                    zh = props.values(read_zip_text(up, name)).get(key)
                                state = f"BUNDLE zh={zh!r}" if zh else "BUNDLE no-translation"
                                happy[value].append(f"    {state} {jar.name}!{name}  key={key}")
                    elif name.startswith("META-INF/") and name.endswith(".xml"):
                        raw = zf.read(name)
                        for s in needles:
                            if s.encode("utf-8") in raw:
                                snippet = raw.decode("utf-8", "replace")
                                idx = snippet.find(s)
                                happy[s].append(
                                    f"    plugin.xml(inline) {jar.name}!{name}  ...{snippet[max(0, idx - 40):idx + len(s) + 20]}..."
                                )
                    elif name.endswith(".class"):
                        raw = zf.read(name)
                        for s in needles:
                            if s.encode("utf-8") in raw:
                                happy[s].append(f"    HARDCODED {jar.name}!{name}")
        except (zipfile.BadZipFile, OSError):
            continue

    for s in needles:
        hits = happy[s]
        if not hits:
            print(f"{s!r}: not found in this install")
            continue
        kinds = {h.split()[0] for h in hits}
        print(f"{s!r}: {'/'.join(sorted(kinds))}")
        for h in hits[:6]:
            print(h)
        if len(hits) > 6:
            print(f"    ... {len(hits) - 6} more")
    return 0


# ------------------------------------------------------- action text overlay


def extract_actions(ide_home: Path) -> dict[str, tuple[str | None, str]]:
    """action/group id -> (inline text or None, jar name)."""
    out: dict[str, tuple[str | None, str]] = {}
    for jar in ide_jars(ide_home):
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if not (name.startswith("META-INF/") and name.endswith(".xml")):
                        continue
                    text = zf.read(name).decode("utf-8", "replace")
                    for match in re.finditer(r"<(action|group)\b([^>]*)>", text):
                        attrs = match.group(2)
                        id_match = re.search(r'\bid\s*=\s*"([^"]+)"', attrs)
                        if id_match is None:
                            continue
                        text_match = re.search(r'\btext\s*=\s*"([^"]*)"', attrs)
                        out.setdefault(id_match.group(1), (text_match.group(1) if text_match else None, jar.name))
        except (zipfile.BadZipFile, OSError):
            continue
    return out


def cmd_audit_actions(args) -> int:
    actions = extract_actions(Path(args.ide_home))
    known = load_action_translations(Path(args.translations))
    with_text = {k: v for k, v in actions.items() if v[0]}
    print(f"action ids registered: {len(actions)}   with inline text: {len(with_text)}"
          f"   translated: {len(known)}")
    missing = [k for k in with_text if k not in known]
    print(f"untranslated (inline text): {len(missing)}")
    for action_id in sorted(missing)[:args.top]:
        text, jar = with_text[action_id]
        print(f"  {action_id:55} {text[:50]:50} [{jar}]")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {len(with_text)} actions with inline text; id=English"]
        for action_id in sorted(with_text):
            lines.append(f"{action_id}={with_text[action_id][0]}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"worklist -> {out}")
    return 0


def load_action_translations(root: Path) -> dict[str, str]:
    path = root / "actions.properties"
    if not path.is_file():
        return {}
    return {k: v for k, v in props.values(path.read_text(encoding="utf-8")).items()
            if not k.endswith(".description") and v.strip()}


def compile_action_classes(ide: Path, source: Path | None, classes: Path) -> dict[str, bytes]:
    import subprocess

    root = Path(__file__).resolve().parent.parent
    source = source or root / "plugin/src/aszh/ActionTextOverlay.java"
    classes = classes or root / "build/classes"
    shutil.rmtree(classes, ignore_errors=True)
    classes.mkdir(parents=True, exist_ok=True)

    javac = ide / "jbr/bin/javac"
    if not javac.is_file():
        raise SystemExit(f"no javac at {javac}")
    classpath = os.pathsep.join(str(p) for p in sorted((ide / "lib").glob("*.jar")))
    subprocess.run([str(javac), "-encoding", "UTF-8", "-nowarn", "-d", str(classes),
                    "-cp", classpath, str(source)], check=True)
    return {c.relative_to(classes).as_posix(): c.read_bytes() for c in sorted(classes.rglob("*.class"))}


def action_data_block(translations: dict[str, str], origin: str) -> bytes:
    lines = [f"# generated by scripts/zhpack.py {origin}"]
    lines += [f"{k}={v}" for k, v in sorted(translations.items())]
    return ("\n".join(lines) + "\n").encode("utf-8")


def cmd_build_all(args) -> int:
    """Plugin jar (language pack + action relabeling) and the boot class path resource jar.

    The two must be different files: a jar that sits in the plugins directory *and* is
    referenced from -Xbootclasspath/a: is classified as a bundled plugin and gets no
    class loader ("jarFiles is not set"), which silently disables the language pack.
    """
    ide = Path(args.ide_home)
    upstream = zipfile.ZipFile(args.upstream)
    ov = overlay(Path(args.translations))
    entries: dict[str, bytes] = {
        info.filename: upstream.read(info.filename)
        for info in upstream.infolist() if info.filename != "__index__"
    }

    for name, text in ov.items():
        if name.endswith(".properties") and name in entries:
            entries[name] = props.merge(entries[name].decode("utf-8"), text).encode("utf-8")
        else:
            entries[name] = text.encode("utf-8")

    translations = load_action_translations(Path(args.translations))
    entries["actions.properties"] = action_data_block(translations, "build-all")
    entries.update(compile_action_classes(ide, Path(args.source) if args.source else None,
                                          Path(args.classes) if args.classes else Path("build/classes")))

    for name, target in (("LICENSE", "META-INF/LICENSE"), ("NOTICE", "META-INF/NOTICE")):
        source = Path(__file__).resolve().parent.parent / name
        if source.is_file():
            entries[target] = source.read_bytes()
        else:
            print(f"warning: {name} not found, the plugin jar will not carry it")

    xml = patch_plugin_xml(entries["META-INF/plugin.xml"].decode("utf-8"), args)
    if "<applicationInitializedListener" not in xml:
        xml = xml.replace("</extensions>",
                          '    <applicationInitializedListener implementation="aszh.ActionTextOverlay"/>\n  </extensions>', 1)
    entries["META-INF/plugin.xml"] = xml.encode("utf-8")

    out = Path(args.out)
    write_zip(out, entries)

    native_entries: dict[str, bytes] = {}
    bundles = collect_bundles(ide)
    for name in sorted(bundles):
        if not any("/plugins/" in str(j) for j in bundles[name]["jars"]):
            continue
        content = combined(name, ov, upstream)
        if content.strip():
            native_entries[f"{name[:-len('.properties')]}_{args.locale}.properties"] = content.encode("utf-8")
    if not native_entries:
        raise SystemExit("no native bundles found - check --ide-home")

    native_out = Path(args.native_out) if args.native_out else out.with_name(f"zh-native-{out.stem.split('-')[-1]}.jar")
    write_zip(native_out, native_entries)

    version = re.search(r"<version>(.*?)</version>", xml).group(1)
    print(f"plugin jar -> {out}")
    print(f"  id/version : {re.search(r'<id>(.*?)</id>', xml).group(1)} / {version}")
    vendor = re.search(r"<vendor\b[^>]*>(.*?)</vendor>", xml, re.S)
    print(f"  vendor     : {vendor.group(1) if vendor else '(none)'}")
    print(f"  entries    : {len(entries)}  actions: {len(translations)}")
    print(f"native jar -> {native_out}")
    print(f"  entries    : {len(native_entries)}  ({args.locale})")
    print("  install    : install-pack --jar <plugin jar> --ide-home <AS>; "
          "install-native --jar <native jar>")
    return 0


def default_vmoptions() -> Path | None:
    cands = sorted(
        Path.home().glob(".config/Google/AndroidStudio*/studio64.vmoptions"),
        key=lambda p: p.stat().st_mtime,
    )
    return cands[-1] if cands else None


def vm_block(jar: Path, locale: str) -> list[str]:
    lang, _, country = locale.partition("_")
    block = [MARK_BEGIN, f"-Xbootclasspath/a:{jar}", f"-Duser.language={lang}"]
    if country:
        block.append(f"-Duser.country={country}")
    block.append(MARK_END)
    return block


def cmd_install_native(args) -> int:
    jar = Path(args.jar).resolve()
    if not jar.is_file():
        raise SystemExit(f"missing overlay jar: {jar}")
    vm = Path(args.vmoptions).resolve() if args.vmoptions else default_vmoptions()
    if vm is None or not vm.is_file():
        raise SystemExit("cannot find studio64.vmoptions - pass --vmoptions")

    dest_dir = Path(args.dest) if args.dest else Path.home() / ".local/share/android-studio-zh-native"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / jar.name

    text = vm.read_text(encoding="utf-8")
    keep: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            skipping = True
            continue
        if line.strip() == MARK_END:
            skipping = False
            continue
        if not skipping and not line.startswith("-Xbootclasspath/a:") and not line.startswith("-Duser.language=") \
                and not line.startswith("-Duser.country="):
            keep.append(line)

    if args.remove:
        vm.write_text("\n".join(keep).rstrip("\n") + "\n", encoding="utf-8")
        print(f"removed overlay JVM options from {vm}")
        return 0

    if args.print_only:
        print(f"jar     -> {jar}  ({jar.stat().st_size} bytes, would be copied to {dest_dir})")
        for line in vm_block(dest, args.locale):
            print(f"    {line}")
        print(f"vmoptions -> {vm}  (--print-only: nothing written)")
        return 0

    if dest.exists():
        print(f"overwriting existing {dest}")
    shutil.copyfile(jar, dest)

    backup = vm.with_suffix(vm.suffix + ".bak")
    if not backup.exists():
        shutil.copyfile(vm, backup)
        print(f"backup  -> {backup}")
    body = "\n".join(keep).rstrip("\n")
    block = "\n".join(vm_block(dest, args.locale))
    vm.write_text(f"{body}\n\n{block}\n" if body else f"{block}\n", encoding="utf-8")
    print(f"updated -> {vm}")

    print(f"jar     -> {dest}  ({dest.stat().st_size} bytes)")
    for line in vm_block(dest, args.locale):
        print(f"    {line}")
    print("\nrestart Android Studio for the native translations to take effect")
    return 0


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="locate/download the upstream JetBrains language pack")
    f.add_argument("--from-jar", help="copy an existing language pack jar")
    f.add_argument("--ide-home", help="IDE install that bundles plugins/localization-zh")
    f.add_argument("--for-version", metavar="AS_VERSION",
                   help="按 Android Studio 版本推断 IDEA 版本，如 2026.2.2.9 或 2026.2")
    f.add_argument("--latest", action="store_true",
                   help="probe the newest IntelliJ IDEA release and take its language pack")
    f.add_argument("--latest-for", metavar="BUILD",
                   help="newest IDEA whose build matches this prefix, e.g. 262")
    f.add_argument("--expect-build", help="warn if the language pack build differs from this prefix (e.g. 262)")
    f.add_argument("--from-archive", help="IDE distribution zip (local path or URL), range-read")
    f.add_argument("--entry", default=PACK_ENTRY, help=f"entry inside the archive (default {PACK_ENTRY})")
    f.add_argument("--marketplace", action="store_true", help="JetBrains Marketplace (stops at 242.152)")
    f.add_argument("--out", default="upstream/localization-zh.jar")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("audit", help="translation coverage report")
    a.add_argument("--ide-home", required=True)
    a.add_argument("--pack", help="upstream language pack jar")
    a.add_argument("--translations", default="translations")
    a.add_argument("--top", type=int, default=30)
    a.add_argument("--show-missing", action="store_true")
    a.add_argument("--json")
    a.add_argument("--stubs", help="write per-bundle stub files for translators")
    a.set_defaults(func=cmd_audit)

    aa = sub.add_parser("audit-actions", help="list action ids and their inline English texts")
    aa.add_argument("--ide-home", required=True)
    aa.add_argument("--translations", default="translations")
    aa.add_argument("--top", type=int, default=40)
    aa.add_argument("--out")
    aa.set_defaults(func=cmd_audit_actions)

    ba = sub.add_parser("build-all", help="one jar: language pack + native resources + action overlay")
    ba.add_argument("--ide-home", required=True)
    ba.add_argument("--upstream", required=True)
    ba.add_argument("--translations", default="translations")
    ba.add_argument("--locale", default="zh_CN")
    ba.add_argument("--native-out", help="resource jar for -Xbootclasspath/a (default: zh-native-<ver>.jar)")
    ba.add_argument("--source")
    ba.add_argument("--classes")
    ba.add_argument("--id", default="com.intellij.zh")
    ba.add_argument("--vendor", default="AndroidStudioChinese (unofficial build)",
                    help="plugin.xml 的 <vendor>，默认标明非官方构建")
    ba.add_argument("--version")
    ba.add_argument("--since")
    ba.add_argument("--until")
    ba.add_argument("--out", required=True)
    ba.set_defaults(func=cmd_build_all)

    loc = sub.add_parser("locate", help="find where a UI string lives (bundle / plugin.xml / hardcoded)")
    loc.add_argument("--ide-home", required=True)
    loc.add_argument("--pack")
    loc.add_argument("--translations", default="translations")
    loc.add_argument("strings", nargs="+")
    loc.set_defaults(func=cmd_locate)

    p_inst = sub.add_parser("install-pack", help="copy the language pack into the plugins directory")
    p_inst.add_argument("--jar", required=True)
    p_inst.add_argument("--ide-home")
    p_inst.add_argument("--plugins-dir")
    p_inst.add_argument("--remove", action="store_true")
    p_inst.add_argument("--print-only", action="store_true")
    p_inst.set_defaults(func=cmd_install_pack)

    i = sub.add_parser("install-native", help="install the overlay jar and JVM options")
    i.add_argument("--jar", required=True)
    i.add_argument("--vmoptions")
    i.add_argument("--dest")
    i.add_argument("--locale", default="zh_CN")
    i.add_argument("--remove", action="store_true")
    i.add_argument("--print-only", action="store_true")
    i.set_defaults(func=cmd_install_native)

    args = p.parse_args(argv)
    if args.func is cmd_build_all and not (args.since or args.until):
        args.since, args.until = "AI-0", "AI-999.*"
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
