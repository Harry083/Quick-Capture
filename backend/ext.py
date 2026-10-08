"""A small read-only ext2/3/4 reader: inodes, extents and block maps, directories and symlinks."""
from __future__ import annotations

import struct
from typing import Iterator

S_IFMT, S_IFDIR, S_IFREG, S_IFLNK = 0xF000, 0x4000, 0x8000, 0xA000
EXTENTS_FL, INLINE_DATA_FL = 0x80000, 0x10000000
MAX_READ = 64 * 1024 * 1024


class ExtError(Exception):
    pass


class Inode:
    __slots__ = ("number", "mode", "size", "atime", "ctime", "mtime", "dtime", "links", "flags", "block", "uid")

    def __init__(self, number: int, raw: bytes) -> None:
        self.number = number
        self.mode, self.uid, size_lo, self.atime, self.ctime, self.mtime, self.dtime = struct.unpack_from("<HHIIIII", raw)
        self.links, = struct.unpack_from("<H", raw, 26)
        self.flags, = struct.unpack_from("<I", raw, 32)
        self.block = raw[40:100]
        size_hi, = struct.unpack_from("<I", raw, 108)
        self.size = size_lo | size_hi << 32

    @property
    def is_dir(self) -> bool:
        return self.mode & S_IFMT == S_IFDIR

    @property
    def is_file(self) -> bool:
        return self.mode & S_IFMT == S_IFREG

    @property
    def is_link(self) -> bool:
        return self.mode & S_IFMT == S_IFLNK


