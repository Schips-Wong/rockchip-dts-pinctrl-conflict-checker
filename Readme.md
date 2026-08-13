# check pinctrl conflict.py

一个基于设备树 `#include` 包含关系的 `pinctrl-0` GPIO Pin 冲突检测工具。

它解析主 dts 及其递归 `#include` 的所有 dts/dtsi 文件，收集各节点的 `pinctrl-0` 属性，将引用的 pinctrl 组展开为具体的 GPIO `(bank, pin)` 占用，然后检测不同节点之间是否存在 pin 冲突。

## 背景

在 Rockchip 等平台的内核设备树中，节点通过 `pinctrl-0 = <&pinmux_group>` 声明它所占用的引脚复用配置，而 `pinmux_group` 内部通过 `rockchip,pins = <bank pin func bias>` 描述具体占用了哪些 GPIO 引脚。

当一个引脚被多个节点通过**不同的** pinctrl 组引用时，就会产生硬件上的引脚冲突。例如：

```dts
&pinctrl {
	i2c7 {
		/omit-if-no-ref/
		i2c7m3_xfer: i2c7m3-xfer {
			rockchip,pins =
				/* i2c7_scl_m3 */
				<4 RK_PB2 9 &pcfg_pull_none_smt>,
				/* i2c7_sda_m3 */
				<4 RK_PB3 9 &pcfg_pull_none_smt>;
		};
	};

	can1 {
		/omit-if-no-ref/
		can1m1_pins: can1m1-pins {
			rockchip,pins =
				/* can1_rx_m1 */
				<4 RK_PB2 12 &pcfg_pull_none>,
				/* can1_tx_m1 */
				<4 RK_PB3 12 &pcfg_pull_none>;
		};
	};
};

&i2c7 {
    pinctrl-0 = <&i2c7m3_xfer>;   /* 占用 GPIO4_RK_PB2 / GPIO4_RK_PB3 */
};

&can1 {
    pinctrl-0 = <&can1m1_pins>;   /* 占用 GPIO4_RK_PB2 / GPIO4_RK_PB3 */
};
```

此时 `i2c7m3_xfer` 与 `can1m1_pins` 引用的是不同的 pinctrl 标签，却争抢了相同的引脚，属于真实冲突。本工具正是为了自动找出这类问题。

## 功能

- 递归解析主 dts 的 `#include` 链（含 `.dts` / `.dtsi` 文件）。
- 收集所有 pinctrl 组定义（`rockchip,pins = <...>`）及其 `(bank, pin)` 占用。
- 收集所有节点的 `pinctrl-0` 引用。
- 遵循设备树**属性覆盖原则**：
  - 同一节点路径的 `pinctrl-0`，后出现的覆盖先出现的（同一文件内按行序，跨文件按 include 顺序）。
  - 同一 pinctrl 标签的 `rockchip,pins` 定义，后 include 的覆盖先 include 的。
- 冲突判定：对同一个 GPIO pin，仅当不同节点引用了**不同的** pinctrl 标签时才视为冲突；若多个节点引用的是同一个标签（共享同一份配置），视为正常。

## 用法

```bash
# 基本冲突检测
python3 check_pinctrl_conflict.py <主dts文件>

# 显示冲突 + 汇总信息
python3 check_pinctrl_conflict.py -v <主dts文件>

# 仅显示汇总信息
python3 check_pinctrl_conflict.py --summary-only <主dts文件>

# 列出所有被 pinctrl-0 占用的 pin 及所属节点
python3 check_pinctrl_conflict.py --all-pins <主dts文件>
```

### 示例

```bash
python3 check_pinctrl_conflict.py rk3588-nvr-demo-v10-ipc-4x-linux.dts
```

输出示例：

```
解析设备树: rk3588-nvr-demo-v10-ipc-4x-linux.dts

解析完成: 535 个 pinctrl 定义, 23 个 pinctrl-0 引用 (覆盖后)

================================================================================
  pinctrl-0 GPIO Pin 冲突报告
================================================================================

[冲突] GPIO4_RK_PB2 (bank=4, pin=10) 被以下节点同时占用:
------------------------------------------------------------
  节点: can1
  引用: &can1m1_pins
  位置: schips-bsp-rk3588-can.dtsi:11
  配置: bank=4, pin=10, func=12, bias=&pcfg_pull_none

  节点: i2c7
  引用: &i2c7m3_xfer
  位置: rk3588-schips-cam-6x---mipi0-4lane-dcphy0-as-dphy.dtsi:173
  配置: bank=4, pin=10, func=9, bias=&pcfg_pull_none_smt

================================================================================
  共发现 1 个 GPIO pin 冲突
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `dts_file` | 主 dts 文件路径（必填） |
| `-v`, `--verbose` | 显示详细信息（冲突报告 + 汇总） |
| `--summary-only` | 仅显示汇总信息，不显示具体冲突 |
| `--all-pins` | 列出所有被 pinctrl-0 占用的 pin 及所属节点 |

## 工作原理

1. **解析文件**：去除注释（`/* */` 与 `//`），提取 `#include` 语句。
2. **解析节点结构**：维护节点栈跟踪层级，识别：
   - `&pinctrl { ... }` 内部的 pinctrl 子节点（`label: name { rockchip,pins = ...; }`）。
   - 任意节点内部的 `pinctrl-0 = <&ref1 &ref2>;`（支持跨行写法）。
3. **递归展开 include**：深度优先遍历 include 树，遵循后覆盖前的原则合并结果。
4. **展开引脚占用**：将每个 `pinctrl-0` 引用映射为 `(bank, pin)` 集合。
5. **检测冲突**：对同一 `(bank, pin)`，若存在多个不同的引用标签，则报告冲突。

## 输出字段说明

- **节点**：引用该 pinctrl 组所在的节点路径（如 `i2c7`、`hdmi1`）。
- **引用**：节点 `pinctrl-0` 中引用的 pinctrl 标签（如 `&i2c7m3_xfer`）。
- **位置**：该引用所在的文件及行号。
- **配置**：冲突引脚的具体配置，含 bank、pin、func（功能复用号）、bias（上下拉配置）。

## 限制

- 基于正则与文本解析，未使用 DTC 编译器，对极端复杂的语法（如宏展开、`#if` 条件编译内的复杂分支）可能不完整。
- 主要针对 Rockchip 平台的 `RK_Pxx` 引脚命名（`RK_PA0` ~ `RK_PD7`）以及 `rockchip,pins` 格式。
- 假定 `pinctrl-0` 属性遵循"后者完全覆盖前者"的替换语义（与 DTC 对 phandle 数组属性的一致）。

