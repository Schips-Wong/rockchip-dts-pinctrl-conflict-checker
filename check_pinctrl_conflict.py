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


class GpioUsage:
    """一个设备节点通过 *-gpios / gpio 属性直接引用的 GPIO pin"""
    def __init__(self, node_path, prop_name, filepath, lineno):
        self.node_path = node_path
        self.prop_name = prop_name      # 属性名, 如 "enable-gpios" / "reset-gpios" / "gpio"
        self.filepath = filepath
        self.lineno = lineno
        self.pins = []                  # (bank, pin) 列表, 每个元素为 (bank, pin)

    def __repr__(self):
        return f"GpioUsage({self.node_path}, {self.prop_name}, pins={self.pins})"


# ============================================================
# RK3588 pin 命名解析 (bank, pin)
# ============================================================

# Rockchip 平台: RK_FUNC_GPIO 宏表示 func=0 (GPIO 功能, 不做外设复用)
RK_FUNC_GPIO = 0

# 可识别的 GPIO 控制器引用名 → bank 编号 (rk3588: gpio0~gpio4 对应 bank 0~4)
GPIO_BANK_MAP = {}
for _b in range(0, 8):
    GPIO_BANK_MAP[f'gpio{_b}'] = _b


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
        self.includes = []          # [(lineno, inc_path), ...] 保持出现顺序
        self.pinctrl_nodes = {}     # label → PinctrlNode
        self.node_usages = []       # NodeUsage 列表
        self.gpio_usages = []       # GpioUsage 列表
        self.node_status = {}       # node_path → status 字符串 ("okay"/"disabled"/...)

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

        # --- 第二步: 提取 #include (记录行号, 用于精确模拟展开顺序) ---
        for idx, line in enumerate(cleaned_lines):
            m = re.match(r'#include\s+[<"]([^>"]+\.dts[^>"]*)[>"]', line)
            if m:
                inc_name = m.group(1)
                inc_path = os.path.normpath(os.path.join(base_dir, inc_name))
                self.includes.append((idx + 1, inc_path))

        # --- 第三步: 解析节点结构 ---
        self._parse_nodes(cleaned_lines)

    def _parse_nodes(self, lines):
        """
        逐行解析节点层级和 pinctrl 定义 / pinctrl-0 引用。

        节点身份规则（与设备树 overlay 语义保持一致）:
          - 带标签的定义 `label: node@addr {` 以 label 作为稳定身份，
            这样后面的 `&label { ... }` 覆盖能落到同一个节点上；
          - `&label { ... }` 覆盖同样以 label 作为身份；
          - 无标签节点使用父路径 + 节点名。

        旧实现只识别 `&label {`，遇到 `label: node {` 时不会入栈，
        导致随后的 `};` 把父节点提前弹出、后续节点被错误归到根节点。
        """
        # 栈元素: (node_path, label, lineno)
        node_stack = []
        inside_pinctrl = False
        pinctrl_depth = 0

        # pinctrl 子节点收集状态
        current_label = None
        current_lineno = 0
        collecting = False
        pending_lines = []

        def current_path():
            return node_stack[-1][0] if node_stack else '/'

        for i, line in enumerate(lines):
            lineno = i + 1
            stripped = line.strip()
            if not stripped:
                continue
            # 预处理指令（#include 已单独处理）不参与节点解析
            if stripped.startswith('#'):
                continue

            # --- 节点结束 ---
            if re.fullmatch(r'}\s*;', stripped):
                if collecting and pending_lines:
                    self._flush_pins(current_label, current_lineno, pending_lines)
                    collecting = False
                    pending_lines = []
                if node_stack:
                    node_stack.pop()
                if inside_pinctrl and len(node_stack) < pinctrl_depth:
                    inside_pinctrl = False
                    pinctrl_depth = 0
                continue

            # --- 节点开始（支持 label: node、&label、&{/path}） ---
            overlay_path_m = re.match(r'^&?\{(/[^}]*)\}\s*\{\s*$', stripped)
            node_start_m = re.match(
                r'^(?:([A-Za-z_]\w*)\s*:\s*)?'
                r'(&?[A-Za-z0-9_@\-\.\,\+/]+)\s*\{\s*$',
                stripped)

            if overlay_path_m:
                node_name = overlay_path_m.group(1)
                node_stack.append((node_name, None, lineno))
                continue

            if node_start_m:
                label = node_start_m.group(1)
                raw_name = node_start_m.group(2)
                if raw_name.startswith('&'):
                    name = raw_name[1:]
                    node_path = name
                elif raw_name == '/':
                    name = '/'
                    node_path = '/'
                else:
                    name = raw_name
                    if label:
                        # 标签是全局身份，保证 &label 覆盖能合并到定义节点
                        node_path = label
                    else:
                        parent = node_stack[-1][0] if node_stack else '/'
                        node_path = name if parent in ('', '/') else parent + '/' + name

                node_stack.append((node_path, label, lineno))

                base_name = name.split('@')[0]
                if base_name.startswith('pinctrl') or label == 'pinctrl':
                    inside_pinctrl = True
                    pinctrl_depth = len(node_stack)
                continue

            # --- pinctrl 内部: 收集 pinmux 子节点 ---
            if inside_pinctrl:
                if collecting:
                    pending_lines.append(stripped)
                    if stripped.rstrip().endswith(';'):
                        self._flush_pins(current_label, current_lineno, pending_lines)
                        collecting = False
                        pending_lines = []
                    continue

                if 'rockchip,pins' in stripped:
                    # 找到最近的带标签祖先，作为这组 pin 的 label
                    for _path, label, label_lineno in reversed(node_stack):
                        if label:
                            current_label = label
                            current_lineno = label_lineno
                            collecting = True
                            pending_lines = [stripped]
                            if stripped.rstrip().endswith(';'):
                                self._flush_pins(current_label, current_lineno, pending_lines)
                                collecting = False
                                pending_lines = []
                            break
                    continue

                # pinctrl 组内部的其他属性不需要当作消费者处理
                continue

            # --- 任意节点内部: 收集 status 属性 ---
            status_m = re.match(r'status\s*=\s*"([^"]+)"\s*;', stripped)
            if status_m:
                status_val = status_m.group(1)
                self.node_status[current_path()] = (lineno, status_val)
                continue

            # --- 任意节点内部: 收集 pinctrl-N 引用 (N = 0, 1, 2, ...) ---
            # 单行格式
            pinctrl_m = re.match(r'pinctrl-(\d+)\s*=\s*<(.+)>;', stripped)
            if pinctrl_m:
                pinctrl_index = int(pinctrl_m.group(1))
                refs_str = pinctrl_m.group(2)
                refs = re.findall(r'&([\w]+)', refs_str)
                usage = NodeUsage(current_path(), self.filepath, lineno, pinctrl_index)
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
                usage = NodeUsage(current_path(), self.filepath, lineno, pinctrl_index)
                usage.refs = refs
                self.node_usages.append(usage)
                continue

            # --- 任意节点内部: 收集 *-gpios / gpio 属性直接引用的 GPIO ---
            # 属性名包含 gpio (enable-gpios / reset-gpios / irq-gpio / gpio / gpios / xxx,rst-gpio 等)
            # 值格式: <&gpioX RK_PY flags> (可能多个, 逗号分隔, 可能跨行)
            gpio_m = re.match(r'([\w\-,]*(?:gpio|GPIO)[\w\-]*)\s*=\s*(.+)', stripped)
            if gpio_m:
                prop_name = gpio_m.group(1)
                value_str = gpio_m.group(2).rstrip(';')

                # 跨行续写: 若未闭合则继续拼接
                if value_str.count('<') > value_str.count('>'):
                    j = i + 1
                    while j < len(lines):
                        cont = lines[j].strip()
                        value_str += ' ' + cont.rstrip(';')
                        if value_str.count('<') <= value_str.count('>'):
                            break
                        j += 1

                # 提取 <&gpioX RK_PY flags>，也兼容直接用数字写 pin 的 <&gpioX 5 flags>
                pins = []
                for g in re.findall(r'&(gpio\d+)\s+(RK_P[A-D]\d+|\d+)\b', value_str):
                    gbank = GPIO_BANK_MAP.get(g[0])
                    if g[1].isdigit():
                        gpin = int(g[1])
                        if gpin > 31:
                            continue
                    else:
                        gpin = rk_pin_to_number(g[1])
                    if gbank is not None and gpin is not None:
                        pins.append((gbank, gpin))

                if pins:
                    gpio_usage = GpioUsage(current_path(), prop_name, self.filepath, lineno)
                    gpio_usage.pins = pins
                    self.gpio_usages.append(gpio_usage)
                continue

    def _flush_pins(self, label, lineno, pin_lines):
        """解析缓存的 rockchip,pins 行"""
        if not label:
            return
        combined = ' '.join(pin_lines)
        combined = re.sub(r'rockchip\s*,\s*pins\s*=\s*', '', combined)
        combined = combined.rstrip(';')

        pin_entries = re.findall(
            r'<\s*(\d+)\s+(RK_P[A-D]\d+)\s+([\w]+)\s+(&?\w+)\s*>',
            combined)

        node = self.pinctrl_nodes.get(label)
        if not node:
            node = PinctrlNode(label, self.filepath, lineno)
            self.pinctrl_nodes[label] = node

        for bank_str, pin_name, func_str, bias in pin_entries:
            bank = int(bank_str)
            pin_num = rk_pin_to_number(pin_name)
            if pin_num is None:
                continue
            # func 可能是数字, 也可能是 RK_FUNC_GPIO 等宏
            if func_str == 'RK_FUNC_GPIO':
                func = RK_FUNC_GPIO
            else:
                try:
                    func = int(func_str)
                except ValueError:
                    func = func_str  # 无法识别的宏, 保留字符串
            node.pins.append(PinEntry(bank, pin_num, func, bias, self.filepath, lineno))


