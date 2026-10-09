"""Windows triage from an NTFS volume: OS, device, user accounts and the most recently saved user files.

Everything comes from the registry hives (SYSTEM, SOFTWARE, SAM, each user's NTUSER.DAT) plus one
sequential sweep of the MFT for modification times. No file contents beyond the hives are read.
"""
from __future__ import annotations

import heapq
import struct
from datetime import datetime, timezone

from .ntfs import ROOT, Ntfs, NtfsError, filetime
from .regf import Hive, HiveError

CONFIG = "\\Windows\\System32\\config\\"
HIVE_LIMIT = 256 * 1024 * 1024
PER_FOLDER = 3  # most recent files kept per folder during the MFT sweep
RECENT_LIMIT = 15
BUILTIN_SIDS = {"S-1-5-18": "SYSTEM", "S-1-5-19": "LOCAL SERVICE", "S-1-5-20": "NETWORK SERVICE"}
# Files the OS or apps rewrite on their own; they say nothing about what the user last saved.
NOISE_NAMES = {"desktop.ini", "thumbs.db", "ntuser.dat", "ntuser.ini", "ntuser.pol", "usrclass.dat", "iconcache.db"}
NOISE_PREFIXES = ("ntuser.dat", "usrclass.dat", "~$", "~wr")
NOISE_SUFFIXES = (".tmp", ".lock", ".log1", ".log2", ".regtrans-ms", ".blf", ".etl")
SKIP_PROFILES = {"default", "default user", "all users", "defaultapppool"}
ACB_FLAGS = {0x0001: "disabled", 0x0010: "normal", 0x0200: "password doesn't expire", 0x0400: "locked out"}


def iso(epoch) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return None


def _hive(fs: Ntfs, path: str, notes: list) -> Hive | None:
    try:
        data = fs.read_file(path, limit=HIVE_LIMIT)
    except NtfsError as exc:
        notes.append({"level": "info", "title": f"Couldn't read {path.split(chr(92))[-1]}", "detail": str(exc)})
        return None
    if data is None:
        return None
    try:
        return Hive(data)
    except HiveError as exc:
        notes.append({"level": "info", "title": f"{path.split(chr(92))[-1]} is damaged", "detail": str(exc)})
        return None


def _filetime_bytes(raw) -> str | None:
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 8:
        return iso(filetime(struct.unpack_from("<Q", raw)[0]))
    return None


def is_windows(fs: Ntfs) -> bool:
    return fs.lookup(CONFIG + "SYSTEM") is not None


# ---------------------------------------------------------------- SYSTEM / SOFTWARE
def _control_set(system: Hive):
    select = system.key("Select")
    current = select.value("Current", 1) if select else 1
    return system.key(f"ControlSet{current:03d}") or system.key("ControlSet001")


def read_os(software: Hive | None, system: Hive | None) -> dict:
    os_info: dict = {}
    if software:
        cv = software.key("Microsoft\\Windows NT\\CurrentVersion")
        if cv:
            product = cv.value("ProductName") or "Windows"
            build = str(cv.value("CurrentBuildNumber") or cv.value("CurrentBuild") or "")
            # Windows 11 still says "Windows 10" in ProductName; the build number tells them apart.
            if build.isdigit() and int(build) >= 22000 and "Windows 10" in product:
                product = product.replace("Windows 10", "Windows 11")
            ubr = cv.value("UBR")
            install = cv.value("InstallDate")
            install_time = cv.value("InstallTime")
            os_info = {
                "name": product,
                "edition": cv.value("EditionID"),
                "version": cv.value("DisplayVersion") or cv.value("ReleaseId") or cv.value("CSDVersion"),
                "build": f"{build}.{ubr}" if build and ubr is not None else build,
                "kernel_version": cv.value("CurrentVersion"),
                "installed": iso(filetime(install_time)) if isinstance(install_time, int) and install_time
                else iso(install) if isinstance(install, int) and install else None,
                "registered_owner": cv.value("RegisteredOwner"),
                "registered_org": cv.value("RegisteredOrganization"),
                "product_id": cv.value("ProductId"),
                "system_root": cv.value("SystemRoot"),
            }
    if system:
        cs = _control_set(system)
        env = cs.subkey("Control") if cs else None
        env = env and env.subkey("Session Manager")
        env = env and env.subkey("Environment")
        if env is not None:
            os_info["architecture"] = env.value("PROCESSOR_ARCHITECTURE")
    return {k: v for k, v in os_info.items() if v not in (None, "")}


