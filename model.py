#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共用数据模型与辅助函数。

GUI、DTS 分析器、DTB 分析器和报告导出都使用这里的数据结构，
避免把显示逻辑散落在各个解析器里。
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================
# GPIO pin
# ============================================================

def is_okay(status: Optional[str]) -> bool:
    """设备树 status 是否表示启用。Linux 同时接受 "okay" 和 "ok"。"""
    if not status:
        return True
    return status.strip().lower() in ("okay", "ok")


def rk_pin_to_number(pin_name: str) -> Optional[int]:
    """RK_PA0 -> 0, RK_PB2 -> 10, RK_PD7 -> 31。"""
    if not isinstance(pin_name, str):
        return None
    m = re.fullmatch(r"(?:RK_)?P([A-D])([0-7])", pin_name.strip().upper())
    if not m:
        return None
    return (ord(m.group(1)) - ord("A")) * 8 + int(m.group(2))


def pin_number_to_rk(pin: int) -> str:
    """0 -> RK_PA0, 10 -> RK_PB2。pin 超出 0..31 时返回普通编号。"""
    if 0 <= pin < 32:
        return f"RK_P{chr(ord('A') + pin // 8)}{pin % 8}"
    return f"PIN{pin}"


@dataclass(frozen=True, order=True)
class Pin:
    """一个 Rockchip GPIO 引脚。bank 为 gpio0..gpio7 的编号，pin 为 bank 内 0..31。"""

    bank: int
    pin: int

    @property
    def name(self) -> str:
        """用于显示和查询的标准名称，例如 GPIO4_RK_PB2。"""
        if 0 <= self.pin < 32:
            return f"GPIO{self.bank}_{pin_number_to_rk(self.pin)}"
        return f"GPIO{self.bank}_{self.pin}"

    @property
    def global_number(self) -> int:
        """Linux 全局 GPIO 编号（bank * 32 + pin）。"""
        return self.bank * 32 + self.pin

    @property
    def alias_name(self) -> str:
        """简短写法，例如 GPIO4_B2。"""
        if 0 <= self.pin < 32:
            return f"GPIO{self.bank}_{chr(ord('A') + self.pin // 8)}{self.pin % 8}"
        return f"GPIO{self.bank}_{self.pin}"

    def __str__(self) -> str:  # pragma: no cover - 仅用于调试
        return self.name


# ============================================================
# 占用记录
# ============================================================

@dataclass
class PinClaim:
    """某个节点对某个 GPIO pin 的一次占用。"""

    pin: Pin
    node_path: str                 # 稳定的节点路径，用于冲突归并
    node_label: str                # 显示名（DTS 为节点路径，DTB 优先用 __symbols__ 标签）
    source: str                    # "pinctrl" 或 "gpio"
    ref: str                       # pinctrl 组标签 / gpio 属性名
    ref_id: str                    # 用于判断"是否同一个 pinctrl 组"的唯一 id
    index: Optional[int]           # pinctrl-N 的 N；gpio 为 None
    filepath: str                  # 引用所在文件（DTB 为空）
    lineno: int                    # 引用所在行（DTB 为 0）
    func: Any = None               # 复用功能号
    bias: str = ""                 # 上下拉配置
    status: str = "okay"           # 节点状态
    group_path: str = ""           # pinctrl 组定义所在节点路径
    group_file: str = ""           # pinctrl 组定义文件
    group_line: int = 0            # pinctrl 组定义行号

    @property
    def active(self) -> bool:
        return is_okay(self.status)

    @property
    def tag(self) -> Tuple:
        """同一 pin 上去重用的占用标签。

        pinctrl 按组标签去重：多个节点引用同一个组属于共享配置，不算冲突；
        gpio 按 (节点, 属性名) 去重。
        """
        if self.source == "pinctrl":
            return ("p", self.ref_id)
        return ("g", self.node_path, self.ref_id)

    @property
    def source_text(self) -> str:
        if self.source == "pinctrl":
            if self.index is None:
                return f"&{self.ref}"
            return f"pinctrl-{self.index}  &{self.ref}"
        return self.ref

    @property
    def func_text(self) -> str:
        if self.func is None:
            return "—"
        if isinstance(self.func, int):
            return f"{self.func}（GPIO 模式）" if self.func == 0 else str(self.func)
        return str(self.func)

    @property
    def bias_text(self) -> str:
        return self.bias or "—"