# ============================================================
# 全局数据收集: 递归解析 #include 树
# ============================================================

def collect_all(entry_dts, warnings=None, files=None):
    """
    递归解析 #include 链，收集 pinctrl 定义、pinctrl-N 引用、gpio 属性引用和节点状态。

    设备树覆盖语义:
      设备树中 #include 把被包含文件的内容展开到 include 语句所在位置,
      因此节点的有效定义取决于它们在"展开后"的先后顺序——后出现的覆盖先出现的。
      用全局递增的顺序号 (order) 精确模拟这一顺序:
        - 文件内按行号顺序, include 语句和被包含文件内容插入到 include 行位置,
        - 同一 key 后出现的 (order 更大) 覆盖先出现的 (order 更小)。

    warnings: 可选的 list, 用于收集解析警告 (文件缺失等), 供 GUI 展示。
    files:    可选的 list, 用于收集实际解析到的文件路径。

    返回: (all_pinctrl, all_usages, all_gpio_usages, all_node_status)
    """
    result = _collect_internal(entry_dts, set(), [0], warnings, files)
    return result


def _collect_internal(entry_dts, visited, order_counter, warnings=None, files=None):
    """collect_all 的内部实现, 返回已剥掉 order 包装的结果。

    返回值结构: (all_pinctrl, all_usages, all_gpio_usages, all_node_status)
    其中 all_pinctrl: label → PinctrlNode
         all_usages: (node_path, index) → (filepath, lineno, refs)
         all_gpio_usages: (node_path, prop_name) → GpioUsage
         all_node_status: node_path → status
    """
    entry_path = os.path.abspath(entry_dts)
    if entry_path in visited:
        return {}, {}, {}, {}
    visited.add(entry_path)

    if not os.path.isfile(entry_path):
        msg = f"文件不存在, 跳过: {entry_path}"
        print(f"[WARN] {msg}", file=sys.stderr)
        if warnings is not None:
            warnings.append(msg)
        return {}, {}, {}, {}

    if files is not None and entry_path not in files:
        files.append(entry_path)

    dts = DtsFile(entry_path)
    dts.parse()

    # 结果字典: key → (order, value), order 用于覆盖优先级判断
    all_pinctrl = {}        # label → (order, PinctrlNode)
    all_usages = {}         # (node_path, index) → (order, (filepath, lineno, refs))
    all_gpio_usages = {}    # (node_path, prop_name) → (order, GpioUsage)
    all_node_status = {}    # node_path → (order, status)

    def next_order():
        order_counter[0] += 1
        return order_counter[0]

    def merge_into(target, incoming):
        """将带 order 的 incoming 合并进 target (order 大者胜)"""
        for k, (o, v) in incoming.items():
            if k not in target or o > target[k][0]:
                target[k] = (o, v)

    # 按行号交错合并: 本文件自身的数据项 与 include 子文件内容
    events = []

    # include 事件
    for inc_lineno, inc_path in dts.includes:
        events.append((inc_lineno, 'include', inc_path))

    # 本文件自身数据项事件
    for label, pn in dts.pinctrl_nodes.items():
        events.append((pn.lineno, 'pinctrl_def', label, pn))
    for u in dts.node_usages:
        events.append((u.lineno, 'pinctrl_use', u))
    for g in dts.gpio_usages:
        events.append((g.lineno, 'gpio_use', g))
    for np, (s_lineno, s_val) in dts.node_status.items():
        events.append((s_lineno, 'status', np, s_val))

    # 按行号排序
    events.sort(key=lambda e: e[0])

    for ev in events:
        kind = ev[1]
        if kind == 'include':
            inc_path = ev[2]
            sub_pinctrl, sub_usages, sub_gpio, sub_status = _collect_internal(
                inc_path, visited, order_counter, warnings, files)
            # 子文件结果已经是剥掉 order 的, 需要重新包装 order
            # 但子文件内部已按顺序分配 order, 这里直接以它们的相对顺序重建 order
            for k, v in sub_pinctrl.items():
                all_pinctrl[k] = (next_order(), v)
            for k, v in sub_usages.items():
                all_usages[k] = (next_order(), v)
            for k, v in sub_gpio.items():
                all_gpio_usages[k] = (next_order(), v)
            for k, v in sub_status.items():
                all_node_status[k] = (next_order(), v)
        elif kind == 'pinctrl_def':
            label, pn = ev[2], ev[3]
            all_pinctrl[label] = (next_order(), pn)
        elif kind == 'pinctrl_use':
            u = ev[2]
            all_usages[(u.node_path, u.pinctrl_index)] = (
                next_order(), (u.filepath, u.lineno, u.refs))
        elif kind == 'gpio_use':
            g = ev[2]
            all_gpio_usages[(g.node_path, g.prop_name)] = (next_order(), g)
        elif kind == 'status':
            np, s_val = ev[2], ev[3]
            all_node_status[np] = (next_order(), s_val)

    # 剥掉 order 包装
    final_pinctrl = {k: v for k, (o, v) in all_pinctrl.items()}
    final_usages = {k: v for k, (o, v) in all_usages.items()}
    final_gpio = {k: v for k, (o, v) in all_gpio_usages.items()}
    final_status = {k: v for k, (o, v) in all_node_status.items()}

    return final_pinctrl, final_usages, final_gpio, final_status