def read_device(system: Hive | None) -> dict:
    dev: dict = {}
    if not system:
        return dev
    cs = _control_set(system)
    if cs is None:
        return dev
    def sub(path: str):
        k = cs
        for p in path.split("\\"):
            k = k.subkey(p) if k else None
        return k

    cn = sub("Control\\ComputerName\\ComputerName")
    dev["computer_name"] = cn.value("ComputerName") if cn else None
    tcp = sub("Services\\Tcpip\\Parameters")
    if tcp:
        dev["domain"] = tcp.value("Domain") or tcp.value("DhcpDomain") or None
        ips = []
        ifaces = tcp.subkey("Interfaces")
        for iface in ifaces.subkeys() if ifaces else []:
            for name in ("IPAddress", "DhcpIPAddress"):
                v = iface.value(name)
                for ip in v if isinstance(v, list) else [v]:
                    if isinstance(ip, str) and ip and ip != "0.0.0.0" and ip not in ips:
                        ips.append(ip)
        dev["ip_addresses"] = ips
    tz = sub("Control\\TimeZoneInformation")
    if tz:
        dev["time_zone"] = tz.value("TimeZoneKeyName") or tz.value("StandardName")
        bias = tz.value("ActiveTimeBias")
        if isinstance(bias, int):
            bias = struct.unpack("<i", struct.pack("<I", bias))[0]
            sign = "-" if bias > 0 else "+"  # bias is minutes to *add* to local time to get UTC
            dev["utc_offset"] = f"UTC{sign}{abs(bias) // 60:02d}:{abs(bias) % 60:02d}"
    win = sub("Control\\Windows")
    if win:
        dev["last_shutdown"] = _filetime_bytes(win.value("ShutdownTime"))
    sysinfo = sub("Control\\SystemInformation")
    hw = system.key("HardwareConfig")
    hw_cur = None
    if hw:
        last = hw.value("LastConfig")
        hw_cur = hw.subkey(last) if isinstance(last, str) else None
    for src in (sysinfo, hw_cur):
        if src is None:
            continue
        dev.setdefault("manufacturer", src.value("SystemManufacturer"))
        dev.setdefault("model", src.value("SystemProductName"))
        dev.setdefault("bios", src.value("BIOSVersion"))
    return {k: v for k, v in dev.items() if v not in (None, "", [])}


# ---------------------------------------------------------------- users
def _sam_users(sam: Hive | None) -> dict[int, dict]:
    users: dict[int, dict] = {}
    if not sam:
        return users
    base = sam.key("SAM\\Domains\\Account\\Users")
    if base is None:
        return users
    names = base.subkey("Names")
    for k in names.subkeys() if names else []:
        rid = next((vtype for vname, vtype, _ in k.values() if vname == ""), None)
        if rid is not None:
            users[rid] = {"username": k.name, "rid": rid}
    for k in base.subkeys():
        try:
            rid = int(k.name, 16)
        except ValueError:
            continue
        u = users.setdefault(rid, {"rid": rid})
        f = k.value("F")
        if isinstance(f, bytes) and len(f) >= 68:
            last_logon, pwd_set, _, last_fail = struct.unpack_from("<QQQQ", f, 8)
            acb, = struct.unpack_from("<H", f, 56)
            fails, logons = struct.unpack_from("<HH", f, 64)
            u.update({
                "last_logon": iso(filetime(last_logon)), "password_set": iso(filetime(pwd_set)),
                "last_failed_logon": iso(filetime(last_fail)), "logon_count": logons,
                "failed_logons": fails, "disabled": bool(acb & 0x0001),
            })
        v = k.value("V")
        if isinstance(v, bytes) and len(v) >= 0xCC:
            def field(i: int) -> str:
                off, length = struct.unpack_from("<II", v, 12 * i)
                return v[0xCC + off:0xCC + off + length].decode("utf-16-le", "replace")

            u.setdefault("username", field(1))
            if field(2):
                u["full_name"] = field(2)
            if field(3):
                u["comment"] = field(3)
    return users


def volume_path(path: str) -> str:
    """C:\\Users\\bob or %SystemDrive%\\Users\\bob -> \\Users\\bob (a path inside the volume)."""
    if ":" in path:
        path = path.split(":", 1)[1]
    elif path.startswith("%") and path.count("%") >= 2:
        path = path.split("%", 2)[2]
    return path.rstrip("\\")


def _recent_doc(fs: Ntfs, profile: str) -> dict | None:
    """The newest entry in the user's RecentDocs MRU (files they opened or saved through Explorer)."""
    try:
        data = fs.read_file(volume_path(profile) + "\\NTUSER.DAT", limit=HIVE_LIMIT)
    except NtfsError:
        return None
    if not data:
        return None
    try:
        hive = Hive(data)
    except HiveError:
        return None
    rd = hive.key("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\RecentDocs")
    if rd is None:
        return None
    order = rd.value("MRUListEx")
    if not isinstance(order, bytes) or len(order) < 4:
        return None
    first = struct.unpack_from("<I", order)[0]
    if first == 0xFFFFFFFF:
        return None
    entry = rd.value(str(first))
    if not isinstance(entry, bytes):
        return None
    name = entry.decode("utf-16-le", "replace").split("\0")[0]
    return {"name": name, "when": iso(rd.last_written)} if name else None


