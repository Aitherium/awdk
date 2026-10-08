"""Per-folder copy rules for the family drive: `adk storage drive rule set|show|clear`.

A folder's rule says how its files are kept:

* **redundancy**: ``copies`` 1-3 (whole copies on different devices), or ``erasure k+m``
  (the household cuts each sealed chunk into k data + m parity shards on different devices,
  B10; any k give it back, so m devices can be lost for (k+m)/k the space);
* **devices**: which kinds of device may keep it (``computers``, ``laptops``, ``phones``;
  default any), and ``--no-kids`` keeps it off every child's device;
* **keep-on**: one device that always keeps a copy (copies only).

Files inherit the rule of the nearest folder above them that has one (the whole rule,
not field by field); a rule set on one file wins over its folder's. With no rule at all a
file is kept as before (``put``'s own --ec / --copies / --replicas, default 4+2 shards
falling back to 2 copies).

Rules live INSIDE the drive's sealed index, beside the folder names, so the household
never learns which folder a rule belongs to. It receives only what placement needs,
under an opaque random ``rule_id``: the redundancy, the kinds of device, no-kids, the
pinned device, and which stored objects (content hashes of ciphertext) the rule covers.
Changing a rule re-places what it covers: the household moves copies and shards
(repair); switching between copies and erasure stores each chunk anew from here (opened
and sealed again, so it is a new object), and the old one is removed after the index
points at the new one.

A guardian can also change copies, devices, children's devices and the pinned device
from the Family app; the next command here takes that change into the sealed index.
"""

from __future__ import annotations

import argparse
import json
import secrets
from typing import Any, Dict, List, Mapping, Optional, Tuple

from adk import family_drive_cli as cli
from adk import family_vault as fv

DEFAULT_COPIES = 2
_GROUP_WORDS = {
    "any": [], "all": [],
    "computer": ["computer"], "computers": ["computer"], "desktop": ["computer"],
    "desktops": ["computer"], "server": ["computer"], "servers": ["computer"],
    "laptop": ["laptop"], "laptops": ["laptop"],
    "phone": ["phone"], "phones": ["phone"], "tablet": ["phone"], "tablets": ["phone"],
}
_ALL = {"computer", "laptop", "phone"}


# -- the rule model ---------------------------------------------------------------------

def default_rule(copies: int = DEFAULT_COPIES) -> Dict[str, Any]:
    return {"rule_id": "", "redundancy": {"copies": copies}, "devices": [], "no_kids": False,
            "pin": ""}


def new_rule_id() -> str:
    return secrets.token_hex(8)  # random: never derived from the folder's name


def clean_folder(path: str) -> str:
    """A folder path on the drive; '' (or '/', '.') is the whole drive."""
    p = (path or "").replace("\\", "/").strip().strip("/")
    if p in ("", "."):
        return ""
    if p.startswith("..") or "/../" in f"/{p}/" or len(p) > 512:
        raise fv.VaultError(f"not a drive path: {path!r}")
    return p


def parse_devices(text: str) -> List[str]:
    out: set = set()
    for word in (text or "").replace("+", ",").split(","):
        w = word.strip().lower()
        if not w:
            continue
        if w not in _GROUP_WORDS:
            raise fv.VaultError(f"unknown kind of device {word!r}: use computers, laptops, "
                                "phones or any")
        if not _GROUP_WORDS[w]:
            return []
        out.update(_GROUP_WORDS[w])
    return [] if out == _ALL else sorted(out)


def parse_erasure(text: str) -> List[int]:
    try:
        k, m = (int(x) for x in text.lower().replace("+", " ").replace(":", " ").split())
    except ValueError as exc:
        raise fv.VaultError(f"erasure is k+m, like 4+2 (got {text!r})") from exc
    if not (2 <= k <= 16 and 1 <= m <= 8):
        raise fv.VaultError("erasure wants 2..16 data and 1..8 parity shards")
    return [k, m]


