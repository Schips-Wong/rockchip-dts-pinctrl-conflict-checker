#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AnalysisResult 导出为 Markdown 报告。"""

from __future__ import annotations

import os
from typing import List

from model import AnalysisResult, PinClaim


def _location(result: AnalysisResult, claim: PinClaim) -> str:
    if claim.filepath and claim.lineno:
        return f"{result.relpath(claim.filepath)}:{claim.lineno}"
    return "DTB（无行号）"


def _group_location(result: AnalysisResult, claim: PinClaim) -> str:
    if claim.group_file and claim.group_line:
        return f"{result.relpath(claim.group_file)}:{claim.group_line}"
    if claim.group_path:
        return claim.group_path
    return "—"


def _claim_row(result: AnalysisResult, claim: PinClaim) -> str:
    status = "启用" if claim.active else f"禁用（{claim.status}）"
    return (
        f"| {claim.node_label} | {claim.source_text} | "
        f"func={claim.func_text}，bias={claim.bias_text} | "
        f"{_location(result, claim)} | {status} |"
    )


def build_markdown(result: AnalysisResult, include_all_pins: bool = False) -> str:
    lines: List[str] = []
    lines.append("# 设备树 GPIO 冲突报告")
    lines.append("")
    lines.append(f"- **输入文件**：`{result.entry}`")
    lines.append(f"- **输入类型**：{result.kind_text}")
    lines.append(f"- **解析用时**：{result.parse_seconds:.2f} 秒")
    lines.append("")
    lines.append("## 概要")
    lines.append("")
    lines.append("| 项目 | 数量 |")
    lines.append("| --- | ---: |")
    lines.append(f"| pinctrl 组定义 | {result.pinctrl_def_count} |")
    lines.append(
        f"| pinctrl-N 引用 | {result.pinctrl_usage_count}"
        f"（启用 {result.pinctrl_usage_active}） |"
    )
    lines.append(
        f"| *-gpios 属性 | {result.gpio_usage_count}"
        f"（启用 {result.gpio_usage_active}） |"
    )
    lines.append(f"| 被占用的 GPIO pin | {len(result.active_pins)} |")
    lines.append(f"| **冲突 pin** | **{result.conflict_count}** |")
    if result.uses_dts_source:
        lines.append(f"| 实际解析文件 | {len(result.files)} |")
    lines.append("")

    if result.conflicts:
        lines.append("## 冲突详情")
        lines.append("")
        for index, (pin, owners) in enumerate(sorted(result.conflicts.items()), 1):
            lines.append(f"### {index}. {pin.name}")
            lines.append("")
            lines.append(
                f"bank={pin.bank}，pin={pin.pin}，Linux 全局编号={pin.global_number}，"
                f"占用节点 {len(owners)} 个。"
            )
            lines.append("")
            lines.append("| 节点 | 来源 | 配置 | 位置 | 状态 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for claim in owners:
                lines.append(_claim_row(result, claim))
            lines.append("")

            # 同 pin 但不构成冲突的占用也列出，便于判断是否共享同一个 pinctrl 组
            conflict_keys = {(c.node_path, c.source, c.ref_id) for c in owners}
            others = [
                c for c in result.claims_for(pin)
                if (c.node_path, c.source, c.ref_id) not in conflict_keys
            ]
            if others:
                lines.append("其他占用（共享同一 pinctrl 组或节点已禁用，不计为冲突）：")
                lines.append("")
                for claim in others:
                    state = "启用" if claim.active else f"禁用（{claim.status}）"
                    lines.append(
                        f"- {claim.node_label} ← {claim.source_text}"
                        f"（{_location(result, claim)}，{state}）"
                    )
                lines.append("")
    else:
        lines.append("## 冲突详情")
        lines.append("")
        lines.append("未发现 pinctrl / gpio 冲突。")
        lines.append("")

    if include_all_pins:
        lines.append("## 全部 GPIO 占用")
        lines.append("")
        lines.append("| 引脚 | 节点 | 来源 | 配置 | 位置 | 状态 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for claim in result.claims:
            status = "启用" if claim.active else f"禁用（{claim.status}）"
            lines.append(
                f"| {claim.pin.name} | {claim.node_label} | {claim.source_text} | "
                f"func={claim.func_text}，bias={claim.bias_text} | "
                f"{_location(result, claim)} | {status} |"
            )
        lines.append("")

    if result.warnings:
        lines.append("## 解析提示")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("由 Rockchip 设备树 GPIO 冲突检测器生成。")
    return "\n".join(lines) + "\n"


def default_report_name(result: AnalysisResult) -> str:
    stem = os.path.splitext(os.path.basename(result.entry))[0]
    return f"{stem}-conflict-report.md"
