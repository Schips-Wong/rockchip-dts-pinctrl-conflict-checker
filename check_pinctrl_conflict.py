#!/usr/bin/python3
# -*- encoding=utf8 -*-
"""
pinctrl 冲突检测工具

功能: 解析 Linux 设备树中的 #include 包含关系，收集所有节点的 pinctrl-N 属性
      (pinctrl-0 / pinctrl-1 / pinctrl-2 / ...)，将其展开为具体的 GPIO
      (bank, pin) 占用，检测不同节点之间的 pin 冲突。

原理:
  1. 从主 dts 出发，递归解析 #include 链
  2. 在每个 dts/dtsi 文件中收集:
     a) pinctrl 节点定义:  node_name { rockchip,pins = <bank pin func bias>; }
     b) 消费节点的 pinctrl-N 引用:  &node { pinctrl-0 = <&pinmux_a &pinmux_b>; }
  3. 将 pinctrl-N 中的引用展开为 (bank, pin) 列表
  4. 按状态索引分组检测: 同一状态 (如 pinctrl-0) 内不同节点引用不同 pinmux
     且占用同一 (bank, pin) 时报告冲突; 不同状态之间天然允许共用 pin, 不算冲突

用法:
  python3 check_pinctrl_conflict.py <主dts文件>

示例:
  python3 check_pinctrl_conflict.py rk3588-nvr-demo-v10-ipc-4x-linux.dts
"""

import re
import os
import sys
import argparse

# ============================================================
# 数据结构
# ============================================================

class PinEntry:
    """一个 pinctrl pin 配置条目"""
    def __init__(self, bank, pin, func, bias, filepath, lineno):
        self.bank = bank
        self.pin = pin
        self.func = func
        self.bias = bias
        self.filepath = filepath
        self.lineno = lineno

    @property
    def key(self):
        """用于冲突比较的唯一键: (bank, pin)"""
        return (self.bank, self.pin)

    def __repr__(self):
        return f"PinEntry(bank={self.bank}, pin={self.pin}, func={self.func}, bias={self.bias})"


class PinctrlNode:
    """pinctrl 子节点定义, 包含一组 PinEntry"""
    def __init__(self, label, filepath, lineno):
        self.label = label
        self.filepath = filepath
        self.lineno = lineno
        self.pins = []

    def __repr__(self):
        return f"PinctrlNode({self.label}, pins={self.pins})"


class NodeUsage:
    """一个设备节点对某个 pinctrl-N 状态的使用"""
    def __init__(self, node_path, filepath, lineno, pinctrl_index):
        self.node_path = node_path
        self.filepath = filepath
        self.lineno = lineno
        self.pinctrl_index = pinctrl_index  # 0 对应 pinctrl-0, 1 对应 pinctrl-1, ...
        self.refs = []

    def __repr__(self):
        return f"NodeUsage({self.node_path}, pinctrl-{self.pinctrl_index}, refs={self.refs})"


# ============================================================
# RK3588 pin 命名解析 (bank, pin)
# ============================================================

def rk_pin_to_number(pin_name):
    """将 RK_Pxx 转为数值: RK_PA0→0, RK_PB2→10, RK_PD5→29 等"""
    if not isinstance(pin_name, str):
        return None
    m = re.match(r'RK_P([A-D])(\d+)', pin_name, re.IGNORECASE)
    if not m:
        return None
    bank_char = m.group(1).upper()
    pin_num = int(m.group(2))
    bank_offset = ord(bank_char) - ord('A')
    return bank_offset * 8 + pin_num


# ============================================================
# 文件解析
# ============================================================

