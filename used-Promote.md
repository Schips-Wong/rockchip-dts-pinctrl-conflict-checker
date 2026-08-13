## v1

### a

我希望实现对指定dts节点里面`pinctrl-0` 的冲突关系进行检测

例如，以主dts`rk3588-nvr-demo-v10-ipc-4x-linux.dts`为例，实际会发现`i2c7m3_xfer`中`rockchip,pins`（`4 RK_PB2`或`4 RK_PB3`）和其他节点冲突，编写py工具，将这些冲突找出来

2025-01-15：Python实现列出Linux内核所用设备树中的包含关系.mdtool.py

### b

从执行结果来看，其实某些结果是没有冲突的，但你写的程序却判断为冲突了，当对应的node_path引用了同一个`ref_label`，视为正常。

例如：

```
[冲突] GPIO0_RK_PA2 (bank=0, pin=2) 被以下节点同时占用:
------------------------------------------------------------
  节点: rk806single
  引用: &pmic_pins
  位置: rk3588-nvr-demo.dtsi:543
  配置: bank=0, pin=2, func=1, bias=&pcfg_pull_none

  节点: spi2
  引用: &pmic_pins
  位置: rk3588-rk806-single.dtsi:27
  配置: bank=0, pin=2, func=1, bias=&pcfg_pull_none
```

换句话说，我们关心的是这种冲突，因为它们引用标签不一致，一个是`i2c4m1_xfer
`，而另外一个是`hdmim0_tx1_sda
`：

```
[冲突] GPIO2_RK_PB4 (bank=2, pin=12) 被以下节点同时占用:
------------------------------------------------------------
  节点: i2c4
  引用: &i2c4m1_xfer
  位置: rk3588-schips-cam-6x---mipi5-4lane-dphy1-full-mode.dtsi:73
  配置: bank=2, pin=12, func=9, bias=&pcfg_pull_none_smt

  节点: hdmi1
  引用: &hdmim0_tx1_sda
  位置: schips-bsp-rk3588-hdmi.dtsi:52
  配置: bank=2, pin=12, func=4, bias=&pcfg_pull_none_drv_level_1_smt
```

