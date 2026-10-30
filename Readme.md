# Rockchip 设备树 GPIO 冲突检测器

一个基于设备树 `#include` 包含关系 / DTB phandle 的 GPIO Pin 冲突检测工具：

- 命令行版本：`check_pinctrl_conflict.py`
- 图形界面版本：`gui.py`（推荐，支持 `.dts`、`.dtsi` 和编译后的 `.dtb`）

它解析主 dts 及其递归 `#include` 的所有 dts/dtsi 文件，或者直接读取编译后的 DTB，收集各节点的 `pinctrl-N`（`pinctrl-0` / `pinctrl-1` / ...）引用以及 `*-gpios` 属性（`enable-gpios`、`reset-gpios`、`goodix,irq-gpio` 等），将它们展开为具体的 GPIO `(bank, pin)` 占用，然后检测不同节点之间是否存在 pin 冲突。

![图形界面](../gui-screenshot.png)

## 图形界面（推荐）

### 启动

在 Windows 上直接双击：

```text
run_gui.bat
```

或者用命令行启动（可同时指定要打开的设备树）：

```bash
python gui.py
python gui.py rk3588-nvr-demo-v10-ipc-4x-linux.dts
python gui.py rk3588-nvr-demo-v10-ipc-4x-linux.dtb
```

只依赖 Python 标准库（`tkinter`），无需安装第三方包。

### 功能

- **打开设备树**：支持 `.dts` / `.dtsi` / `.dtb`。
  - DTS 会递归解析 `#include` 链；
  - DTB 直接读取二进制 FDT，通过 phandle 精确解析 `pinctrl-N`，不依赖 `dtc`。
- **冲突列表**：按 GPIO pin 汇总冲突，左侧列表显示引脚、节点数、占用节点；点击一行可在右侧查看：
  - 每个节点通过哪个 `pinctrl-N` 组或 `*-gpios` 属性占用；
  - 复用功能号 `func`、上下拉 `bias`；
  - 文件与行号（DTB 没有行号信息，显示节点路径）；
  - 节点状态是否为启用。
- **全部占用**：列出所有 pinctrl / gpio 占用记录，支持按引脚、节点、属性过滤；双击一行可把该引脚填入查询框。
- **GPIO 查询**：在底部编辑框输入一个 GPIO，立即判断它是否已经被使用、被哪些节点使用、是否存在冲突。
- **导出报告**：导出 Markdown 格式的冲突报告，可选包含全部引脚占用。
- **解析日志**：显示实际解析到的文件、解析耗时和警告。

### GPIO 查询格式

| 输入 | 含义 |
|------|------|
| `GPIO4_RK_PB2` | bank 4 的 RK_PB2（pin 10） |
| `GPIO4_PB2` / `GPIO4_B2` | 同上，简写 |
| `4 RK_PB2` | 同上 |
| `4 10` / `4,10` / `gpio4 10` / `bank4 pin10` | bank 4，pin 10 |
| `RK_PB2` / `PB2` | 查询所有 bank 的同一位置 |

查询结果会区分：

- **未被使用**：没有任何节点引用；
- **已被使用**：列出所有启用节点及其来源；
- **存在冲突**：该 pin 被多个节点用不同的 pinctrl 组 / GPIO 属性占用；
- **只被已禁用节点引用**：引脚在设备树里有定义，但相关节点 `status` 不是 `okay`，实际未启用。

## 冲突判定规则

对同一个 GPIO pin：

1. **pinctrl vs pinctrl**：不同节点引用**不同的** pinctrl 组，并且同一状态索引内占用同一 pin → 冲突。
   - 多个节点引用**同一个** pinctrl 组属于共享配置，不算冲突。
   - `pinctrl-0` 与 `pinctrl-1` 等不同状态之间天然允许共用 pin，不算冲突。
2. **gpio vs gpio**：不同节点通过 `*-gpios` 占用同一 pin → 冲突。
3. **pinctrl vs gpio**：pin 既被 pinctrl 复用，又被其他节点的 `*-gpios` 引用 → 冲突。
4. **同一节点豁免**：同一节点内部 pinctrl 与 `*-gpios` 配套使用（例如 pinctrl 把 pin 配成 GPIO 模式，`enable-gpios` 再操作它）不算冲突。
5. **禁用节点**：`status` 不是 `okay` / `ok` 的节点不参与冲突判定；未写 `status` 的节点按启用处理。
6. **共享组完整列出**：当一个 pin 上有多个节点共享同一 pinctrl 组、又有第三个节点来争用时，冲突详情会列出所有实际占用该 pin 的启用节点。

## 命令行用法