class DtsFile:
    """代表一个 dts/dtsi 文件"""
    def __init__(self, filepath):
        self.filepath = os.path.abspath(filepath)
        self.includes = []
        self.pinctrl_nodes = {}     # label → PinctrlNode
        self.node_usages = []       # NodeUsage 列表

    def parse(self):
        base_dir = os.path.dirname(self.filepath)
        with open(self.filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # --- 第一步: 去除注释 (保持行号不变) ---
        # 1) 先整体去除跨行块注释 /* ... */ (DOTALL 让 . 匹配换行)
        #    用等量换行符替换注释内容, 保证行号不错位
        def _blank_out_block_comment(m):
            # 保留注释内的换行符数量, 其余字符替换为空格
            return re.sub(r'[^\n]', ' ', m.group(0))

        content = re.sub(r'/\*.*?\*/', _blank_out_block_comment, content, flags=re.DOTALL)
        # 2) 再逐行去除 // 行注释 (行注释不跨行, 不影响行号)
        cleaned_lines = []
        for line in content.split('\n'):
            line = re.sub(r'//.*', '', line)
            cleaned_lines.append(line)

        # --- 第二步: 提取 #include ---
        for line in cleaned_lines:
            m = re.match(r'#include\s+[<"]([^>"]+\.dts[^>"]*)[>"]', line)
            if m:
                inc_name = m.group(1)
                inc_path = os.path.normpath(os.path.join(base_dir, inc_name))
                self.includes.append(inc_path)

        # --- 第三步: 解析节点结构 ---
        self._parse_nodes(cleaned_lines)

    def _parse_nodes(self, lines):
        """
        逐行解析节点层级和 pinctrl 定义 / pinctrl-0 引用。
        """
        node_stack = []             # 节点名栈
        inside_pinctrl = False      # 是否在 &pinctrl { } 内部

        # pinctrl 子节点收集状态
        current_label = None
        current_lineno = 0
        collecting = False
        pending_lines = []

        for i, line in enumerate(lines):
            lineno = i + 1
            stripped = line.strip()
            if not stripped:
                continue

            # --- 检测节点开始 ---
            node_start_m = re.match(r'(&?[\w@\-\./]+)\s*\{', stripped)
            if node_start_m:
                node_name = node_start_m.group(1).lstrip('&')
                node_stack.append(node_name)

                if node_name == 'pinctrl' or 'pinctrl' in node_name:
                    inside_pinctrl = True
                continue

            # --- 检测节点结束 ---
            if stripped == '};':
                if node_stack:
                    popped = node_stack.pop()
                    if popped == 'pinctrl' or 'pinctrl' in popped:
                        inside_pinctrl = False

                if collecting:
                    self._flush_pins(current_label, current_lineno, pending_lines)
                    collecting = False
                    pending_lines = []
                continue

            # --- pinctrl 内部: 收集 pinmux 子节点 ---
            if inside_pinctrl:
                # 匹配标签节点:  label: name {
                label_m = re.match(r'([\w]+)\s*:\s*[\w\-]+(\s*\{)', stripped)
                if label_m:
                    label = label_m.group(1)
                    current_label = label
                    current_lineno = lineno
                    collecting = True
                    pending_lines = []

                    # 提取节点名推入栈 (从 label: name 中取 name 部分)
                    rest = stripped[stripped.index(':') + 1:].strip()
                    inner_name = rest.rstrip('{').strip()
                    node_stack.append(inner_name)
                    continue

                # 在 pinctrl 子节点内部, 收集 rockchip,pins 行
                if collecting:
                    if 'rockchip,pins' in stripped:
                        pending_lines.append(stripped)
                        if stripped.rstrip().endswith(';'):
                            self._flush_pins(current_label, current_lineno, pending_lines)
                            collecting = False
                            pending_lines = []
                    elif pending_lines:
                        pending_lines.append(stripped)
                        if stripped.rstrip().endswith(';'):
                            self._flush_pins(current_label, current_lineno, pending_lines)
                            collecting = False
                            pending_lines = []
                continue

            # --- 任意节点内部: 收集 pinctrl-N 引用 (N = 0, 1, 2, ...) ---
            # 单行格式
            pinctrl_m = re.match(r'pinctrl-(\d+)\s*=\s*<(.+)>;', stripped)
            if pinctrl_m:
                pinctrl_index = int(pinctrl_m.group(1))
                refs_str = pinctrl_m.group(2)
                refs = re.findall(r'&([\w]+)', refs_str)
                node_path = '/'.join(node_stack) if node_stack else '/'
                usage = NodeUsage(node_path, self.filepath, lineno, pinctrl_index)
                usage.refs = refs
                self.node_usages.append(usage)
                continue

            # 跨行格式
            pinctrl_start_m = re.match(r'pinctrl-(\d+)\s*=\s*<(.+)', stripped)
            if pinctrl_start_m:
                pinctrl_index = int(pinctrl_start_m.group(1))
                refs_combined = pinctrl_start_m.group(2)
                j = i + 1
                while j < len(lines):
                    cont_line = lines[j].strip()
                    refs_combined += ' ' + cont_line
                    if '>' in cont_line:
                        break
                    j += 1
                refs = re.findall(r'&([\w]+)', refs_combined)
                node_path = '/'.join(node_stack) if node_stack else '/'
                usage = NodeUsage(node_path, self.filepath, lineno, pinctrl_index)
                usage.refs = refs
                self.node_usages.append(usage)
                continue

    def _flush_pins(self, label, lineno, pin_lines):
        """解析缓存的 rockchip,pins 行"""
        if not label:
            return
        combined = ' '.join(pin_lines)
        combined = re.sub(r'rockchip\s*,\s*pins\s*=\s*', '', combined)
        combined = combined.rstrip(';')

        pin_entries = re.findall(
            r'<\s*(\d+)\s+(RK_P[A-D]\d+)\s+(\d+)\s+(&?\w+)\s*>',
            combined)

        node = self.pinctrl_nodes.get(label)
        if not node:
            node = PinctrlNode(label, self.filepath, lineno)
            self.pinctrl_nodes[label] = node

        for bank_str, pin_name, func, bias in pin_entries:
            bank = int(bank_str)
            pin_num = rk_pin_to_number(pin_name)
            if pin_num is not None:
                node.pins.append(PinEntry(bank, pin_num, int(func), bias, self.filepath, lineno))


# ============================================================
# 全局数据收集: 递归解析 #include 树
# ============================================================

def collect_all(entry_dts, visited=None):
    """
    递归解析 #include 链，收集所有 pinctrl 定义和 pinctrl-N 引用。

    设备树覆盖语义:
      - 同一节点路径、同一状态索引的 pinctrl-N: 后 include 的覆盖先 include 的
      - 同一 label 的 pinctrl 节点定义: 后 include 的覆盖先 include 的
      - 所以使用 dict 存储，后出现的自动覆盖先出现的
    """
    if visited is None:
        visited = set()

    entry_path = os.path.abspath(entry_dts)
    if entry_path in visited:
        return {}, {}
    visited.add(entry_path)

    if not os.path.isfile(entry_path):
        print(f"[WARN] 文件不存在, 跳过: {entry_path}", file=sys.stderr)
        return {}, {}

    dts = DtsFile(entry_path)
    dts.parse()

    # 当前文件的 pinctrl 定义: label → PinctrlNode
    all_pinctrl = dict(dts.pinctrl_nodes)
    # 当前文件的 pinctrl-N 引用: (node_path, index) → (filepath, lineno, refs)
    all_usages = {}
    for u in dts.node_usages:
        all_usages[(u.node_path, u.pinctrl_index)] = (u.filepath, u.lineno, u.refs)

    for inc_path in dts.includes:
        sub_pinctrl, sub_usages = collect_all(inc_path, visited)
        # 合并: 后出现的覆盖先出现的 (设备树覆盖语义)
        all_pinctrl.update(sub_pinctrl)
        all_usages.update(sub_usages)

    return all_pinctrl, all_usages


# ============================================================
# 冲突检测
# ============================================================

def find_conflicts(all_pinctrl, all_usages):
    """检测 pinctrl-N 的 pin 冲突。
    all_usages: dict[(node_path, pinctrl_index) → (filepath, lineno, refs)]
    已经过设备树覆盖处理: 同一节点路径、同一状态索引只保留最后出现的引用。

    冲突判定:
      - 按 pinctrl 状态索引分组比较 (pinctrl-0 之间、pinctrl-1 之间、... 各自独立)。
      - 不同状态 (如 pinctrl-0 vs pinctrl-1) 天然允许共用 pin, 不算冲突。
      - 对同一状态、同一个 GPIO pin, 只有当不同节点引用了不同的 ref_label 时才冲突。
      - 多个节点引用同一 ref_label (共享同一份配置) 视为正常。
    """
    from collections import defaultdict

    # (pinctrl_index, pin_key) → { ref_label → [(node_path, filepath, lineno), ...] }
    pin_owners = defaultdict(lambda: defaultdict(list))

    for (node_path, pinctrl_index), (filepath, lineno, refs) in all_usages.items():
        for ref_label in refs:
            pn = all_pinctrl.get(ref_label)
            if not pn:
                continue
            for pe in pn.pins:
                pin_owners[(pinctrl_index, pe.key)][ref_label].append(
                    (node_path, filepath, lineno))

    conflicts = {}
    for (pinctrl_index, pin_key), ref_map in pin_owners.items():
        # 只有当同一个状态、同一个 pin 被多个不同的 ref_label 占用时才冲突
        if len(ref_map) >= 2:
            owners = []
            for ref_label, entries in ref_map.items():
                # 每个 ref_label 取第一个节点作为代表 (同一 label 的多个节点视为共享)
                node_path, filepath, lineno = entries[0]
                owners.append((node_path, filepath, lineno, ref_label))
            conflicts[(pinctrl_index, pin_key)] = owners

    return conflicts


# ============================================================
# 输出
# ============================================================

def _pin_key_to_name(pin_key):
    bank, pin = pin_key
    return f"GPIO{bank}_RK_P{chr(ord('A') + pin // 8)}{pin % 8}"


def print_conflicts(conflicts, all_pinctrl, base_dir):
    if not conflicts:
        print("未发现 pinctrl 冲突。")
        return

    print(f"\n{'='*80}")
    print(f"  pinctrl GPIO Pin 冲突报告")
    print(f"{'='*80}")

    for (pinctrl_index, pin_key), owners in sorted(conflicts.items()):
        pin_name = _pin_key_to_name(pin_key)
        print(f"\n[冲突] pinctrl-{pinctrl_index} {pin_name} (bank={pin_key[0]}, pin={pin_key[1]}) 被以下节点同时占用:")
        print(f"{'-'*60}")
        for node_path, filepath, lineno, ref_label in owners:
            rel_path = os.path.relpath(filepath, base_dir) if os.path.isabs(filepath) else filepath
            print(f"  节点: {node_path}")
            print(f"  引用: &{ref_label}")
            print(f"  位置: {rel_path}:{lineno}")
            pn = all_pinctrl.get(ref_label)
            if pn:
                for pe in pn.pins:
                    if pe.key == pin_key:
                        print(f"  配置: bank={pe.bank}, pin={pe.pin}, func={pe.func}, bias={pe.bias}")
            print()

    print(f"{'='*80}")
    print(f"  共发现 {len(conflicts)} 个 GPIO pin 冲突")


def print_summary(all_pinctrl, all_usages):
    print(f"\n{'='*80}")
    print(f"  pinctrl 使用情况汇总")
    print(f"{'='*80}")
    print(f"  共 {len(all_pinctrl)} 个 pinctrl 节点定义")
    print(f"  共 {len(all_usages)} 个 pinctrl-N 引用 (覆盖后)")

    from collections import defaultdict
    # 统计各状态索引的引用数量
    index_count = defaultdict(int)
    pin_usage_count = defaultdict(int)
    for (node_path, pinctrl_index), (filepath, lineno, refs) in all_usages.items():
        index_count[pinctrl_index] += 1
        for ref_label in refs:
            pn = all_pinctrl.get(ref_label)
            if pn:
                for pe in pn.pins:
                    pin_usage_count[(pinctrl_index, pe.key)] += 1

    for idx in sorted(index_count):
        print(f"  pinctrl-{idx}: {index_count[idx]} 个引用")

    print(f"  共 {len(pin_usage_count)} 个 (状态, GPIO pin) 被引用")
    multi = [(k, v) for k, v in pin_usage_count.items() if v >= 2]
    if multi:
        print(f"  其中 {len(multi)} 个 (状态, pin) 被多次引用 (潜在冲突)")
    print()


def print_all_pins(all_pinctrl, all_usages, base_dir):
    from collections import defaultdict
    pin_owners_all = defaultdict(list)
    for (node_path, pinctrl_index), (filepath, lineno, refs) in all_usages.items():
        for ref_label in refs:
            pn = all_pinctrl.get(ref_label)
            if not pn:
                continue
            for pe in pn.pins:
                rel = os.path.relpath(filepath, base_dir)
                pin_owners_all[(pinctrl_index, pe.key)].append(
                    f"{node_path} <- &{ref_label} ({rel}:{lineno})")

    print(f"\n{'='*80}")
    print(f"  所有 pinctrl-N 占用的 GPIO Pin")
    print(f"{'='*80}")
    for (pinctrl_index, pin_key), owners in sorted(pin_owners_all.items()):
        pin_name = _pin_key_to_name(pin_key)
        print(f"  pinctrl-{pinctrl_index} {pin_name}:")
        for o in owners:
            print(f"    - {o}")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='设备树 pinctrl 冲突检测工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 check_pinctrl_conflict.py rk3588-nvr-demo-v10-ipc-4x-linux.dts
  python3 check_pinctrl_conflict.py -v rk3588-evb1-lp4-v10-linux.dts
  python3 check_pinctrl_conflict.py --summary-only rk3588-nvr-demo.dts
  python3 check_pinctrl_conflict.py --all-pins rk3588-nvr-demo-v10-ipc-4x-linux.dts
        """
    )
    parser.add_argument('dts_file', help='主 dts 文件路径')
    parser.add_argument('-v', '--verbose', action='store_true', help='显示详细信息')
    parser.add_argument('--summary-only', action='store_true',
                        help='仅显示汇总信息')
    parser.add_argument('--all-pins', action='store_true',
                        help='列出所有被 pinctrl-N 占用的 pin 及所属节点')

    args = parser.parse_args()

    entry = args.dts_file
    base_dir = os.path.dirname(os.path.abspath(entry))

    if not os.path.isfile(entry):
        print(f"错误: 文件不存在 - {entry}", file=sys.stderr)
        sys.exit(1)

    print(f"解析设备树: {entry}")
    all_pinctrl, all_usages = collect_all(entry)

    print(f"解析完成: {len(all_pinctrl)} 个 pinctrl 定义, {len(all_usages)} 个 pinctrl-N 引用 (覆盖后)")

    if args.summary_only:
        print_summary(all_pinctrl, all_usages)
        sys.exit(0)

    conflicts = find_conflicts(all_pinctrl, all_usages)
    print_conflicts(conflicts, all_pinctrl, base_dir)

    if args.verbose:
        print_summary(all_pinctrl, all_usages)

    if args.all_pins:
        print_all_pins(all_pinctrl, all_usages, base_dir)


if __name__ == '__main__':
    main()

