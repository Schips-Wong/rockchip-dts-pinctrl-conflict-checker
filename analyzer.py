#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备树分析入口。

- .dts / .dtsi：复用 check_pinctrl_conflict.py 的文本解析与 #include 展开逻辑；
- .dtb：用 dtb_parser.py 读取编译后的最终设备树，按 phandle 精确解析
  pinctrl-N 引用和 *-gpios 属性。

对于 .dtb，还可以选择「同名 DTS」解析方式：不做二进制解析，改为查找
同目录下的同名 .dts 源文件并按 DTS 文本解析，便于在只有编译产物时仍能
看到文件行号。

两种输入最终都转换成统一的 PinClaim / AnalysisResult，供 GUI 显示和查询。
"""

from __future__ import annotations

import os
import re
import struct
import time
from typing import Dict, List, Optional, Set, Tuple

import check_pinctrl_conflict as dtscore
from dtb_parser import DeviceTree, FdtNode, FDT_MAGIC, parse_dtb
from model import (
    AnalysisResult,
    Pin,
    PinClaim,
    build_analysis,
    is_okay,
)


# ============================================================
# 通用
# ============================================================

# DTB 的两种解析方式：
#   DTB_SOURCE_BINARY —— 直接解析 .dtb 二进制（编译后的最终状态）；
#   DTB_SOURCE_DTS    —— 改用同目录下的同名 .dts 源文件解析（有行号，可读性好）。
DTB_SOURCE_BINARY = "dtb"
DTB_SOURCE_DTS = "dts"


class SiblingDtsMissing(FileNotFoundError):
    """选择了「同名 DTS」解析方式，但找不到同名 .dts 文件。"""

    def __init__(self, dtb_path: str, searched: List[str]):
        self.dtb_path = dtb_path
        self.searched = list(searched)
        detail = "\n".join(f"  - {p}" for p in self.searched)
        super().__init__(
            f"未找到与 {os.path.basename(dtb_path)} 同名的 DTS 源文件。\n"
            f"已查找：\n{detail}"
        )


def is_dtb_file(path: str) -> bool:
    """按扩展名或 FDT magic 判断是否为 DTB。"""
    if path.lower().endswith(".dtb"):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(4) == struct.pack(">I", FDT_MAGIC)
    except OSError:
        return False


def _is_dtb(path: str) -> bool:
    return is_dtb_file(path)


def find_sibling_dts(dtb_path: str) -> Optional[str]:
    """查找与 DTB 同名的 .dts（同目录、同主文件名，大小写不敏感兜底）。"""
    base = os.path.splitext(os.path.abspath(dtb_path))[0]
    candidate = base + ".dts"
    if os.path.isfile(candidate):
        return candidate

    directory = os.path.dirname(base)
    target = os.path.basename(candidate).lower()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in names:
        if name.lower() != target:
            continue
        full = os.path.join(directory, name)
        if os.path.isfile(full):
            return full
    return None


def _add_warning(bucket: List[str], seen: Set[str], message: str) -> None:
    if message not in seen:
        seen.add(message)
        bucket.append(message)


def _effective_status(node_path: str, status_map: Dict[str, str]) -> str:
    """DTS 源文件里的有效状态：节点自身或其任一父节点被禁用即视为禁用。"""
    parts = [p for p in node_path.replace("\\", "/").split("/") if p]
    for i in range(len(parts), 0, -1):
        prefix = "/".join(parts[:i])
        value = status_map.get(prefix)
        if value and not is_okay(value):
            return value
    return status_map.get(node_path, "okay")


def analyze(path: str, dtb_source: str = DTB_SOURCE_BINARY) -> AnalysisResult:
    """解析 dts 或 dtb，返回统一的结果对象。

    dtb_source 只对 .dtb 生效：
      DTB_SOURCE_BINARY —— 直接解析 .dtb 二进制；
      DTB_SOURCE_DTS    —— 改用同名 .dts 源文件解析。
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文件不存在：{path}")
    if _is_dtb(path):
        if dtb_source == DTB_SOURCE_DTS:
            return analyze_dtb_via_dts(path)
        return analyze_dtb(path)
    return analyze_dts(path)


# ============================================================
# DTS 源文件
# ============================================================