def ancestors(name: str) -> List[str]:
    """Folders above a drive path, nearest first, ending with '' (the whole drive)."""
    parts = name.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)] + [""]


def effective(index: Mapping[str, Any], name: str) -> Tuple[Dict[str, Any], str]:
    """The rule a file follows and where it comes from: 'file', a folder path, or 'default'."""
    entry = (index.get("files") or {}).get(name)
    if entry and entry.get("rule"):
        return dict(entry["rule"]), "file"
    folders = index.get("folders") or {}
    for f in ancestors(name):
        if f in folders:
            return dict(folders[f]), f
    return default_rule(), "default"


def folder_effective(index: Mapping[str, Any], folder: str) -> Tuple[Dict[str, Any], str]:
    """The rule a folder's files follow (its own, or the nearest one above it)."""
    folders = index.get("folders") or {}
    if folder in folders:
        return dict(folders[folder]), folder
    for f in ancestors(folder + "/x")[1:] if folder else []:
        if f in folders:
            return dict(folders[f]), f
    return default_rule(), "default"


def describe(rule: Mapping[str, Any], labels: Optional[Mapping[str, str]] = None) -> str:
    if not rule.get("rule_id"):
        return "the drive's default (4+2 shards for big chunks when enough devices lend, else 2 copies)"
    red = rule.get("redundancy") or {"copies": DEFAULT_COPIES}
    if "erasure" in red:
        k, m = red["erasure"]
        parts = [f"erasure {k}+{m} (any {k} of {k + m} shards)"]
    else:
        n = int(red.get("copies", DEFAULT_COPIES))
        parts = [f"{n} {'copy' if n == 1 else 'copies'}"]
    devs = rule.get("devices") or []
    if devs:
        names = {"computer": "computers", "laptop": "laptops", "phone": "phones and tablets"}
        parts.append(" and ".join(names[d] for d in devs) + " only")
    else:
        parts.append("any device")
    if rule.get("no_kids"):
        parts.append("never on kids' devices")
    if rule.get("pin"):
        pin = rule["pin"]
        parts.append(f"keeps a copy on {(labels or {}).get(pin) or pin}")
    return ", ".join(parts)


# -- what the household receives (never a name) ---------------------------------------

def placement_params(rule: Mapping[str, Any]) -> Dict[str, str]:
    """Query params for one chunk put under ``rule`` (no name: kinds of device, pin, id)."""
    red = rule.get("redundancy") or {"copies": DEFAULT_COPIES}
    params = {"replicas": str(int(red.get("copies", DEFAULT_COPIES))), "tier": "warm"}
    if "erasure" in red:
        k, m = red["erasure"]
        params["ec"] = f"{k}+{m}"  # chunks under 64 KiB keep whole copies (B10)
    if rule.get("devices"):
        params["devices"] = ",".join(rule["devices"])
    if rule.get("no_kids"):
        params["no_kids"] = "true"
    if rule.get("pin") and "erasure" not in red:
        params["pin"] = rule["pin"]
    if rule.get("rule_id"):
        params["rule"] = rule["rule_id"]
    return params


def redundancy_label(rule: Mapping[str, Any]) -> str:
    """The file entry's ``redundancy`` (B10's words): ``copies`` or ``ec k+m``."""
    red = rule.get("redundancy") or {}
    if "erasure" in red:
        k, m = red["erasure"]
        return f"ec {k}+{m}"
    return "copies"


def rule_body(rule: Mapping[str, Any], *, kind: str, assign: List[str]) -> Dict[str, Any]:
    return {"kind": kind, "redundancy": dict(rule["redundancy"]),
            "devices": list(rule.get("devices") or []), "no_kids": bool(rule.get("no_kids")),
            "pin": rule.get("pin") or "", "assign": assign, "source": "adk"}


def server_rules() -> Dict[str, Any]:
    """The household's rules and lending devices ({} parts when it has none yet)."""
    try:
        _s, body = cli.request("GET", "/family/storage/drive/rules")
    except cli.ApiError as exc:
        if exc.status == 404:
            return {"rules": [], "devices": []}
        raise
    return body if isinstance(body, dict) else {"rules": [], "devices": []}