@dataclass
class AnalysisResult:
    """一次完整解析的结果，供 GUI 与报告导出使用。"""

    entry: str
    kind: str                       # "dts" 或 "dtb"
    base_dir: str
    claims: List[PinClaim] = field(default_factory=list)
    pin_claims: Dict[Pin, List[PinClaim]] = field(default_factory=dict)
    conflicts: Dict[Pin, List[PinClaim]] = field(default_factory=dict)
    pinctrl_def_count: int = 0
    pinctrl_usage_count: int = 0
    pinctrl_usage_active: int = 0
    gpio_usage_count: int = 0
    gpio_usage_active: int = 0
    files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    parse_seconds: float = 0.0

    # ---------- 派生信息 ----------

    @property
    def active_claims(self) -> List[PinClaim]:
        return [c for c in self.claims if c.active]

    @property
    def active_pins(self) -> List[Pin]:
        return sorted({c.pin for c in self.claims if c.active})

    @property
    def disabled_pins(self) -> List[Pin]:
        return sorted({c.pin for c in self.claims if not c.active})

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    @property
    def file_name(self) -> str:
        return os.path.basename(self.entry)

    @property
    def kind_text(self) -> str:
        if self.kind == "dtb":
            return "DTB（编译后的最终状态）"
        if self.kind == "dtb_dts":
            return "DTB → 同名 DTS（源文件 + include）"
        return "DTS（源文件 + include）"

    @property
    def uses_dts_source(self) -> bool:
        """本次解析是否走了 DTS 文本解析（含 DTB 换成同名 DTS 的情况）。"""
        return self.kind in ("dts", "dtb_dts")

    def relpath(self, path: str) -> str:
        if not path:
            return ""
        try:
            return os.path.relpath(path, self.base_dir)
        except ValueError:
            return path

    def claims_for(self, pin: Pin) -> List[PinClaim]:
        return list(self.pin_claims.get(pin, ()))

    def conflict_for(self, pin: Pin) -> List[PinClaim]:
        return list(self.conflicts.get(pin, ()))

    def is_conflict_pin(self, pin: Pin) -> bool:
        return pin in self.conflicts


# ============================================================
# 由占用记录构建结果（冲突判定集中在这里）
# ============================================================

def build_analysis(
    entry: str,
    kind: str,
    claims: Iterable[PinClaim],
    *,
    pinctrl_def_count: int,
    pinctrl_usage_count: int,
    pinctrl_usage_active: int,
    gpio_usage_count: int,
    gpio_usage_active: int,
    files: Iterable[str],
    warnings: Iterable[str],
    parse_seconds: float = 0.0,
) -> AnalysisResult:
    """整理占用记录，计算每 pin 的占用者以及冲突集合。

    冲突规则与命令行工具保持一致：
      - 只有 status 不是 okay/ok 的节点不参与冲突；
      - 同一状态索引内，不同节点引用不同的 pinctrl 组但占用同一 pin -> 冲突；
      - 不同节点通过 *-gpios 占用同一 pin -> 冲突；
      - pinctrl 与 gpio 交叉占用也算冲突；
      - 同一节点内部的 pinctrl + gpio 配套使用豁免；
      - 多个节点引用同一个 pinctrl 组视为共享配置，不算冲突。
    """
    base_dir = os.path.dirname(os.path.abspath(entry))
    claim_list: List[PinClaim] = []
    seen_claims = set()
    for claim in claims:
        # 同一节点、同一状态、同一组里重复出现的同一个 pin 只保留一次；
        # 但不同节点共享同一个 pinctrl 组时仍然各自保留（查询时需要看到所有节点）。
        key = (claim.pin, claim.node_path, claim.source, claim.ref_id, claim.index)
        if key in seen_claims:
            continue
        seen_claims.add(key)
        claim_list.append(claim)
    claim_list.sort(
        key=lambda c: (c.pin.bank, c.pin.pin, c.node_path, c.source, c.index if c.index is not None else -1, c.ref)
    )

    unique_warnings: List[str] = []
    seen_warnings = set()
    for warning in warnings:
        if warning not in seen_warnings:
            seen_warnings.add(warning)
            unique_warnings.append(warning)

    pin_claims: Dict[Pin, List[PinClaim]] = defaultdict(list)
    for claim in claim_list:
        pin_claims[claim.pin].append(claim)

    # pin -> {tag -> claim}，只统计启用节点
    by_tag: Dict[Pin, Dict[Tuple, PinClaim]] = defaultdict(dict)
    for claim in claim_list:
        if not claim.active:
            continue
        by_tag[claim.pin].setdefault(claim.tag, claim)

    conflicts: Dict[Pin, List[PinClaim]] = {}
    for pin, tag_map in by_tag.items():
        if len(tag_map) < 2:
            continue
        if len({c.node_path for c in tag_map.values()}) < 2:
            continue
        # 同一节点可能有 pinctrl 与 gpio 两种占用，按节点聚合成一个代表，
        # 优先保留 pinctrl（与命令行工具输出一致）。
        # 这里从该 pin 的“全部启用占用”聚合，而不是只从 tag_map 聚合，
        # 这样两个节点共享同一个 pinctrl 组、第三个节点又来抢这个 pin 时，
        # 共享组的两个节点也会完整列出。
        by_node: Dict[str, PinClaim] = {}
        for claim in pin_claims.get(pin, ()):
            if not claim.active:
                continue
            existing = by_node.get(claim.node_path)
            if existing is None:
                by_node[claim.node_path] = claim
            elif claim.source == "pinctrl" and existing.source == "gpio":
                by_node[claim.node_path] = claim
        conflicts[pin] = sorted(by_node.values(), key=lambda c: (c.node_path, c.source))

    return AnalysisResult(
        entry=os.path.abspath(entry),
        kind=kind,
        base_dir=base_dir,
        claims=claim_list,
        pin_claims=dict(pin_claims),
        conflicts=conflicts,
        pinctrl_def_count=pinctrl_def_count,
        pinctrl_usage_count=pinctrl_usage_count,
        pinctrl_usage_active=pinctrl_usage_active,
        gpio_usage_count=gpio_usage_count,
        gpio_usage_active=gpio_usage_active,
        files=list(files),
        warnings=unique_warnings,
        parse_seconds=parse_seconds,
    )