def read_users(fs: Ntfs, sam: Hive | None, software: Hive | None, notes: list) -> list[dict]:
    sam_users = _sam_users(sam)
    users = []
    seen_rids = set()
    profiles = software.key("Microsoft\\Windows NT\\CurrentVersion\\ProfileList") if software else None
    for p in profiles.subkeys() if profiles else []:
        sid = p.name
        if sid in BUILTIN_SIDS or not sid.startswith("S-1-"):
            continue
        path = p.value("ProfileImagePath") or ""
        folder = path.rstrip("\\").split("\\")[-1]
        rid = int(sid.rsplit("-", 1)[-1]) if sid.rsplit("-", 1)[-1].isdigit() else None
        u = {"sid": sid, "profile": path, "account": "local" if rid in sam_users else _account_type(sid)}
        if rid in sam_users and sid.startswith("S-1-5-21-"):
            u.update(sam_users[rid])
            seen_rids.add(rid)
        u.setdefault("username", folder)
        hi, lo = p.value("LocalProfileLoadTimeHigh"), p.value("LocalProfileLoadTimeLow")
        if isinstance(hi, int) and isinstance(lo, int) and (hi or lo):
            u["profile_loaded"] = iso(filetime(hi << 32 | lo))
        if path:
            recent = _recent_doc(fs, path)
            if recent:
                u["recent_doc"] = recent
        users.append(u)
    for rid, u in sorted(sam_users.items()):
        if rid not in seen_rids:
            users.append({**u, "account": "local", "profile": None})
    return users


def _account_type(sid: str) -> str:
    if sid.startswith("S-1-12-1-"):
        return "Microsoft Entra ID"
    if sid.startswith("S-1-5-21-"):
        return "domain"
    return "other"


# ---------------------------------------------------------------- last saved file
def _is_noise(name: str) -> bool:
    low = name.lower()
    return low in NOISE_NAMES or low.startswith(NOISE_PREFIXES) or low.endswith(NOISE_SUFFIXES)


def recent_files(fs: Ntfs, profile_roots: dict[str, str], progress=None, cancel=None) -> dict:
    """One pass over the MFT. Keeps only folder names (to rebuild paths) and the newest few files per
    folder, so memory stays proportional to the number of folders, not files."""
    dirs: dict[int, tuple[int, str]] = {}
    per_folder: dict[int, list] = {}

    def on_record(number, flags, parent, name, modified, size):
        if flags & 2:
            dirs[number] = (parent, name)
            return
        if number < 24 or not modified or _is_noise(name):
            return
        heap = per_folder.get(parent)
        item = (modified, number, name, size)
        if heap is None:
            per_folder[parent] = [item]
        elif len(heap) < PER_FOLDER:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)

    fs.sweep(on_record, progress=progress, cancel=cancel)

    paths: dict[int, str | None] = {ROOT: ""}

    def path_of(d: int) -> str | None:
        chain = []
        while d not in paths:
            if d not in dirs or len(chain) > 64:
                for c in chain:
                    paths[c] = None
                return None
            chain.append(d)
            d = dirs[d][0]
        base = paths[d]
        for c in reversed(chain):
            base = None if base is None else base + "\\" + dirs[c][1]
            paths[c] = base
        return base

    roots = {k.lower().rstrip("\\"): v for k, v in profile_roots.items()}
    files = []
    for folder, items in per_folder.items():
        p = path_of(folder)
        if p is None:
            continue
        low = p.lower()
        parts = low.split("\\")
        if len(parts) < 3 or parts[1] != "users" or parts[2] in SKIP_PROFILES or "appdata" in parts[3:]:
            continue
        owner = roots.get("\\".join(parts[:3]), p.split("\\")[2])
        for modified, number, name, size in items:
            files.append({
                "path": p + "\\" + name, "name": name, "user": owner, "record": number,
                "modified": iso(filetime(modified)), "_ft": modified, "size": size,
            })
    files.sort(key=lambda f: f["_ft"], reverse=True)
    by_user: dict[str, dict] = {}
    for f in files:
        by_user.setdefault(f["user"], f)
    for f in files:
        f.pop("_ft", None)
    return {"latest": files[0] if files else None, "recent": files[:RECENT_LIMIT], "by_user": by_user,
            "records_scanned": fs.record_count}


def analyse(fs: Ntfs, progress=None, cancel=None) -> dict:
    notes: list = []
    system = _hive(fs, CONFIG + "SYSTEM", notes)
    software = _hive(fs, CONFIG + "SOFTWARE", notes)
    sam = _hive(fs, CONFIG + "SAM", notes)
    os_info = read_os(software, system)
    device = read_device(system)
    users = read_users(fs, sam, software, notes)
    if progress:
        progress(0.0, "sweeping the MFT for recently saved files")
    roots = {}
    for u in users:
        if u.get("profile"):
            roots[volume_path(u["profile"])] = u["username"]
    files = recent_files(fs, roots, progress=lambda f: progress(f, "sweeping the MFT") if progress else None,
                         cancel=cancel)
    for u in users:
        latest = files["by_user"].get(u.get("username"))
        if latest:
            u["last_saved"] = {k: latest[k] for k in ("path", "modified", "size")}
    return {
        "kind": "windows", "os": os_info, "device": device, "users": users,
        "last_saved": files["latest"], "recent_files": files["recent"],
        "records_scanned": files["records_scanned"], "notes": notes,
    }