# ============================================================
# 冲突检测
# ============================================================

def find_conflicts(all_pinctrl, all_usages, all_gpio_usages, all_node_status):
    """检测 pinctrl-N 与 *-gpios 的 GPIO pin 冲突。

    参数:
      all_pinctrl:     dict[label → PinctrlNode]
      all_usages:      dict[(node_path, pinctrl_index) → (filepath, lineno, refs)]
      all_gpio_usages: dict[(node_path, prop_name) → GpioUsage]
      all_node_status: dict[node_path → status 字符串]

    冲突判定:
      - 被 disabled 的节点 (status != "okay") 不视为冲突方, 其占用被跳过。
      - pinctrl vs pinctrl: 同一状态索引内, 不同节点引用不同 ref_label 且占用同一 pin
        → 冲突 (不同状态如 pinctrl-0 vs pinctrl-1 天然允许共用, 不算冲突)。
      - gpio vs gpio: 不同节点通过 *-gpios 占用同一 pin → 冲突。
      - pinctrl vs gpio: 某个 pin 既被 pinctrl 复用(或占用)又被 (其它节点的) *-gpios
        直接引用 → 冲突。
      - 豁免: 同一节点内部的多个占用 (pinctrl 与 *-gpios 配套) 视为正常, 不算冲突;
        多节点冲突时按节点聚合, 每个节点只保留一个代表。
    """
    from collections import defaultdict

    def is_disabled(node_path):
        # 设备树中未写 status 的节点默认是启用的（okay），
        # 只有显式写成非 okay/ok 的值才算禁用。
        status = all_node_status.get(node_path)
        return status is not None and status not in ('okay', 'ok')

    # 按 pin_key 分组, 收集所有"占用标签"
    # pinctrl 占用标签: 'p' + ref_label (同一 ref_label 被多个节点引用视为共享, 只算一个标签)
    # gpio 占用标签: 'g' + node_path + '#' + prop_name
    # pin_key → { 占用标签 → owner 详情 }
    pin_owners = defaultdict(dict)

    # --- 收集 pinctrl 占用 (按 ref_label 去重, 共享同一标签视为正常) ---
    for (node_path, pinctrl_index), (filepath, lineno, refs) in all_usages.items():
        if is_disabled(node_path):
            continue
        for ref_label in refs:
            pn = all_pinctrl.get(ref_label)
            if not pn:
                continue
            for pe in pn.pins:
                tag = ('p', ref_label)
                if tag not in pin_owners[pe.key]:
                    pin_owners[pe.key][tag] = {
                        'node_path': node_path, 'filepath': filepath,
                        'lineno': lineno, 'source': 'pinctrl',
                        'ref_label': ref_label, 'func': pe.func,
                    }

    # --- 收集 gpio 占用 ---
    for (node_path, prop_name), gpio_usage in all_gpio_usages.items():
        if is_disabled(node_path):
            continue
        for (bank, pin) in gpio_usage.pins:
            tag = ('g', node_path, prop_name)
            if tag not in pin_owners[(bank, pin)]:
                pin_owners[(bank, pin)][tag] = {
                    'node_path': node_path, 'filepath': gpio_usage.filepath,
                    'lineno': gpio_usage.lineno, 'source': 'gpio',
                    'ref_label': prop_name, 'func': None,
                }

    conflicts = {}
    for pin_key, owner_map in pin_owners.items():
        owners = list(owner_map.values())
        if len(owners) < 2:
            continue

        # 豁免: 同一节点内部的占用视为配套使用, 不算冲突。
        # 同一节点内 pinctrl 负责 pinmux、*-gpios 负责逻辑操作, 二者指向同一 pin 是正常的。
        node_paths = set(o['node_path'] for o in owners)
        if len(node_paths) == 1:
            continue

        # 按 node_path 聚合: 同一节点的多个占用 (pinctrl + gpio 配套) 归为一个代表。
        # 每个节点优先取 pinctrl 引用作代表, 没有 pinctrl 则取 gpio 属性。
        by_node = {}
        for o in owners:
            np = o['node_path']
            if np not in by_node:
                by_node[np] = o
            else:
                # 已有代表时, 若当前是 pinctrl 且原代表是 gpio, 则用 pinctrl 替换
                if o['source'] == 'pinctrl' and by_node[np]['source'] == 'gpio':
                    by_node[np] = o

        conflict_owners = [
            (o['node_path'], o['filepath'], o['lineno'],
             o['source'], o['ref_label'], o['func'])
            for o in by_node.values()
        ]
        conflicts[pin_key] = conflict_owners

    return conflicts