class Ext:
    def __init__(self, volume) -> None:
        self.vol = volume
        sb = volume.read(1024, 1024)
        if len(sb) < 1024 or sb[56:58] != b"\x53\xef":
            raise ExtError("Not an ext2/3/4 volume")
        self.inodes_count, blocks_lo = struct.unpack_from("<II", sb, 0)
        self.first_data_block, log_bs = struct.unpack_from("<II", sb, 20)
        self.block_size = 1024 << log_bs
        self.blocks_per_group, = struct.unpack_from("<I", sb, 32)
        self.inodes_per_group, = struct.unpack_from("<I", sb, 40)
        self.mount_time, self.write_time = struct.unpack_from("<II", sb, 44)
        rev, = struct.unpack_from("<I", sb, 76)
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] if rev >= 1 else 128
        self.incompat, = struct.unpack_from("<I", sb, 96)
        self.label = sb[120:136].split(b"\0")[0].decode("utf-8", "replace")
        self.last_mounted = sb[136:200].split(b"\0")[0].decode("utf-8", "replace")
        is64 = bool(self.incompat & 0x80)
        self.desc_size = (struct.unpack_from("<H", sb, 254)[0] or 32) if is64 else 32
        blocks_hi = struct.unpack_from("<I", sb, 0x150)[0] if is64 else 0
        self.size = (blocks_lo | blocks_hi << 32) * self.block_size
        if not self.inodes_per_group or self.inode_size < 128:
            raise ExtError("Damaged ext superblock")
        self._gdt = (self.first_data_block + 1) * self.block_size
        self._tables: dict[int, int] = {}

    # ---------------------------------------------------------------- inodes
    def _inode_table(self, group: int) -> int:
        t = self._tables.get(group)
        if t is None:
            d = self.vol.read(self._gdt + group * self.desc_size, self.desc_size)
            t = struct.unpack_from("<I", d, 8)[0]
            if self.desc_size >= 64:
                t |= struct.unpack_from("<I", d, 0x28)[0] << 32
            self._tables[group] = t
        return t

    def inode(self, number: int) -> Inode:
        if not 1 <= number <= self.inodes_count:
            raise ExtError(f"Inode {number} out of range")
        group, index = divmod(number - 1, self.inodes_per_group)
        raw = self.vol.read(self._inode_table(group) * self.block_size + index * self.inode_size, 128)
        return Inode(number, raw)

    # ---------------------------------------------------------------- data
    def _extents(self, node: bytes, depth_guard: int = 0) -> Iterator[tuple[int, int, int]]:
        magic, entries, _, depth = struct.unpack_from("<HHHH", node)
        if magic != 0xF30A or depth_guard > 8:
            return
        for i in range(entries):
            e = node[12 + 12 * i:24 + 12 * i]
            if depth == 0:
                lblock, length, hi, lo = struct.unpack("<IHHI", e)
                if length > 32768:  # uninitialised extent: allocated, reads as zeros
                    yield lblock, length - 32768, -1
                else:
                    yield lblock, length, hi << 32 | lo
            else:
                _, lo, hi = struct.unpack_from("<IIH", e)
                child = self.vol.read((hi << 32 | lo) * self.block_size, self.block_size)
                yield from self._extents(child, depth_guard + 1)

    def _block_map(self, inode: Inode, nblocks: int) -> Iterator[tuple[int, int, int]]:
        ptrs = struct.unpack("<15I", inode.block)
        per = self.block_size // 4
        lblock = 0

        def indirect(block: int, level: int):
            nonlocal lblock
            if lblock >= nblocks:
                return
            if not block:
                lblock += per ** (level + 1)
                return
            table = struct.unpack(f"<{per}I", self.vol.read(block * self.block_size, self.block_size))
            for b in table:
                if lblock >= nblocks:
                    return
                if level == 0:
                    if b:
                        yield lblock, 1, b
                    lblock += 1
                else:
                    yield from indirect(b, level - 1)

        for b in ptrs[:12]:
            if lblock >= nblocks:
                return
            if b:
                yield lblock, 1, b
            lblock += 1
        for level, b in enumerate(ptrs[12:]):
            yield from indirect(b, level)

    def read(self, inode: Inode, limit: int = MAX_READ) -> bytes:
        size = min(inode.size, limit)
        if inode.flags & INLINE_DATA_FL:
            return inode.block[:size]
        if inode.is_link and inode.size < 60 and not inode.flags & EXTENTS_FL:
            return inode.block[:inode.size]
        bs = self.block_size
        nblocks = -(-size // bs)
        runs = self._extents(inode.block) if inode.flags & EXTENTS_FL else self._block_map(inode, nblocks)
        buf = bytearray(nblocks * bs)
        for lblock, length, phys in runs:
            if lblock >= nblocks or phys < 0:
                continue
            length = min(length, nblocks - lblock)
            buf[lblock * bs:(lblock + length) * bs] = self.vol.read(phys * bs, length * bs).ljust(length * bs, b"\0")
        return bytes(buf[:size])

    # ---------------------------------------------------------------- directories and paths
    def list_dir(self, inode: Inode) -> Iterator[tuple[str, int, int]]:
        """(name, inode number, file type) per entry; works on hashed (htree) directories too."""
        data = self.read(inode, limit=32 * 1024 * 1024)
        bs = self.block_size
        for base in range(0, len(data), bs):
            pos = base
            while pos + 8 <= min(base + bs, len(data)):
                ino, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, pos)
                if rec_len < 8:
                    break
                if ino and name_len:
                    name = data[pos + 8:pos + 8 + name_len].decode("utf-8", "replace")
                    if name not in (".", ".."):
                        yield name, ino, ftype
                pos += rec_len

    def lookup(self, path: str, follow: bool = True, _depth: int = 0) -> Inode | None:
        node = self.inode(2)
        parts = [p for p in _normalise(path).split("/") if p]
        walked: list[str] = []
        for i, part in enumerate(parts):
            if not node.is_dir:
                return None
            ino = next((n for name, n, _ in self.list_dir(node) if name == part), None)
            if ino is None:
                return None
            node = self.inode(ino)
            if node.is_link and (follow or i < len(parts) - 1) and _depth < 16:
                target = self.read(node).decode("utf-8", "replace")
                rest = "/".join(parts[i + 1:])
                base = "" if target.startswith("/") else "/".join(walked)
                return self.lookup(f"{base}/{target}/{rest}", follow, _depth + 1)
            walked.append(part)
        return node

    def read_file(self, path: str, limit: int = MAX_READ) -> bytes | None:
        try:
            node = self.lookup(path)
        except (ExtError, struct.error):
            return None
        if node is None or not node.is_file:
            return None
        return self.read(node, limit)

    def readlink(self, path: str) -> str | None:
        node = self.lookup(path, follow=False)
        return self.read(node).decode("utf-8", "replace") if node is not None and node.is_link else None


def _normalise(path: str) -> str:
    out: list[str] = []
    for p in path.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if out:
                out.pop()
        else:
            out.append(p)
    return "/" + "/".join(out)
