"""Linux triage from an ext2/3/4 root volume: OS release, host, accounts and the newest files in home folders.

The home-folder walk skips dot-files and dot-folders (the Linux equivalent of AppData) and stops after
WALK_LIMIT entries, so a huge /home can't turn triage into a full index.
"""
from __future__ import annotations

import heapq
import struct

from .ext import Ext, ExtError
from .windows import iso

WALK_LIMIT = 200_000
RECENT_LIMIT = 15
WTMP_TAIL = 4 * 1024 * 1024
UTMP_RECORD = 384


def is_linux(fs: Ext) -> bool:
    try:
        return fs.lookup("/etc") is not None and (
            fs.lookup("/etc/os-release") is not None or fs.lookup("/etc/passwd") is not None
        )
    except (ExtError, struct.error):
        return False


def _text(fs: Ext, path: str, limit: int = 1024 * 1024) -> str:
    try:
        data = fs.read_file(path, limit)
    except (ExtError, struct.error):
        data = None
    return data.decode("utf-8", "replace") if data else ""


def _kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read_os(fs: Ext) -> dict:
    rel = _kv(_text(fs, "/etc/os-release") or _text(fs, "/usr/lib/os-release"))
    info = {
        "name": rel.get("PRETTY_NAME") or rel.get("NAME"),
        "version": rel.get("VERSION") or rel.get("VERSION_ID"),
        "id": rel.get("ID"),
        "codename": rel.get("VERSION_CODENAME"),
    }
    if not info["name"]:
        lsb = _kv(_text(fs, "/etc/lsb-release"))
        info["name"] = lsb.get("DISTRIB_DESCRIPTION")
    if not info["name"]:
        for path, label in (("/etc/redhat-release", ""), ("/etc/debian_version", "Debian ")):
            t = _text(fs, path).strip()
            if t:
                info["name"] = label + t
                break
    info["kernel_versions"] = _kernels(fs)
    return {k: v for k, v in info.items() if v}


def _kernels(fs: Ext) -> list[str]:
    try:
        boot = fs.lookup("/boot")
        names = [n for n, _, _ in fs.list_dir(boot)] if boot is not None and boot.is_dir else []
    except (ExtError, struct.error):
        names = []
    return sorted({n[len("vmlinuz-"):] for n in names if n.startswith("vmlinuz-")})


def read_device(fs: Ext) -> dict:
    dev = {"computer_name": _text(fs, "/etc/hostname").strip().split("\n")[0] or None}
    tz = _text(fs, "/etc/timezone").strip()
    if not tz:
        try:
            link = fs.readlink("/etc/localtime") or ""
        except (ExtError, struct.error):
            link = ""
        tz = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""
    dev["time_zone"] = tz or None
    dev["last_mounted_at"] = fs.last_mounted or None
    dev["last_mounted"] = iso(fs.mount_time) if fs.mount_time else None
    dev["last_written"] = iso(fs.write_time) if fs.write_time else None
    vendor = _text(fs, "/sys/class/dmi/id/sys_vendor").strip()  # rarely present on disk, cheap to try
    if vendor:
        dev["manufacturer"] = vendor
    return {k: v for k, v in dev.items() if v}


def _last_logins(fs: Ext) -> dict[str, str]:
    try:
        node = fs.lookup("/var/log/wtmp")
    except (ExtError, struct.error):
        node = None
    if node is None or not node.is_file or not node.size:
        return {}
    data = fs.read(node, limit=node.size)[-WTMP_TAIL:]
    data = data[len(data) % UTMP_RECORD:]
    last: dict[str, int] = {}
    for off in range(0, len(data) - UTMP_RECORD + 1, UTMP_RECORD):
        rtype = struct.unpack_from("<h", data, off)[0]
        if rtype != 7:  # USER_PROCESS
            continue
        user = data[off + 44:off + 76].split(b"\0")[0].decode("utf-8", "replace")
        sec = struct.unpack_from("<i", data, off + 340)[0]
        if user and sec > last.get(user, 0):
            last[user] = sec
    return {u: iso(t) for u, t in last.items()}


def read_users(fs: Ext) -> list[dict]:
    logins = _last_logins(fs)
    users = []
    for line in _text(fs, "/etc/passwd").splitlines():
        f = line.split(":")
        if len(f) < 7 or not f[2].isdigit():
            continue
        uid = int(f[2])
        if uid != 0 and not 1000 <= uid < 60000:
            continue  # system/service accounts
        users.append({
            "username": f[0], "uid": uid, "full_name": f[4].split(",")[0] or None, "profile": f[5],
            "shell": f[6], "account": "local", "last_logon": logins.get(f[0]),
            "disabled": f[6].endswith(("nologin", "false")),
        })
    return users


def recent_files(fs: Ext, users: list[dict], cancel=None) -> dict:
    homes = [(u["username"], u["profile"]) for u in users if u.get("profile") and u["profile"] != "/"]
    heap: list = []
    budget = [WALK_LIMIT]
    by_user: dict[str, dict] = {}

    def walk(owner: str, path: str, node, depth: int) -> None:
        if depth > 24:
            return
        try:
            entries = list(fs.list_dir(node))
        except (ExtError, struct.error):
            return
        for name, ino, ftype in entries:
            if budget[0] <= 0 or (cancel is not None and cancel.is_set()):
                return
            budget[0] -= 1
            if name.startswith("."):
                continue
            if ftype not in (1, 2):  # regular file / directory (others: links, devices, sockets)
                continue
            try:
                child = fs.inode(ino)
            except (ExtError, struct.error):
                continue
            full = f"{path}/{name}"
            if child.is_dir:
                walk(owner, full, child, depth + 1)
            elif child.is_file:
                item = (child.mtime, full, owner, child.size)
                best = by_user.get(owner)
                if best is None or child.mtime > best["_t"]:
                    by_user[owner] = {"path": full, "modified": iso(child.mtime), "size": child.size, "_t": child.mtime}
                if len(heap) < RECENT_LIMIT:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)

    for owner, home in homes:
        try:
            node = fs.lookup(home)
        except (ExtError, struct.error):
            node = None
        if node is not None and node.is_dir:
            walk(owner, home.rstrip("/"), node, 0)
    recent = [
        {"path": p, "name": p.rsplit("/", 1)[-1], "user": o, "modified": iso(t), "size": s}
        for t, p, o, s in sorted(heap, reverse=True)
    ]
    for v in by_user.values():
        v.pop("_t", None)
    return {"latest": recent[0] if recent else None, "recent": recent, "by_user": by_user,
            "entries_walked": WALK_LIMIT - budget[0], "truncated": budget[0] <= 0}


def analyse(fs: Ext, progress=None, cancel=None) -> dict:
    os_info = read_os(fs)
    device = read_device(fs)
    users = read_users(fs)
    if progress:
        progress(0.0, "walking home folders")
    files = recent_files(fs, users, cancel=cancel)
    for u in users:
        if u["username"] in files["by_user"]:
            u["last_saved"] = files["by_user"][u["username"]]
    notes = []
    if files["truncated"]:
        notes.append({"level": "info", "title": "Home folder walk stopped early",
                      "detail": f"Looked at the first {WALK_LIMIT:,} entries; the newest file may be elsewhere"})
    return {
        "kind": "linux", "os": os_info, "device": device, "users": users,
        "last_saved": files["latest"], "recent_files": files["recent"], "notes": notes,
    }