def analyze_dts(entry: str) -> AnalysisResult:
    t0 = time.perf_counter()
    warnings: List[str] = []
    files: List[str] = []
    warn_seen: Set[str] = set()

    all_pinctrl, all_usages, all_gpio, all_status = dtscore.collect_all(
        entry, warnings=warnings, files=files
    )

    claims: List[PinClaim] = []
    usage_active = 0
    gpio_active = 0

    for (node_path, index), (filepath, lineno, refs) in all_usages.items():
        status = _effective_status(node_path, all_status)
        if is_okay(status):
            usage_active += 1
        for ref_label in refs:
            group = all_pinctrl.get(ref_label)
            if group is None:
                _add_warning(
                    warnings, warn_seen,
                    f"节点 {node_path} 的 pinctrl-{index} 引用了未定义的 &{ref_label}"
                    f"（{os.path.basename(filepath)}:{lineno}）",
                )
                continue
            for pin_entry in group.pins:
                claims.append(PinClaim(
                    pin=Pin(pin_entry.bank, pin_entry.pin),
                    node_path=node_path,
                    node_label=node_path,
                    source="pinctrl",
                    ref=ref_label,
                    ref_id=ref_label,
                    index=index,
                    filepath=filepath,
                    lineno=lineno,
                    func=pin_entry.func,
                    bias=str(pin_entry.bias or ""),
                    status=status,
                    group_path=ref_label,
                    group_file=group.filepath,
                    group_line=group.lineno,
                ))

    for (node_path, prop_name), gpio_usage in all_gpio.items():
        status = _effective_status(node_path, all_status)
        if is_okay(status):
            gpio_active += 1
        for bank, pin in gpio_usage.pins:
            claims.append(PinClaim(
                pin=Pin(bank, pin),
                node_path=node_path,
                node_label=node_path,
                source="gpio",
                ref=prop_name,
                ref_id=prop_name,
                index=None,
                filepath=gpio_usage.filepath,
                lineno=gpio_usage.lineno,
                func=None,
                bias="",
                status=status,
            ))

    _add_warning(
        warnings, warn_seen,
        "DTS 源文件是文本解析，节点标签/条件编译等复杂语法可能与最终 DTB 有差异；"
        "需要最终结果时请优先选择编译后的 .dtb。",
    )

    return build_analysis(
        entry=entry,
        kind="dts",
        claims=claims,
        pinctrl_def_count=len(all_pinctrl),
        pinctrl_usage_count=len(all_usages),
        pinctrl_usage_active=usage_active,
        gpio_usage_count=len(all_gpio),
        gpio_usage_active=gpio_active,
        files=files,
        warnings=warnings,
        parse_seconds=time.perf_counter() - t0,
    )


# ============================================================
# DTB 二进制
# ============================================================

GPIO_PROPERTY_EXCLUDE = {
    "gpio-controller",
    "gpio-ranges",
    "gpio-line-names",
    "gpio-reserved-ranges",
    "gpio-int-mask",
    "ngpios",
    "gpios-controller",
}


def _is_gpio_property(name: str) -> bool:
    low = name.lower()
    if low.startswith("#"):
        return False
    if low in GPIO_PROPERTY_EXCLUDE:
        return False
    if "gpio" not in low:
        return False
    return True


def _is_gpio_controller(node: FdtNode) -> bool:
    return "gpio-controller" in node.props or "#gpio-cells" in node.props


def _dtb_status(node: FdtNode) -> str:
    """沿父链查找 status；父节点禁用时子节点也视为禁用。"""
    cur: Optional[FdtNode] = node
    while cur is not None:
        raw = cur.props.get("status")
        if raw is not None:
            value = cur.string("status")
            if not is_okay(value):
                return value
        cur = cur.parent
    return "okay"


def _u32_list(raw: bytes) -> List[int]:
    count = len(raw) // 4
    if count == 0:
        return []
    return list(struct.unpack(">%dI" % count, raw[: count * 4]))