```bash
# 基本冲突检测
python check_pinctrl_conflict.py <主dts文件>

# 显示冲突 + 汇总信息
python check_pinctrl_conflict.py -v <主dts文件>

# 仅显示汇总信息
python check_pinctrl_conflict.py --summary-only <主dts文件>

# 列出所有被 pinctrl-N / *-gpios 占用的 pin 及所属节点
python check_pinctrl_conflict.py --all-pins <主dts文件>
```

示例：

```bash
python check_pinctrl_conflict.py rk3588-nvr-demo-v10-ipc-4x-linux.dts
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `dts_file` | 主 dts 文件路径（必填） |
| `-v`, `--verbose` | 显示详细信息（冲突报告 + 汇总） |
| `--summary-only` | 仅显示汇总信息，不显示具体冲突 |
| `--all-pins` | 列出所有被 pinctrl-N / *-gpios 占用的 pin 及所属节点 |

## 实现原理

### DTS 源文件

1. **去除注释**：`/* */` 跨行块注释与 `//` 行注释，保持行号不变。
2. **提取 `#include`**：递归展开包含链，深度优先遍历，后出现的定义覆盖先出现的定义。
3. **解析节点结构**：
   - 同时识别 `label: node@addr {`、`&label {` 和 `&{/path} {`；
   - 带标签的节点以 label 作为稳定身份，使后面的 `&label` 覆盖能合并到同一个节点；
   - 识别 `&pinctrl { ... }` 内部的 pinctrl 组（`label: group { rockchip,pins = <...>; }`）。
4. **收集占用**：
   - 任意节点的 `pinctrl-N = <&group ...>;`（支持跨行）；
   - 任意节点的 `*-gpios` / `gpio` 属性（支持 `snps,reset-gpio` 这类带厂商前缀的属性名，支持跨行）。
5. **检测冲突**：把引用展开为 `(bank, pin)`，按上述规则判定。

### DTB 二进制

1. 读取 FDT header、struct 块和 strings 块，构造节点树。
2. 解析 `phandle` / `linux,phandle`、`/aliases`、`/__symbols__`：
   - `pinctrl-N` 的值是 phandle 数组，解析到对应 pinctrl 组；
   - pinctrl 组的 `rockchip,pins` 每 4 个 cell 为 `(bank, pin, func, bias-phandle)`；
   - `*-gpios` 的值包含 GPIO 控制器 phandle、pin 号和 flags，按控制器的 `#gpio-cells` 解析；
   - 通过 `/aliases/gpioN`（优先）或 `gpio-ranges` 确定控制器对应的 bank；
   - 通过 `/__symbols__` 把节点路径还原成 `i2c7`、`pcfg_pull_none` 这类标签显示。
3. 使用 DTB 中最终生效的 `status`，并沿父链继承禁用状态。

DTB 是编译后的最终状态，推荐用于最终确认；DTS 源文件解析是文本级的，适合在修改设备树时快速检查。

## 文件结构

```text
rockchip-dts-pinctrl-conflict-checker/
├── gui.py                      # 图形界面入口
├── analyzer.py                 # DTS / DTB 统一分析入口
├── model.py                    # 数据模型、冲突判定、GPIO 查询解析
├── dtb_parser.py               # DTB 二进制解析
├── report.py                   # Markdown 报告导出
├── check_pinctrl_conflict.py   # 命令行版本 / DTS 文本解析核心
├── run_gui.bat                 # Windows 图形界面启动脚本
└── Readme.md
tests/
├── fdt_builder.py              # 测试用最小 DTB 构造器
└── test_analyzer.py            # 自动化测试
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

- GPIO 查询输入的各种格式；
- 真实 RK3588 DTS 与 DTB 的组数、引用数、冲突数一致性；
- `label: node {` 节点栈解析（不产生根节点 `/` 误报）；
- 合成 DTB 的 pinctrl 冲突、GPIO 直连冲突、父子节点禁用继承、`/aliases` bank 映射；
- Markdown 报告内容。

## 限制

- DTS 解析基于正则与文本，不使用 DTC 编译器；对宏展开、`#if` 条件编译内的复杂分支可能不完整，最终结论建议用 `.dtb` 复核。
- DTB 解析针对标准 flattened device tree；不负责把 DTB 反编译成 DTS 文本。
- 主要针对 Rockchip 平台的 `rockchip,pins` 格式、`RK_Pxx`（`RK_PA0` ~ `RK_PD7`）命名，以及 `gpio0` ~ `gpio7` 控制器命名。
- DTB 中 GPIO 控制器到 bank 的映射依赖 `/aliases/gpioN`；没有 aliases 时尝试用 `gpio-ranges` 推断，仍无法确定时会在解析日志中给出警告。
- 假定 `pinctrl-N` 与 `*-gpios` 属性遵循“后者完全覆盖前者”的替换语义。