# ============================================================
# 输出
# ============================================================

def _pin_key_to_name(pin_key):
    bank, pin = pin_key
    return f"GPIO{bank}_RK_P{chr(ord('A') + pin // 8)}{pin % 8}"


def print_conflicts(conflicts, all_pinctrl, base_dir):
    if not conflicts:
        print("未发现 pinctrl / gpio 冲突。")
        return

    print(f"\n{'='*80}")
    print(f"  GPIO Pin 冲突报告 (pinctrl + *-gpios)")
    print(f"{'='*80}")

    for pin_key, owners in sorted(conflicts.items()):
        pin_name = _pin_key_to_name(pin_key)
        print(f"\n[冲突] {pin_name} (bank={pin_key[0]}, pin={pin_key[1]}) 被以下节点同时占用:")
        print(f"{'-'*60}")
        for owner in owners:
            node_path, filepath, lineno, source, ref_label, func = owner
            rel_path = os.path.relpath(filepath, base_dir) if os.path.isabs(filepath) else filepath
            print(f"  节点: {node_path}")
            if source == 'pinctrl':
                print(f"  引用: &{ref_label} (pinctrl)")
                pn = all_pinctrl.get(ref_label)
                if pn:
                    for pe in pn.pins:
                        if pe.key == pin_key:
                            print(f"  配置: bank={pe.bank}, pin={pe.pin}, func={pe.func}, bias={pe.bias}")
            else:
                print(f"  属性: {ref_label} (gpio)")
            print(f"  位置: {rel_path}:{lineno}")
            print()

    print(f"{'='*80}")
    print(f"  共发现 {len(conflicts)} 个 GPIO pin 冲突")