# ============================================================
# GPIO 查询输入解析
# ============================================================

class QueryError(ValueError):
    """查询输入无法识别。"""


@dataclass
class QuerySpec:
    text: str
    pins: List[Pin]
    wildcard_bank: bool = False

    @property
    def label(self) -> str:
        if len(self.pins) == 1:
            return self.pins[0].name
        if not self.pins:
            return self.text
        if self.wildcard_bank:
            return f"所有 bank 的 {pin_number_to_rk(self.pins[0].pin)}"
        names = "、".join(p.name for p in self.pins[:8])
        return names + ("…" if len(self.pins) > 8 else "")


def _query_error(text: str) -> QueryError:
    return QueryError(
        f"无法识别 GPIO 描述：{text}\n"
        "支持格式：GPIO4_RK_PB2、GPIO4_PB2、GPIO4_B2、4 RK_PB2、4 10、"
        "gpio4 10、bank4 pin10、RK_PB2（查询所有 bank）。"
    )


def parse_gpio_query(text: str) -> QuerySpec:
    """解析用户输入的 GPIO 描述。

    支持：
      GPIO4_RK_PB2 / gpio4_rk_pb2
      GPIO4_PB2 / GPIO4_B2
      4 RK_PB2 / 4 10 / 4,10
      gpio4 10 / bank4 pin10
      RK_PB2 / PB2        -> 所有 bank 的同一位置
    """
    raw = (text or "").strip()
    if not raw:
        raise QueryError("请输入要查询的 GPIO，例如 GPIO4_RK_PB2 或 4 10。")

    normalized = raw.upper()
    for ch in "_-:,./\\":
        normalized = normalized.replace(ch, " ")
    tokens = [t for t in normalized.split() if t]
    if not tokens:
        raise _query_error(raw)

    bank: Optional[int] = None
    letter: Optional[str] = None
    numeric_pin: Optional[int] = None

    for token in tokens:
        if token in ("RK", "P"):
            continue

        m = re.fullmatch(r"(?:GPIO|BANK)(\d+)", token)
        if m:
            if bank is not None:
                raise QueryError(f"GPIO 描述里出现了两个 bank：{raw}")
            bank = int(m.group(1))
            continue

        m = re.fullmatch(r"PIN(\d+)", token)
        if m:
            if numeric_pin is not None:
                raise QueryError(f"GPIO 描述里出现了两个引脚号：{raw}")
            numeric_pin = int(m.group(1))
            continue

        m = re.fullmatch(r"P?([A-D])([0-7])", token)
        if m:
            if letter is not None:
                raise QueryError(f"GPIO 描述里出现了两个引脚位置：{raw}")
            letter = m.group(1)
            numeric_pin = (ord(letter) - ord("A")) * 8 + int(m.group(2))
            continue

        m = re.fullmatch(r"(\d+)", token)
        if m:
            value = int(m.group(1))
            if bank is None:
                bank = value
            elif numeric_pin is None:
                numeric_pin = value
            else:
                raise QueryError(f"GPIO 描述里有多余的数字：{raw}")
            continue

        raise _query_error(raw)

    if numeric_pin is None:
        raise QueryError(f"缺少引脚号：{raw}\n例如 GPIO4_RK_PB2 或 4 10。")
    if not 0 <= numeric_pin <= 31:
        raise QueryError(f"引脚号必须在 0..31 之间（或 A0..D7）：{raw}")

    if bank is None:
        # 只给了 RK_PB2 这类位置：查询所有 bank。
        return QuerySpec(text=raw, pins=[Pin(b, numeric_pin) for b in range(8)], wildcard_bank=True)

    if not 0 <= bank <= 31:
        raise QueryError(f"bank 编号必须在 0..31 之间：{raw}")
    return QuerySpec(text=raw, pins=[Pin(bank, numeric_pin)], wildcard_bank=False)