def adopt_server_changes(index: Dict[str, Any], served: Mapping[str, Any]) -> List[str]:
    """Take changes a guardian made in the Family app into the index (newer versions win)."""
    by_id = {r["rule_id"]: r for r in served.get("rules") or []}
    changed: List[str] = []

    def take(rule: Dict[str, Any]) -> None:
        s = by_id.get(rule.get("rule_id") or "")
        if s is None or int(s.get("version", 0)) <= int(rule.get("server_version", 0)):
            return
        rule.update({"redundancy": s["redundancy"], "devices": list(s.get("devices") or []),
                     "no_kids": bool(s.get("no_kids")), "pin": s.get("pin") or "",
                     "server_version": int(s["version"])})
        changed.append(rule["rule_id"])

    for rule in (index.get("folders") or {}).values():
        take(rule)
    for entry in (index.get("files") or {}).values():
        if entry.get("rule"):
            take(entry["rule"])
    return changed


# -- layouts: how a file's sealed chunks are kept -------------------------------------

def entry_objects(entry: Mapping[str, Any]) -> List[str]:
    """Every stored object behind one file (its chunks; B10 keeps shards behind each)."""
    return list(entry.get("chunks") or [])


def fits(entry: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    """Is the file stored the way the rule asks (copies vs which erasure), so the household
    can re-place it without this computer storing it anew?"""
    return str(entry.get("redundancy") or "copies") == redundancy_label(rule)


def store_sealed(sealed_chunks: List[bytes], rule: Mapping[str, Any]) -> Dict[str, Any]:
    """Store sealed chunks under ``rule``; the layout fields of a file entry."""
    from adk import family_drive_files as files

    params = placement_params(rule)
    red = rule.get("redundancy") or {}
    return {"chunks": [str(files.put_params(s, params)["object_id"]) for s in sealed_chunks],
            "replicas": int(red.get("copies", DEFAULT_COPIES)),
            "redundancy": redundancy_label(rule)}


def resealed_chunks(key: bytes, entry: Mapping[str, Any], *, wait_s: float) -> List[bytes]:
    """The file's chunks opened and sealed again (new nonce, so new objects)."""
    from adk import family_drive_files as files

    return [fv.seal(key, fv.open_sealed(key, files.fetch_object(oid, wait_s=wait_s)))
            for oid in entry_objects(entry)]


# -- applying a rule change -------------------------------------------------------------

def covered(index: Mapping[str, Any], rule_id: str) -> List[str]:
    """Files whose effective rule is ``rule_id``."""
    return [n for n in (index.get("files") or {}) if effective(index, n)[0].get("rule_id") == rule_id]


def _relayout(key: bytes, names: List[str], index: Dict[str, Any], *, wait_s: float
              ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Store anew the files whose layout no longer matches their rule; (new layouts, old objects)."""
    new: Dict[str, Dict[str, Any]] = {}
    old: List[str] = []
    for name in names:
        entry = index["files"][name]
        rule, _src = effective(index, name)
        if fits(entry, rule):
            continue
        layout = store_sealed(resealed_chunks(key, entry, wait_s=wait_s), rule)
        new[name] = layout
        old += entry_objects(entry)
    return new, old


def apply_rule(key: bytes, rule_id: str, kind: str, change: Any, *, wait_s: float,
               also: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Commit an index change touching rule ``rule_id``, then re-place what it covers.

    ``change(index)`` edits the folders / overrides; files that switch between copies and
    erasure are re-encoded here; finally the household tags every covered object with the
    rule (and re-places them). ``also`` are other rules whose coverage may have grown.
    """
    from adk import family_drive_files as files

    index = files.update_index(key, change, wait_s=wait_s)
    touched = [rule_id, *also]
    names = sorted({n for r in touched if r for n in covered(index, r)})
    layouts, old = _relayout(key, names, index, wait_s=wait_s)
    if layouts:
        def swap(ix: Dict[str, Any]) -> None:
            for name, layout in layouts.items():
                e = ix["files"].get(name)
                if e is None:
                    continue
                if e.get("chunks") != index["files"][name].get("chunks"):
                    continue  # the file changed meanwhile: its new version already has a rule
                e.update(layout)
                e["rule_id"] = effective(ix, name)[0].get("rule_id", "")
        index = files.update_index(key, swap, wait_s=wait_s)
    else:
        def tag(ix: Dict[str, Any]) -> None:
            for name in names:
                if name in ix["files"]:
                    ix["files"][name]["rule_id"] = effective(ix, name)[0].get("rule_id", "")
        if names:
            index = files.update_index(key, tag, wait_s=wait_s)
    for oid in old:
        try:
            cli.request("DELETE", f"/family/storage/objects/{oid}")
        except cli.ApiError:
            pass
    report = {"files": len(names), "restored": len(layouts), "rules": {}}
    for rid in touched:
        if not rid:
            continue
        rule, rkind = _find_rule(index, rid)
        if rule is None:
            continue
        objs = [o for n in covered(index, rid) for o in entry_objects(index["files"][n])]
        _s, out = cli.request("PUT", f"/family/storage/drive/rules/{rid}",
                              json_body=rule_body(rule, kind=rkind, assign=objs))
        report["rules"][rid] = {"objects": len(objs), "version": (out or {}).get("version")}
    return report


def _find_rule(index: Mapping[str, Any], rule_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    for rule in (index.get("folders") or {}).values():
        if rule.get("rule_id") == rule_id:
            return rule, "folder"
    for entry in (index.get("files") or {}).values():
        if (entry.get("rule") or {}).get("rule_id") == rule_id:
            return entry["rule"], "file"
    return None, ""


# -- commands ----------------------------------------------------------------------------

def _labels(served: Mapping[str, Any]) -> Dict[str, str]:
    return {d["device_id"]: d.get("label") or d["device_id"] for d in served.get("devices") or []}


def _resolve_device(text: str, served: Mapping[str, Any]) -> str:
    devices = served.get("devices") or []
    hits = [d for d in devices if d["device_id"] == text] or [
        d for d in devices if (d.get("label") or "").lower() == text.lower()]
    if len(hits) != 1:
        names = ", ".join(sorted(d.get("label") or d["device_id"] for d in devices)) or "none yet"
        raise fv.VaultError(f"no single lending device called {text!r} (lending: {names})")
    return str(hits[0]["device_id"])


def _new_rule(base: Mapping[str, Any], args: argparse.Namespace,
              served: Mapping[str, Any]) -> Dict[str, Any]:
    rule = {k: base.get(k) for k in ("rule_id", "redundancy", "devices", "no_kids", "pin",
                                     "server_version")}
    rule["redundancy"] = dict(base.get("redundancy") or {"copies": DEFAULT_COPIES})
    rule["devices"] = list(base.get("devices") or [])
    rule["no_kids"] = bool(base.get("no_kids"))
    rule["pin"] = base.get("pin") or ""
    if args.copies is not None and args.erasure:
        raise fv.VaultError("choose --copies or --erasure, not both")
    if args.copies is not None:
        rule["redundancy"] = {"copies": args.copies}
    if args.erasure:
        rule["redundancy"] = {"erasure": parse_erasure(args.erasure)}
    if args.devices is not None:
        rule["devices"] = parse_devices(args.devices)
    if args.no_kids:
        rule["no_kids"] = True
    if args.kids_ok:
        rule["no_kids"] = False
    if args.keep_on:
        rule["pin"] = _resolve_device(args.keep_on, served)
    if args.no_keep_on:
        rule["pin"] = ""
    if rule["pin"] and "erasure" in rule["redundancy"]:
        raise fv.VaultError("--keep-on keeps whole copies; it does not go with --erasure")
    if args.erasure and not args.keep_on:
        rule["pin"] = ""  # a kept-on device is for whole copies
    return rule


def cmd_rule_set(args: argparse.Namespace) -> int:
    from adk import family_drive_files as files

    key = cli.family_key()
    served = server_rules()
    _oid, index = files.read_index(key, wait_s=args.wait)
    adopt_server_changes(index, served)
    path = clean_folder(args.path)
    as_file = args.file or (not args.folder and path in index["files"])
    if as_file:
        if path not in index["files"]:
            raise fv.VaultError(f"{path} is not on the family drive")
        own = index["files"][path].get("rule")
        base = own or effective(index, path)[0]
    else:
        own = (index.get("folders") or {}).get(path)
        base = own or folder_effective(index, path)[0]
    rule = _new_rule(base, args, served)
    rule["rule_id"] = (own or {}).get("rule_id") or new_rule_id()
    kind = "file" if as_file else "folder"
    # the household checks the rule (and the pinned device) before the index changes
    _s, out = cli.request("PUT", f"/family/storage/drive/rules/{rule['rule_id']}",
                          json_body=rule_body(rule, kind=kind, assign=[]))
    rule["server_version"] = int((out or {}).get("version") or 0)

    def change(ix: Dict[str, Any]) -> None:
        adopt_server_changes(ix, served)
        if as_file:
            if path not in ix["files"]:
                raise fv.VaultError(f"{path} is not on the family drive")
            ix["files"][path]["rule"] = dict(rule)
        else:
            ix.setdefault("folders", {})[path] = dict(rule)

    report = apply_rule(key, rule["rule_id"], kind, change, wait_s=args.wait)
    where = path or "the whole drive"
    print(f"{where}: {describe(rule, _labels(served))}.")
    print(f"  {report['files']} file(s) follow it"
          + (f"; {report['restored']} stored anew" if report["restored"] else "")
          + "; the family's devices are moving copies to match.")
    return 0


def cmd_rule_clear(args: argparse.Namespace) -> int:
    from adk import family_drive_files as files

    key = cli.family_key()
    served = server_rules()
    _oid, index = files.read_index(key, wait_s=args.wait)
    path = clean_folder(args.path)
    as_file = args.file or (not args.folder and path in index["files"]
                            and bool(index["files"][path].get("rule")))
    if as_file:
        old = (index["files"].get(path) or {}).get("rule")
    else:
        old = (index.get("folders") or {}).get(path)
    if not old:
        raise fv.VaultError(f"{path or 'the whole drive'} has no rule of its own")
    was = old["rule_id"]

    def change(ix: Dict[str, Any]) -> None:
        adopt_server_changes(ix, served)
        if as_file:
            if path in ix["files"]:
                ix["files"][path].pop("rule", None)
        else:
            (ix.get("folders") or {}).pop(path, None)
        for name in ix["files"]:  # files that followed it now follow what is above
            if ix["files"][name].get("rule_id") == was:
                ix["files"][name]["rule_id"] = effective(ix, name)[0].get("rule_id", "")

    after = dict(index)
    after["folders"] = {k: v for k, v in (index.get("folders") or {}).items()
                        if as_file or k != path}
    parent = (effective({**index, "files": {**index["files"], path: {
        k: v for k, v in index["files"][path].items() if k != "rule"}}}, path)[0]
        if as_file else folder_effective(after, path)[0])
    if parent.get("rule_id"):
        report = apply_rule(key, parent["rule_id"], "folder", change, wait_s=args.wait)
    else:
        index = files.update_index(key, change, wait_s=args.wait)
        report = {"files": sum(1 for e in index["files"].values() if not e.get("rule_id"))}
    # whatever it still covers is freed (the household drops its limits and pin)
    cli.request("DELETE", f"/family/storage/drive/rules/{was}", params={"release": "true"})
    print(f"{path or 'the whole drive'}: rule cleared; its files now follow "
          f"{describe(parent, _labels(served))}.")
    return 0


def cmd_rule_show(args: argparse.Namespace) -> int:
    from adk import family_drive_files as files

    key = cli.family_key()
    served = server_rules()
    _oid, index = files.read_index(key, wait_s=args.wait)
    adopt_server_changes(index, served)
    labels = _labels(served)
    if not args.path:
        rows = [{"folder": f or "/", "rule": describe(r, labels), "rule_id": r.get("rule_id"),
                 "files": len(covered(index, r.get("rule_id")))}
                for f, r in sorted((index.get("folders") or {}).items())]
        rows += [{"file": n, "rule": describe(e["rule"], labels), "rule_id": e["rule"]["rule_id"]}
                 for n, e in sorted(index["files"].items()) if e.get("rule")]
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print(f"No copy rules: every file keeps {DEFAULT_COPIES} copies on any device.")
        else:
            for r in rows:
                print(f"{r.get('folder') or r.get('file')}: {r['rule']}")
        return 0
    path = clean_folder(args.path)
    if path in index["files"] and not args.folder:
        rule, src = effective(index, path)
        names = [path]
    else:
        rule, src = folder_effective(index, path)
        names = [n for n in index["files"] if n.startswith(path + "/") or not path]
    under = [n for n in names if effective(index, n)[0].get("rule_id") == rule.get("rule_id")]
    overrides = {n: effective(index, n) for n in names if n not in under}
    size = sum(int(index["files"][n].get("size") or 0) for n in under)
    if src == "default":
        origin = "the drive's default"
    elif src == "file":
        origin = "this file's own rule"
    elif src == path:
        origin = "this folder's own rule"
    else:
        origin = f"inherited from {src or 'the whole drive'}"
    out = {"path": path or "/", "rule": rule, "describe": describe(rule, labels), "from": origin,
           "files": len(under), "bytes": size,
           "other_rules": {n: {"describe": describe(r, labels), "from": s or "/"}
                           for n, (r, s) in sorted(overrides.items())}}
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{out['path']}: {out['describe']} ({origin}).")
    print(f"  {len(under)} file(s), {size} bytes follow it.")
    for n, o in out["other_rules"].items():
        print(f"  {n}: {o['describe']} ({'own rule' if o['from'] == n else 'from ' + o['from']})")
    return 0


def add_parsers(sub: Any) -> None:
    rule = sub.add_parser("rule", help="Copy rules per folder (or per file)")
    rs = rule.add_subparsers(dest="rule_verb")
    st = rs.add_parser("set", help="Set how a folder's (or one file's) files are kept")
    st.add_argument("path", help="Folder on the drive ('/' = the whole drive), or one file")
    st.add_argument("--copies", type=int, choices=[1, 2, 3])
    st.add_argument("--erasure", default="", help="k+m, e.g. 4+2: any 4 of 6 shards rebuild it")
    st.add_argument("--devices", default=None,
                    help="computers, laptops, phones (comma list) or any")
    st.add_argument("--no-kids", action="store_true", help="never on a child's device")
    st.add_argument("--kids-ok", action="store_true", help="undo --no-kids")
    st.add_argument("--keep-on", default="", help="a device (name or id) that keeps a copy")
    st.add_argument("--no-keep-on", action="store_true", help="drop the keep-on device")
    st.add_argument("--file", action="store_true", help="the path is one file (override)")
    st.add_argument("--folder", action="store_true", help="the path is a folder")
    st.add_argument("--wait", type=float, default=600)
    st.set_defaults(handler=cmd_rule_set)
    sh = rs.add_parser("show", help="The rule a folder or file follows, and where from")
    sh.add_argument("path", nargs="?", default="")
    sh.add_argument("--folder", action="store_true")
    sh.add_argument("--json", action="store_true")
    sh.add_argument("--wait", type=float, default=600)
    sh.set_defaults(handler=cmd_rule_show)
    cl = rs.add_parser("clear", help="Remove a folder's (or file's) own rule")
    cl.add_argument("path")
    cl.add_argument("--file", action="store_true")
    cl.add_argument("--folder", action="store_true")
    cl.add_argument("--wait", type=float, default=600)
    cl.set_defaults(handler=cmd_rule_clear)