def print_summary(all_pinctrl, all_usages, all_gpio_usages):
    print(f"\n{'='*80}")
    print(f"  pinctrl / gpio 使用情况汇总")
    print(f"{'='*80}")
    print(f"  共 {len(all_pinctrl)} 个 pinctrl 节点定义")
    print(f"  共 {len(all_usages)} 个 pinctrl-N 引用 (覆盖后)")
    print(f"  共 {len(all_gpio_usages)} 个 *-gpios 属性引用 (覆盖后)")

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


def print_all_pins(all_pinctrl, all_usages, all_gpio_usages, base_dir):
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
                    f"{node_path} <- &{ref_label} (pinctrl, {rel}:{lineno})")

    for (node_path, prop_name), gpio_usage in all_gpio_usages.items():
        for (bank, pin) in gpio_usage.pins:
            rel = os.path.relpath(gpio_usage.filepath, base_dir)
            pin_owners_all[(None, (bank, pin))].append(
                f"{node_path} <- {prop_name} (gpio, {rel}:{gpio_usage.lineno})")

    print(f"\n{'='*80}")
    print(f"  所有 pinctrl-N / *-gpios 占用的 GPIO Pin")
    print(f"{'='*80}")
    for (pinctrl_index, pin_key), owners in sorted(pin_owners_all.items(), key=lambda x: x[0][1]):
        pin_name = _pin_key_to_name(pin_key)
        prefix = f"pinctrl-{pinctrl_index}" if pinctrl_index is not None else "gpio"
        print(f"  [{prefix}] {pin_name}:")
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
    all_pinctrl, all_usages, all_gpio_usages, all_node_status = collect_all(entry)

    print(f"解析完成: {len(all_pinctrl)} 个 pinctrl 定义, "
          f"{len(all_usages)} 个 pinctrl-N 引用, {len(all_gpio_usages)} 个 *-gpios 引用 (覆盖后)")

    if args.summary_only:
        print_summary(all_pinctrl, all_usages, all_gpio_usages)
        sys.exit(0)

    conflicts = find_conflicts(all_pinctrl, all_usages, all_gpio_usages, all_node_status)
    print_conflicts(conflicts, all_pinctrl, base_dir)

    if args.verbose:
        print_summary(all_pinctrl, all_usages, all_gpio_usages)

    if args.all_pins:
        print_all_pins(all_pinctrl, all_usages, all_gpio_usages, base_dir)


if __name__ == '__main__':
    main()