class _DtbAnalyzer:
    def __init__(self, tree: DeviceTree):
        self.tree = tree
        self.warnings: List[str] = list(tree.warnings)
        self.warn_seen: Set[str] = set(self.warnings)
        self.group_cache: Dict[str, List[Tuple[int, int, object, str]]] = {}
        self.claims: List[PinClaim] = []
        self.pinctrl_def_count = sum(
            1 for node in tree.nodes if "rockchip,pins" in node.props
        )
        self.pinctrl_usage_count = 0
        self.pinctrl_usage_active = 0
        self.gpio_usage_count = 0
        self.gpio_usage_active = 0

    # ---------- pinctrl 组 ----------

    def _group_pins(self, group: FdtNode) -> List[Tuple[int, int, object, str]]:
        if group.path in self.group_cache:
            return self.group_cache[group.path]
        cells = group.u32_list("rockchip,pins")
        pins: List[Tuple[int, int, object, str]] = []
        if len(cells) % 4 != 0:
            _add_warning(
                self.warnings, self.warn_seen,
                f"{group.path} 的 rockchip,pins 长度不是 4 的倍数，可能解析不完整。",
            )
        for i in range(0, len(cells) - 3, 4):
            bank, pin, func, bias_handle = cells[i:i + 4]
            pins.append((bank, pin, func, self._bias_text(bias_handle)))
        self.group_cache[group.path] = pins
        return pins

    def _bias_text(self, handle: int) -> str:
        if handle == 0:
            return "0"
        node = self.tree.phandles.get(handle)
        if node is None:
            return f"0x{handle:x}"
        label = self.tree.symbols.get(node.path)
        if label:
            return f"&{label}"
        return f"&{node.name}"

    def _group_label(self, group: FdtNode) -> str:
        return self.tree.symbols.get(group.path, group.name)

    # ---------- 主流程 ----------

    def run(self) -> AnalysisResult:
        tree = self.tree

        for node in tree.nodes:
            if node.path in ("/aliases", "/__symbols__"):
                continue
            status = _dtb_status(node)
            node_label = tree.label(node.path)

            for prop_name, raw in node.props.items():
                m = re.fullmatch(r"pinctrl-(\d+)", prop_name)
                if m:
                    self._handle_pinctrl_property(node, node_label, status, int(m.group(1)), raw)
                    continue
                if _is_gpio_property(prop_name):
                    self._handle_gpio_property(node, node_label, status, prop_name, raw)

        return build_analysis(
            entry=tree.filepath,
            kind="dtb",
            claims=self.claims,
            pinctrl_def_count=self.pinctrl_def_count,
            pinctrl_usage_count=self.pinctrl_usage_count,
            pinctrl_usage_active=self.pinctrl_usage_active,
            gpio_usage_count=self.gpio_usage_count,
            gpio_usage_active=self.gpio_usage_active,
            files=[tree.filepath],
            warnings=self.warnings,
            parse_seconds=0.0,
        )

    def _handle_pinctrl_property(
        self,
        node: FdtNode,
        node_label: str,
        status: str,
        index: int,
        raw: bytes,
    ) -> None:
        self.pinctrl_usage_count += 1
        if is_okay(status):
            self.pinctrl_usage_active += 1

        cells = _u32_list(raw)
        resolved_nodes = 0
        for handle in cells:
            group = self.tree.phandles.get(handle)
            if group is None:
                continue
            resolved_nodes += 1
            if "rockchip,pins" not in group.props:
                # 例如 rk806 的 PMIC pinctrl 组使用 pins/function 属性，
                # 不是 Rockchip GPIO pinmux 组，不参与 GPIO 冲突检测。
                continue
            for bank, pin, func, bias in self._group_pins(group):
                self.claims.append(PinClaim(
                    pin=Pin(bank, pin),
                    node_path=node.path,
                    node_label=node_label,
                    source="pinctrl",
                    ref=self._group_label(group),
                    ref_id=group.path,
                    index=index,
                    filepath="",
                    lineno=0,
                    func=func,
                    bias=bias,
                    status=status,
                    group_path=group.path,
                    group_file="",
                    group_line=0,
                ))

        if resolved_nodes == 0:
            _add_warning(
                self.warnings, self.warn_seen,
                f"节点 {node.path} 的 pinctrl-{index} 没有解析到有效的 pinctrl 组。",
            )

    def _handle_gpio_property(
        self,
        node: FdtNode,
        node_label: str,
        status: str,
        prop_name: str,
        raw: bytes,
    ) -> None:
        if len(raw) < 8:
            return
        cells = _u32_list(raw)
        found = 0
        i = 0
        while i < len(cells):
            controller = self.tree.phandles.get(cells[i])
            if controller is None or not _is_gpio_controller(controller):
                i += 1
                continue

            gpio_cells = controller.u32("#gpio-cells", 2) or 2
            if gpio_cells < 1 or gpio_cells > 4:
                gpio_cells = 2
            if i + 1 >= len(cells):
                break
            pin_number = cells[i + 1]
            bank = self.tree.gpio_banks.get(controller.path)
            if bank is None:
                _add_warning(
                    self.warnings, self.warn_seen,
                    f"{node.path} 的 {prop_name} 引用了无法确定 bank 的 GPIO 控制器 "
                    f"{controller.path}，已跳过。",
                )
                i += 1 + gpio_cells
                continue

            self.claims.append(PinClaim(
                pin=Pin(bank, pin_number),
                node_path=node.path,
                node_label=node_label,
                source="gpio",
                ref=prop_name,
                ref_id=prop_name,
                index=None,
                filepath="",
                lineno=0,
                func=None,
                bias="",
                status=status,
            ))
            found += 1
            i += 1 + gpio_cells

        if found:
            self.gpio_usage_count += 1
            if is_okay(status):
                self.gpio_usage_active += 1


def analyze_dtb(path: str) -> AnalysisResult:
    t0 = time.perf_counter()
    tree = parse_dtb(path)
    analyzer = _DtbAnalyzer(tree)
    result = analyzer.run()
    result.parse_seconds = time.perf_counter() - t0
    return result


def analyze_dtb_via_dts(path: str) -> AnalysisResult:
    """选择「同名 DTS」方式时：用同目录同名 .dts 源文件代替 DTB 做文本解析。"""
    dtb_path = os.path.abspath(path)
    dts_path = find_sibling_dts(dtb_path)
    if dts_path is None:
        raise SiblingDtsMissing(
            dtb_path,
            [os.path.splitext(dtb_path)[0] + ".dts"],
        )

    result = analyze_dts(dts_path)
    # kind 单独标记，便于界面提示“这是 DTB 但按同名 DTS 解析”；
    # entry 仍保留用户打开的 .dtb，报告文件名与之对应。
    result.kind = "dtb_dts"
    result.entry = dtb_path
    result.base_dir = os.path.dirname(dtb_path)
    result.warnings.insert(
        0,
        f"解析方式：使用同名 DTS 源文件代替 DTB 解析 —— {dts_path}",
    )
    return result
