#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小的 Flattened Device Tree (DTB) 二进制解析器。

不依赖 dtc：直接读取 DTB 的 struct/strings 块，构造节点树，并解析
phandle、/aliases、/__symbols__，供 analyzer.py 做 pinctrl / GPIO 分析。
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9


class DtbError(Exception):
    """DTB 文件格式错误。"""


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _decode_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


@dataclass
class FdtNode:
    name: str
    path: str
    parent: Optional["FdtNode"] = None
    props: Dict[str, bytes] = field(default_factory=dict)
    children: List["FdtNode"] = field(default_factory=list)

    # ---------- 属性读取 ----------

    def prop_bytes(self, name: str) -> Optional[bytes]:
        return self.props.get(name)

    def string(self, name: str, default: str = "") -> str:
        raw = self.props.get(name)
        if raw is None:
            return default
        return _decode_string(raw)

    def u32(self, name: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.props.get(name)
        if raw is None or len(raw) < 4:
            return default
        return struct.unpack(">I", raw[:4])[0]

    def u32_list(self, name: str) -> List[int]:
        raw = self.props.get(name)
        if not raw:
            return []
        count = len(raw) // 4
        if count == 0:
            return []
        return list(struct.unpack(">%dI" % count, raw[: count * 4]))

    def has(self, name: str) -> bool:
        return name in self.props

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class DeviceTree:
    """解析后的 DTB。"""

    filepath: str
    root: FdtNode
    nodes: List[FdtNode]
    by_path: Dict[str, FdtNode]
    phandles: Dict[int, FdtNode]
    symbols: Dict[str, str]            # node path -> 第一个标签
    labels: Dict[str, str]             # 标签 -> node path
    gpio_banks: Dict[str, int]         # gpio 控制器 node path -> bank 编号
    warnings: List[str] = field(default_factory=list)

    def node(self, path: str) -> Optional[FdtNode]:
        return self.by_path.get(path)

    def label(self, path: str) -> str:
        """节点显示名：有 __symbols__ 标签用标签，否则用路径最后一段。"""
        if path in self.symbols:
            return self.symbols[path]
        if path == "/":
            return "/"
        return path.rsplit("/", 1)[-1]


def _parse_struct(data: bytes, off_struct: int, off_strings: int, size_strings: int) -> FdtNode:
    pos = off_struct
    stack: List[FdtNode] = []
    root: Optional[FdtNode] = None
    end = len(data)

    while pos + 4 <= end:
        tag = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4

        if tag == FDT_BEGIN_NODE:
            nul = data.find(b"\x00", pos)
            if nul < 0:
                raise DtbError("节点名没有以 NUL 结尾")
            name = data[pos:nul].decode("utf-8", errors="replace")
            pos = _align4(nul + 1)

            parent = stack[-1] if stack else None
            if parent is None:
                path = "/"
            else:
                path = parent.path.rstrip("/") + "/" + name
            node = FdtNode(name=name, path=path, parent=parent)
            if parent is not None:
                parent.children.append(node)
            else:
                if root is not None:
                    raise DtbError("DTB 中出现多个根节点")
                root = node
            stack.append(node)

        elif tag == FDT_END_NODE:
            if not stack:
                raise DtbError("多余的 FDT_END_NODE")
            stack.pop()

        elif tag == FDT_PROP:
            if not stack:
                raise DtbError("属性出现在根节点之外")
            if pos + 8 > end:
                raise DtbError("属性头越界")
            length, nameoff = struct.unpack(">II", data[pos:pos + 8])
            pos += 8
            if nameoff >= size_strings:
                raise DtbError("属性名偏移越界")
            name_end = data.find(b"\x00", off_strings + nameoff)
            if name_end < 0:
                raise DtbError("属性名没有以 NUL 结尾")
            prop_name = data[off_strings + nameoff:name_end].decode("utf-8", errors="replace")
            if pos + length > end:
                raise DtbError("属性值越界")
            value = data[pos:pos + length]
            pos = _align4(pos + length)
            stack[-1].props[prop_name] = value

        elif tag == FDT_NOP:
            continue

        elif tag == FDT_END:
            break

        else:
            raise DtbError(f"未知的 FDT token: {tag}")

    if root is None:
        raise DtbError("DTB 中没有根节点")
    return root


def parse_dtb(filepath: str) -> DeviceTree:
    """读取并解析 .dtb 文件。"""
    filepath = os.path.abspath(filepath)
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < 40:
        raise DtbError("文件太小，不是有效的 DTB")
    header = struct.unpack(">10I", data[:40])
    (magic, totalsize, off_struct, off_strings, off_rsvmap,
     version, last_comp_version, boot_cpuid, size_strings, size_struct) = header
    if magic != FDT_MAGIC:
        raise DtbError(f"DTB magic 不正确：0x{magic:08x}")
    if totalsize > len(data):
        raise DtbError("DTB 头部 totalsize 大于实际文件大小")
    if off_strings + size_strings > len(data):
        raise DtbError("DTB 字符串块越界")

    root = _parse_struct(data, off_struct, off_strings, size_strings)
    nodes = list(root.walk())
    by_path = {n.path: n for n in nodes}

    phandles: Dict[int, FdtNode] = {}
    for node in nodes:
        for prop_name in ("phandle", "linux,phandle"):
            raw = node.props.get(prop_name)
            if raw is not None and len(raw) >= 4:
                handle = struct.unpack(">I", raw[:4])[0]
                phandles.setdefault(handle, node)
                break

    warnings: List[str] = []

    # __symbols__: label -> path；同时建立 path -> label（同一路径取第一个标签）。
    symbols: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    symbols_node = by_path.get("/__symbols__")
    if symbols_node is not None:
        for label, raw in symbols_node.props.items():
            path = _decode_string(raw)
            if not path:
                continue
            labels.setdefault(label, path)
            symbols.setdefault(path, label)

    # /aliases 里的 gpioN 可能是路径字符串，也可能是 phandle。
    gpio_banks: Dict[str, int] = {}
    aliases = by_path.get("/aliases")
    if aliases is not None:
        for name, raw in aliases.props.items():
            m = re.fullmatch(r"gpio(\d+)", name)
            if not m:
                continue
            target: Optional[FdtNode] = None
            if raw.startswith(b"/"):
                target = by_path.get(_decode_string(raw))
            elif len(raw) >= 4:
                target = phandles.get(struct.unpack(">I", raw[:4])[0])
            if target is not None:
                gpio_banks[target.path] = int(m.group(1))

    # 兜底：用 gpio-ranges 的第三个 cell（gpio-base / 32）推断 bank。
    for node in nodes:
        if node.path in gpio_banks:
            continue
        if "gpio-controller" not in node.props and "#gpio-cells" not in node.props:
            continue
        ranges = node.u32_list("gpio-ranges")
        if len(ranges) >= 4:
            gpio_base = ranges[2]
            if gpio_base % 32 == 0 and 0 <= gpio_base // 32 <= 31:
                gpio_banks[node.path] = gpio_base // 32

    if not gpio_banks:
        warnings.append("没有找到 /aliases/gpioN 或 gpio-ranges，无法确定 GPIO 控制器对应的 bank。")

    return DeviceTree(
        filepath=filepath,
        root=root,
        nodes=nodes,
        by_path=by_path,
        phandles=phandles,
        symbols=symbols,
        labels=labels,
        gpio_banks=gpio_banks,
        warnings=warnings,
    )
