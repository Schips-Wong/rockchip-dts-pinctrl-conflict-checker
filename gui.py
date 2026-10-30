#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rockchip 设备树 GPIO 冲突检测器（图形界面）。

功能：
  1. 打开 .dts / .dtsi / .dtb；
  2. 列出当前的 pinctrl / GPIO pin 冲突关系；
  3. 在底部编辑框输入 GPIO，查询它是否已经被占用、被哪个节点占用。

仅使用 Python 标准库（tkinter），无需安装第三方依赖。
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from analyzer import (
    DTB_SOURCE_BINARY,
    DTB_SOURCE_DTS,
    SiblingDtsMissing,
    analyze,
    find_sibling_dts,
    is_dtb_file,
)
from model import (
    AnalysisResult,
    Pin,
    PinClaim,
    QueryError,
    QuerySpec,
    parse_gpio_query,
)
from report import build_markdown, default_report_name


APP_TITLE = "Rockchip 设备树 GPIO 冲突检测器"

# DTB 解析方式（下拉框显示文本）；默认使用同名 DTS 解析
MODE_SIBLING_DTS = "同名 DTS 解析"
MODE_BINARY = "DTB 二进制解析"
DTB_MODES = (MODE_SIBLING_DTS, MODE_BINARY)
DEFAULT_DTB_MODE = MODE_SIBLING_DTS

COLOR_TEXT = "#1f2937"
COLOR_MUTED = "#667085"
COLOR_RED = "#b42318"
COLOR_ORANGE = "#b54708"
COLOR_GREEN = "#067647"
COLOR_BLUE = "#175cd3"
COLOR_PANEL = "#f8fafc"


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _short_func(claim: PinClaim) -> str:
    if claim.func is None:
        return "—"
    return f"func={claim.func}"


def _kind_badge(kind: str) -> Tuple[str, str]:
    """返回 (徽标文字, 颜色)，用于提示本次结果来自哪种解析逻辑。"""
    if kind == "dtb":
        return "DTB", COLOR_BLUE
    if kind == "dtb_dts":
        return "DTB→DTS", COLOR_ORANGE
    return "DTS", COLOR_MUTED


def _status_text(claim: PinClaim) -> str:
    return "启用" if claim.active else f"禁用（{claim.status}）"


class DtsGuiApp:
    def __init__(
        self,
        root: tk.Tk,
        initial_path: Optional[str] = None,
        initial_query: Optional[str] = None,
        initial_dtb_mode: Optional[str] = None,
    ):
        self.root = root
        self.result: Optional[AnalysisResult] = None
        self.current_path: Optional[str] = None
        self.busy = False
        self._token = 0
        self._queue: "queue.Queue[Tuple[str, int, object]]" = queue.Queue()
        self._last_dir = os.getcwd()
        self._conflict_pins: Dict[str, Pin] = {}
        self._query_pins: Dict[str, Pin] = {}
        self._initial_query = initial_query
        self._initial_dtb_mode = initial_dtb_mode

        self.family = self._pick_font_family()
        self._configure_root()
        self._configure_style()
        self._build_ui()
        self._bind_shortcuts()
        self._set_empty_state()

        if self._initial_dtb_mode in DTB_MODES:
            self.dtb_mode_var.set(self._initial_dtb_mode)

        if initial_path:
            self.root.after(80, lambda: self.load_file(initial_path))

    # ========================================================
    # 初始化
    # ========================================================

    def _pick_font_family(self) -> str:
        available = set(tkfont.families(self.root))
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "TkDefaultFont"):
            if name in available:
                return name
        return "TkDefaultFont"

    def _configure_root(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1320x880")
        self.root.minsize(1024, 680)
        self.root.option_add("*Font", (self.family, 10))
        self.root.configure(background=COLOR_PANEL)

        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(font_name).configure(family=self.family, size=10)
            except tk.TclError:
                pass

        # 居中显示
        self.root.update_idletasks()
        width = 1320
        height = 880
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue

        style.configure("Treeview", rowheight=26, font=(self.family, 10), background="white")
        style.configure("Treeview.Heading", font=(self.family, 10, "bold"))
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", font=(self.family, 10, "bold"))
        style.configure("TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT)
        style.configure("Muted.TLabel", foreground=COLOR_MUTED)
        style.configure("Title.TLabel", font=(self.family, 12, "bold"), foreground=COLOR_TEXT)
        style.configure("Badge.TLabel", font=(self.family, 11, "bold"))
        style.configure("TLabelframe", background=COLOR_PANEL)
        style.configure("TLabelframe.Label", background=COLOR_PANEL, foreground=COLOR_TEXT)
        style.configure("Card.TFrame", background=COLOR_PANEL)

    def _build_ui(self) -> None:
        self._build_menu()
        self._build_toolbar()
        # 先 pack 底部查询面板，再 pack 可扩展的 Notebook，
        # 否则 Notebook 会先占满空间，把查询面板挤成 0 高度。
        self._build_query_panel()
        self._build_notebook()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="打开设备树…", accelerator="Ctrl+O", command=self.on_open)
        file_menu.add_command(label="重新解析", accelerator="F5", command=self.on_reparse)
        file_menu.add_separator()
        file_menu.add_command(label="导出冲突报告…", accelerator="Ctrl+S", command=self.on_export)
        file_menu.add_command(label="导出完整报告（含全部引脚）…", command=lambda: self.on_export(include_all=True))
        file_menu.add_separator()
        file_menu.add_command(label="退出", accelerator="Alt+F4", command=self._on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_command(label="冲突列表", command=lambda: self.notebook.select(self.tab_conflicts))
        view_menu.add_command(label="全部占用", command=lambda: self.notebook.select(self.tab_all))
        view_menu.add_command(label="解析日志", command=lambda: self.notebook.select(self.tab_log))
        view_menu.add_separator()
        view_menu.add_command(label="聚焦 GPIO 查询框", accelerator="Ctrl+F", command=self.focus_query)
        menubar.add_cascade(label="视图", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="使用说明", command=self.on_help)
        help_menu.add_command(label="关于", command=self.on_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)

    def _build_toolbar(self) -> None:
        outer = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        outer.pack(side="top", fill="x")
        outer.columnconfigure(2, weight=1)

        self.open_btn = ttk.Button(outer, text="打开设备树…", style="Accent.TButton", command=self.on_open)
        self.open_btn.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(outer, text="文件").grid(row=0, column=1, padx=(0, 6))

        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(outer, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.path_entry.bind("<Return>", lambda _e: self.on_reparse())

        self.reparse_btn = ttk.Button(outer, text="解析", command=self.on_reparse)
        self.reparse_btn.grid(row=0, column=3, padx=(0, 6))
        self.export_btn = ttk.Button(outer, text="导出报告", command=self.on_export)
        self.export_btn.grid(row=0, column=4, padx=(0, 8))

        ttk.Label(outer, text="DTB 解析方式").grid(row=0, column=5, padx=(0, 6))
        self.dtb_mode_var = tk.StringVar(value=DEFAULT_DTB_MODE)
        self.dtb_mode_combo = ttk.Combobox(
            outer,
            textvariable=self.dtb_mode_var,
            state="readonly",
            width=16,
            values=DTB_MODES,
        )
        self.dtb_mode_combo.grid(row=0, column=6, padx=(0, 8))
        self.dtb_mode_combo.bind("<<ComboboxSelected>>", self._on_dtb_mode_change)

        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=110)
        self.progress.grid(row=0, column=7)
        self.progress.grid_remove()

        summary = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        summary.pack(side="top", fill="x")
        summary.columnconfigure(2, weight=1)

        self.kind_badge = tk.Label(
            summary, text="未加载", font=(self.family, 9, "bold"),
            fg="white", bg=COLOR_MUTED, padx=8, pady=2,
        )
        self.kind_badge.grid(row=0, column=0, padx=(0, 8))
        self.file_label = ttk.Label(summary, text="尚未打开设备树文件", style="Title.TLabel")
        self.file_label.grid(row=0, column=1, columnspan=2, sticky="w")
        self.summary_label = ttk.Label(summary, text="", style="Muted.TLabel")
        self.summary_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 6))

        self.tab_conflicts = ttk.Frame(self.notebook)
        self.tab_all = ttk.Frame(self.notebook)
        self.tab_log = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_conflicts, text="  冲突列表  ")
        self.notebook.add(self.tab_all, text="  全部占用  ")
        self.notebook.add(self.tab_log, text="  解析日志  ")

        self._build_conflict_tab()
        self._build_all_tab()
        self._build_log_tab()

    def _build_conflict_tab(self) -> None:
        paned = ttk.PanedWindow(self.tab_conflicts, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.Frame(paned, padding=(0, 0, 4, 0))
        right = ttk.Frame(paned, padding=(4, 0, 0, 0))
        paned.add(left, weight=2)
        paned.add(right, weight=3)

        ttk.Label(left, text="冲突 GPIO（点击查看详情）", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        tree_frame, self.conflict_tree = self._make_tree(
            left,
            columns=("pin", "count", "nodes"),
            headings=("引脚", "节点数", "占用节点"),
            widths=(170, 70, 480),
            stretch={"nodes": True},
        )
        tree_frame.pack(fill="both", expand=True)
        self.conflict_tree.bind("<<TreeviewSelect>>", self._on_conflict_select)
        self.conflict_tree.tag_configure("conflict", foreground=COLOR_RED)
        self.conflict_tree.tag_configure("odd", background="#f7f9fc")
        self.conflict_tree.tag_configure("disabled", foreground=COLOR_MUTED)

        detail_head = ttk.Frame(right)
        detail_head.pack(fill="x", pady=(0, 6))
        ttk.Label(detail_head, text="冲突详情", style="Title.TLabel").pack(side="left")
        ttk.Button(detail_head, text="复制详情", command=self.copy_conflict_detail).pack(side="right")

        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True)
        self.detail_text = tk.Text(
            text_frame, wrap="word", font=(self.family, 10),
            background="white", foreground=COLOR_TEXT,
            relief="solid", borderwidth=1, padx=10, pady=8,
            state="disabled", cursor="arrow",
        )
        detail_vsb = ttk.Scrollbar(text_frame, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_vsb.set)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_vsb.grid(row=0, column=1, sticky="ns")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        self.detail_text.tag_configure("h1", font=(self.family, 13, "bold"), foreground="#0b4f6c", spacing3=6)
        self.detail_text.tag_configure("h2", font=(self.family, 10, "bold"), foreground=COLOR_BLUE, spacing1=8, spacing3=2)
        self.detail_text.tag_configure("warn", foreground=COLOR_RED)
        self.detail_text.tag_configure("muted", foreground=COLOR_MUTED)
        self.detail_text.tag_configure("ok", foreground=COLOR_GREEN)

    def _build_all_tab(self) -> None:
        top = ttk.Frame(self.tab_all, padding=(8, 8, 8, 4))
        top.pack(side="top", fill="x")
        top.columnconfigure(2, weight=1)
        ttk.Label(top, text="过滤：").grid(row=0, column=0, padx=(0, 6))
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=36)
        filter_entry.grid(row=0, column=1, sticky="w")
        filter_entry.bind("<KeyRelease>", lambda _e: self._refresh_all_pins())
        self.filter_count_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.filter_count_label.grid(row=0, column=2, sticky="e")

        tree_frame, self.all_tree = self._make_tree(
            self.tab_all,
            columns=("pin", "source", "node", "ref", "state", "config", "location"),
            headings=("引脚", "类型", "节点", "引用 / 属性", "状态", "配置", "位置"),
            widths=(160, 70, 190, 210, 80, 220, 310),
            stretch={"node": True, "ref": True, "location": True},
        )
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.all_tree.tag_configure("conflict", foreground=COLOR_RED)
        self.all_tree.tag_configure("disabled", foreground=COLOR_MUTED)
        self.all_tree.tag_configure("odd", background="#f7f9fc")
        self.all_tree.bind("<Double-1>", self._on_all_double_click)

        hint = ttk.Label(
            self.tab_all,
            text="提示：双击一行可将该引脚填入底部查询框。",
            style="Muted.TLabel",
        )
        hint.pack(side="bottom", anchor="w", padx=12, pady=(0, 6))

    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self.tab_log, padding=8)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            frame, wrap="word", font=(self.family, 10),
            background="white", foreground=COLOR_TEXT,
            relief="solid", borderwidth=1, padx=10, pady=8,
            state="disabled",
        )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.log_text.tag_configure("h", font=(self.family, 11, "bold"), foreground="#0b4f6c")
        self.log_text.tag_configure("warn", foreground=COLOR_ORANGE)
        self.log_text.tag_configure("error", foreground=COLOR_RED)
        self.log_text.tag_configure("muted", foreground=COLOR_MUTED)

    def _build_query_panel(self) -> None:
        self.query_frame = ttk.LabelFrame(
            self.root, text="  GPIO 查询 — 检查指定引脚是否已经被使用  ",
            padding=(10, 6, 10, 8),
        )
        self.query_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        self.query_frame.columnconfigure(1, weight=0)

        row = ttk.Frame(self.query_frame)
        row.grid(row=0, column=0, sticky="ew")
        row.columnconfigure(3, weight=1)
        ttk.Label(row, text="GPIO：", font=(self.family, 10, "bold")).grid(row=0, column=0, padx=(0, 4))
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(row, textvariable=self.query_var, width=30, font=(self.family, 11))
        self.query_entry.grid(row=0, column=1, padx=(0, 8))
        self.query_entry.bind("<Return>", lambda _e: self.on_query())
        self.query_btn = ttk.Button(row, text="查询", style="Accent.TButton", command=self.on_query)
        self.query_btn.grid(row=0, column=2, padx=(0, 6))
        self.clear_btn = ttk.Button(row, text="清空", command=self.on_clear_query)
        self.clear_btn.grid(row=0, column=3, sticky="w")

        hint = ttk.Label(
            row,
            text="示例：GPIO4_RK_PB2  ·  4 10  ·  gpio4 10  ·  RK_PB2（所有 bank）",
            style="Muted.TLabel",
        )
        hint.grid(row=0, column=4, sticky="e", padx=(10, 0))

        self.query_badge = tk.Label(
            self.query_frame, text="尚未查询", font=(self.family, 11, "bold"),
            fg=COLOR_MUTED, bg=COLOR_PANEL, anchor="w", justify="left",
        )
        self.query_badge.grid(row=1, column=0, sticky="ew", pady=(6, 4))

        tree_frame, self.query_tree = self._make_tree(
            self.query_frame,
            columns=("state", "pin", "node", "source", "config", "location"),
            headings=("状态", "引脚", "节点", "来源", "配置", "位置"),
            widths=(80, 160, 180, 210, 220, 300),
            stretch={"node": True, "source": True, "location": True},
            height=4,
        )
        tree_frame.grid(row=2, column=0, sticky="nsew")
        self.query_frame.rowconfigure(2, weight=1)
        self.query_frame.columnconfigure(0, weight=1)
        self.query_tree.tag_configure("conflict", foreground=COLOR_RED)
        self.query_tree.tag_configure("used", foreground=COLOR_ORANGE)
        self.query_tree.tag_configure("disabled", foreground=COLOR_MUTED)
        self.query_tree.tag_configure("free", foreground=COLOR_GREEN)
        self.query_tree.bind("<Double-1>", self._on_query_double_click)

    # ========================================================
    # 小部件辅助
    # ========================================================

    def _make_tree(
        self,
        parent: tk.Widget,
        columns: Tuple[str, ...],
        headings: Tuple[str, ...],
        widths: Tuple[int, ...],
        stretch: Optional[Dict[str, bool]] = None,
        height: int = 12,
    ) -> Tuple[ttk.Frame, ttk.Treeview]:
        stretch = stretch or {}
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for col, head, width in zip(columns, headings, widths):
            tree.heading(col, text=head, anchor="w")
            tree.column(col, width=width, minwidth=60, stretch=stretch.get(col, False), anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return frame, tree

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _append_log(self, text: str, tag: Optional[str] = None) -> None:
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert("end", text, tag)
        else:
            self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self.on_open())
        self.root.bind("<Control-s>", lambda _e: self.on_export())
        self.root.bind("<F5>", lambda _e: self.on_reparse())
        self.root.bind("<Control-f>", lambda _e: self.focus_query())
        self.root.bind("<Control-q>", lambda _e: self._on_close())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ========================================================
    # 文件与解析
    # ========================================================

    def on_open(self) -> None:
        path = filedialog.askopenfilename(
            title="选择设备树文件",
            initialdir=self._last_dir,
            filetypes=[
                ("设备树文件", "*.dts *.dtsi *.dtb"),
                ("DTS 源文件", "*.dts *.dtsi"),
                ("DTB 二进制", "*.dtb"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.load_file(path)

    def on_reparse(self) -> None:
        path = self.path_var.get().strip().strip('"')
        if path:
            self.load_file(path)
        elif self.current_path:
            self.load_file(self.current_path)
        else:
            messagebox.showinfo(APP_TITLE, "请先选择一个 .dts / .dtsi / .dtb 文件。")

    def load_file(self, path: str) -> None:
        if self.busy:
            return
        path = os.path.abspath(path.strip().strip('"'))
        if not os.path.isfile(path):
            messagebox.showerror(APP_TITLE, f"文件不存在：\n{path}")
            return
        self.current_path = path
        self.path_var.set(path)
        self._last_dir = os.path.dirname(path)
        self._start_parse(path)

    def _dtb_source(self) -> str:
        """当前选择的 DTB 解析方式。"""
        return DTB_SOURCE_DTS if self.dtb_mode_var.get() == MODE_SIBLING_DTS else DTB_SOURCE_BINARY

    def _on_dtb_mode_change(self, _event=None) -> None:
        """切换解析方式后，如果当前打开的就是 DTB，则立即用新方式重新解析。"""
        if self.busy or not self.current_path:
            return
        try:
            if not is_dtb_file(self.current_path):
                return
        except OSError:
            return
        self.load_file(self.current_path)

    def _start_parse(self, path: str) -> None:
        source = self._dtb_source()
        sibling: Optional[str] = None
        is_dtb = is_dtb_file(path)

        if is_dtb and source == DTB_SOURCE_DTS:
            sibling = find_sibling_dts(path)
            if sibling is None:
                expected = os.path.splitext(path)[0] + ".dts"
                if not messagebox.askyesno(
                    APP_TITLE,
                    f"当前 DTB 解析方式为「{MODE_SIBLING_DTS}」，但未找到同名 DTS 文件：\n"
                    f"{expected}\n\n"
                    f"是否改用「{MODE_BINARY}」继续解析该 DTB？",
                ):
                    self.summary_label.configure(text="已取消解析：未找到同名 DTS 文件")
                    self.kind_badge.configure(text="未解析", bg=COLOR_MUTED)
                    return
                self.dtb_mode_var.set(MODE_BINARY)
                source = DTB_SOURCE_BINARY

        self.busy = True
        self._token += 1
        token = self._token
        self._set_busy_ui(True)
        self.file_label.configure(text=os.path.basename(path))

        badge_text, badge_color = _kind_badge(
            "dtb_dts" if (is_dtb and source == DTB_SOURCE_DTS) else ("dtb" if is_dtb else "dts")
        )
        self.kind_badge.configure(text=badge_text, bg=badge_color)

        header = f"正在解析：\n{path}\n"
        if is_dtb and source == DTB_SOURCE_DTS and sibling:
            header += f"解析方式：{MODE_SIBLING_DTS} —— {sibling}\n"
        if is_dtb and source == DTB_SOURCE_BINARY:
            header += "\n读取二进制 DTB（无文件行号）。"
        else:
            header += "\nDTS 会递归读取 include 链，文件较多时需要几秒钟。"
        self.summary_label.configure(text="正在解析，请稍候…")
        self.detail_text.configure(state="normal")
        self._set_text(self.detail_text, header)
        self.notebook.select(self.tab_conflicts)

        threading.Thread(
            target=self._parse_worker, args=(path, source, token), daemon=True
        ).start()
        self.root.after(80, self._poll_parse_queue)

    def _parse_worker(self, path: str, source: str, token: int) -> None:
        try:
            result = analyze(path, dtb_source=source)
            self._queue.put(("ok", token, result))
        except SiblingDtsMissing as exc:
            self._queue.put(("error", token, str(exc)))
        except Exception:
            self._queue.put(("error", token, traceback.format_exc()))

    def _poll_parse_queue(self) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            kind, token, payload = self._queue.get_nowait()
        except queue.Empty:
            self.root.after(80, self._poll_parse_queue)
            return

        if token != self._token:
            return

        self.busy = False
        self._set_busy_ui(False)

        if kind == "error":
            self._on_parse_error(str(payload))
            return

        assert isinstance(payload, AnalysisResult)
        self.result = payload
        self._render_result(payload)

    def _set_busy_ui(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        for widget in (self.open_btn, self.reparse_btn, self.export_btn, self.query_btn):
            try:
                widget.state(state)
            except tk.TclError:
                pass
        try:
            self.dtb_mode_combo.configure(state="disabled" if busy else "readonly")
        except tk.TclError:
            pass
        if busy:
            self.progress.grid()
            self.progress.start(12)
            self.root.configure(cursor="watch")
        else:
            self.progress.stop()
            self.progress.grid_remove()
            self.root.configure(cursor="")

    def _on_parse_error(self, trace: str) -> None:
        self.result = None
        self.summary_label.configure(text="解析失败")
        self.kind_badge.configure(text="错误", bg=COLOR_RED)
        self._set_text(self.detail_text, "解析失败。\n\n" + trace)
        self.conflict_tree.delete(*self.conflict_tree.get_children())
        self.all_tree.delete(*self.all_tree.get_children())
        self.query_tree.delete(*self.query_tree.get_children())
        self._set_text(self.log_text, "解析失败：\n\n" + trace)
        messagebox.showerror(
            APP_TITLE,
            "解析失败，详细信息已写入「解析日志」页。\n\n" + trace.strip().splitlines()[-1],
        )

    # ========================================================
    # 渲染结果
    # ========================================================

    def _render_result(self, result: AnalysisResult) -> None:
        self._conflict_pins.clear()

        badge_text, badge_color = _kind_badge(result.kind)
        self.kind_badge.configure(text=badge_text, bg=badge_color)
        self.file_label.configure(text=result.file_name)
        self.summary_label.configure(
            text=(
                f"pinctrl 组 {result.pinctrl_def_count}  ·  "
                f"pinctrl-N 引用 {result.pinctrl_usage_count}（启用 {result.pinctrl_usage_active}）  ·  "
                f"*-gpios {result.gpio_usage_count}（启用 {result.gpio_usage_active}）  ·  "
                f"启用 pin {len(result.active_pins)}  ·  "
                f"冲突 {result.conflict_count}  ·  {result.parse_seconds:.2f}s"
            )
        )

        # 冲突列表
        self.conflict_tree.delete(*self.conflict_tree.get_children())
        for index, pin in enumerate(sorted(result.conflicts)):
            owners = result.conflicts[pin]
            nodes = "、".join(claim.node_label for claim in owners)
            if len(nodes) > 120:
                nodes = nodes[:117] + "…"
            iid = pin.name
            self._conflict_pins[iid] = pin
            self.conflict_tree.insert(
                "", "end", iid=iid,
                values=(pin.name, len(owners), nodes),
                tags=("conflict",) if index % 2 == 0 else ("conflict", "odd"),
            )

        # 全部占用
        self.filter_var.set("")
        self._refresh_all_pins()

        # 日志
        self._render_log(result)

        # 详情初始状态
        if result.conflicts:
            first = sorted(result.conflicts)[0]
            iid = first.name
            self.conflict_tree.selection_set(iid)
            self.conflict_tree.focus(iid)
            self._show_conflict_detail(first)
        else:
            self._set_text(self.detail_text, self._no_conflict_text(result))

        # 查询清空
        self._clear_query_result(keep_entry=False)
        self.query_badge.configure(text="已解析，可在左侧输入 GPIO 查询", fg=COLOR_MUTED)
        self.notebook.select(self.tab_conflicts)

        if self._initial_query:
            query = self._initial_query
            self._initial_query = None
            self.query_var.set(query)
            self.on_query()

    def _render_log(self, result: AnalysisResult) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self._append_log("解析完成\n", "h")
        self._append_log(f"文件：{result.entry}\n")
        self._append_log(f"类型：{result.kind_text}\n")
        self._append_log(f"用时：{result.parse_seconds:.2f} 秒\n")
        self._append_log(
            f"pinctrl 组：{result.pinctrl_def_count}    "
            f"pinctrl-N 引用：{result.pinctrl_usage_count}（启用 {result.pinctrl_usage_active}）    "
            f"*-gpios：{result.gpio_usage_count}（启用 {result.gpio_usage_active}）\n"
        )
        self._append_log(
            f"占用记录：{len(result.claims)} 条    启用 pin：{len(result.active_pins)}    "
            f"冲突 pin：{result.conflict_count}\n\n"
        )

        if result.uses_dts_source:
            self._append_log(f"实际解析文件（{len(result.files)}）：\n", "h")
            for path in result.files:
                self._append_log(f"  {result.relpath(path)}\n")
            self._append_log("\n")

        if result.warnings:
            self._append_log(f"解析提示（{len(result.warnings)}）：\n", "h")
            for warning in result.warnings:
                self._append_log(f"  - {warning}\n", "warn")
            self._append_log("\n")
        else:
            self._append_log("解析提示：无\n\n", "muted")

        self._append_log(
            "说明：冲突判定只统计 status 为 okay/ok 的节点；"
            "多个节点共享同一个 pinctrl 组不算冲突。\n",
            "muted",
        )

    # ========================================================
    # 冲突详情
    # ========================================================

    def _no_conflict_text(self, result: AnalysisResult) -> str:
        return (
            "未发现 pinctrl / gpio 冲突。\n\n"
            f"本次解析共发现 {len(result.active_pins)} 个启用节点占用的 GPIO pin。\n"
            "可以在「全部占用」页查看完整列表，或在底部输入 GPIO 检查是否被使用。\n"
        )

    def _on_conflict_select(self, _event=None) -> None:
        selection = self.conflict_tree.selection()
        if not selection:
            return
        pin = self._conflict_pins.get(selection[0])
        if pin is not None:
            self._show_conflict_detail(pin)

    def _show_conflict_detail(self, pin: Pin) -> None:
        if self.result is None:
            return
        owners = self.result.conflicts.get(pin, [])
        lines: List[str] = []
        lines.append(f"{pin.name}\n")
        lines.append(f"bank={pin.bank}    pin={pin.pin}    Linux 全局编号={pin.global_number}\n")
        lines.append(f"冲突：{len(owners)} 个节点争用同一个 GPIO pin\n")
        lines.append("─" * 72 + "\n")

        for index, claim in enumerate(owners, 1):
            lines.append(f"\n[{index}] 节点：{claim.node_label}\n")
            lines.append(f"    来源：{claim.source_text}\n")
            lines.append(f"    配置：func={claim.func_text}    bias={claim.bias_text}\n")
            lines.append(f"    位置：{self._claim_location(claim)}\n")
            if claim.source == "pinctrl":
                group_loc = self._group_location(claim)
                lines.append(f"    pinctrl 组定义：{group_loc}\n")
            lines.append(f"    节点状态：{_status_text(claim)}\n")

        conflict_keys = {(c.node_path, c.source, c.ref_id) for c in owners}
        others = [
            c for c in self.result.claims_for(pin)
            if (c.node_path, c.source, c.ref_id) not in conflict_keys
        ]
        if others:
            lines.append("\n其他占用（共享同一 pinctrl 组或节点已禁用，不计为冲突）\n")
            lines.append("─" * 72 + "\n")
            for claim in others:
                lines.append(
                    f"  · {claim.node_label}  ←  {claim.source_text}"
                    f"（{_status_text(claim)}，{self._claim_location(claim)}）\n"
                )

        self._set_text(self.detail_text, "".join(lines))

    def _claim_location(self, claim: PinClaim) -> str:
        if self.result is None:
            return ""
        if claim.filepath and claim.lineno:
            return f"{self.result.relpath(claim.filepath)}:{claim.lineno}"
        return "DTB（无行号信息）"

    def _is_conflict_claim(self, claim: PinClaim) -> bool:
        """该条占用本身是否属于冲突（同一 pin 上的其他共享/禁用占用不算）。"""
        if self.result is None:
            return False
        for owner in self.result.conflicts.get(claim.pin, ()):
            if (owner.node_path == claim.node_path
                    and owner.source == claim.source
                    and owner.ref_id == claim.ref_id):
                return True
        return False

    def _group_location(self, claim: PinClaim) -> str:
        if self.result is None:
            return ""
        if claim.group_file and claim.group_line:
            return f"{self.result.relpath(claim.group_file)}:{claim.group_line}"
        if claim.group_path:
            return claim.group_path
        return "—"

    def copy_conflict_detail(self) -> None:
        text = self.detail_text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._append_log("已复制冲突详情到剪贴板。\n", "muted")

    # ========================================================
    # 全部占用
    # ========================================================

    def _refresh_all_pins(self) -> None:
        if self.result is None:
            return
        keyword = self.filter_var.get().strip().lower()
        self.all_tree.delete(*self.all_tree.get_children())
        rows = 0
        for index, claim in enumerate(self.result.claims):
            haystack = " ".join((
                claim.pin.name, claim.node_label, claim.source_text,
                claim.ref, claim.status, self._claim_location(claim),
            )).lower()
            if keyword and keyword not in haystack:
                continue
            tags: Tuple[str, ...] = ()
            if not claim.active:
                tags = ("disabled",)
            elif self._is_conflict_claim(claim):
                tags = ("conflict",)
            if index % 2 == 1:
                tags = tags + ("odd",)
            self.all_tree.insert(
                "", "end",
                values=(
                    claim.pin.name,
                    "pinctrl" if claim.source == "pinctrl" else "gpio",
                    claim.node_label,
                    claim.source_text,
                    "启用" if claim.active else "禁用",
                    f"{_short_func(claim)}  {claim.bias_text}",
                    self._claim_location(claim),
                ),
                tags=tags,
            )
            rows += 1
        total = len(self.result.claims)
        self.filter_count_label.configure(text=f"显示 {rows} / {total} 条占用记录")

    def _on_all_double_click(self, _event=None) -> None:
        selection = self.all_tree.selection()
        if not selection:
            return
        values = self.all_tree.item(selection[0], "values")
        if not values:
            return
        self.query_var.set(values[0])
        self.on_query()
        self.focus_query()

    # ========================================================
    # GPIO 查询
    # ========================================================

    def focus_query(self) -> None:
        self.query_entry.focus_set()
        self.query_entry.selection_range(0, "end")

    def on_query(self) -> None:
        if self.result is None:
            messagebox.showinfo(APP_TITLE, "请先打开并解析一个 .dts / .dtb 文件。")
            return
        text = self.query_var.get().strip()
        try:
            spec = parse_gpio_query(text)
        except QueryError as exc:
            self._clear_query_tree()
            self.query_badge.configure(text=str(exc).splitlines()[0], fg=COLOR_RED)
            self._set_text(self.detail_text, f"无法识别查询输入：\n\n{exc}")
            return

        self._render_query(spec)

    def _render_query(self, spec: QuerySpec) -> None:
        assert self.result is not None
        self._clear_query_tree()
        self._query_pins.clear()

        active_claims: List[PinClaim] = []
        disabled_claims: List[PinClaim] = []
        for pin in spec.pins:
            for claim in self.result.claims_for(pin):
                if claim.active:
                    active_claims.append(claim)
                else:
                    disabled_claims.append(claim)

        if not spec.wildcard_bank:
            pin = spec.pins[0]
            conflict = self.result.conflict_for(pin)
            if active_claims:
                if conflict:
                    self.query_badge.configure(
                        text=(
                            f"✗ {pin.name} 已被使用，并且存在冲突："
                            f"{len(conflict)} 个节点争用该 pin"
                        ),
                        fg=COLOR_RED,
                    )
                else:
                    self.query_badge.configure(
                        text=f"● {pin.name} 已被使用（{len(active_claims)} 处启用占用）",
                        fg=COLOR_ORANGE,
                    )
            elif disabled_claims:
                self.query_badge.configure(
                    text=f"◐ {pin.name} 只被已禁用节点引用，实际未启用",
                    fg=COLOR_ORANGE,
                )
            else:
                self.query_badge.configure(
                    text=f"✓ {pin.name} 未被使用",
                    fg=COLOR_GREEN,
                )
        else:
            used_pins = sorted({c.pin for c in active_claims})
            disabled_pins = sorted({c.pin for c in disabled_claims} - set(used_pins))
            if used_pins:
                names = "、".join(p.name for p in used_pins[:6])
                if len(used_pins) > 6:
                    names += "…"
                self.query_badge.configure(
                    text=f"● 所有 bank 中 {len(used_pins)} 个位置已被使用：{names}",
                    fg=COLOR_ORANGE,
                )
            elif disabled_pins:
                self.query_badge.configure(
                    text=f"◐ 只被已禁用节点引用（{len(disabled_pins)} 个 bank），实际未启用",
                    fg=COLOR_ORANGE,
                )
            else:
                self.query_badge.configure(
                    text=f"✓ 所有 bank 的 {spec.pins[0].alias_name.split('_', 1)[1]} 均未被使用",
                    fg=COLOR_GREEN,
                )

        # 单 pin 且完全未使用：插入一行占位，明确显示“未使用”
        if not active_claims and not disabled_claims and not spec.wildcard_bank:
            self.query_tree.insert(
                "", "end",
                values=("未使用", spec.pins[0].name, "—", "—", "—", "—"),
                tags=("free",),
            )

        for claim in active_claims + disabled_claims:
            if not claim.active:
                state_text, tag = "已禁用", "disabled"
            elif self._is_conflict_claim(claim):
                state_text, tag = "冲突", "conflict"
            else:
                state_text, tag = "已使用", "used"
            iid = self.query_tree.insert(
                "", "end",
                values=(
                    state_text,
                    claim.pin.name,
                    claim.node_label,
                    claim.source_text,
                    f"{_short_func(claim)}  {claim.bias_text}",
                    self._claim_location(claim),
                ),
                tags=(tag,),
            )
            self._query_pins[iid] = claim.pin

        # 详情跟随查询结果。
        # 如果查询到的引脚本身存在冲突，优先展示冲突详情，并同步选中冲突列表中的对应行，
        # 避免 Treeview 的选中事件把详情又切回第一行。
        all_claims = active_claims + disabled_claims
        if all_claims:
            target_pin = next(
                (c.pin for c in all_claims if c.pin in self.result.conflicts),
                all_claims[0].pin,
            )
            if target_pin in self.result.conflicts:
                iid = target_pin.name
                if iid in self._conflict_pins:
                    self.conflict_tree.selection_set(iid)
                    self.conflict_tree.focus(iid)
                    self.conflict_tree.see(iid)
                self._show_conflict_detail(target_pin)
            else:
                selected = self.conflict_tree.selection()
                if selected:
                    self.conflict_tree.selection_remove(*selected)
                self._show_query_detail(spec, active_claims, disabled_claims)
        else:
            selected = self.conflict_tree.selection()
            if selected:
                self.conflict_tree.selection_remove(*selected)
            self._show_query_detail(spec, [], [])

        self.notebook.select(self.tab_conflicts)

    def _show_query_detail(
        self,
        spec: QuerySpec,
        active_claims: List[PinClaim],
        disabled_claims: List[PinClaim],
    ) -> None:
        lines: List[str] = []
        lines.append(f"GPIO 查询：{spec.label}\n")
        if not spec.wildcard_bank:
            pin = spec.pins[0]
            lines.append(f"bank={pin.bank}    pin={pin.pin}    Linux 全局编号={pin.global_number}\n")
        lines.append("")

        if active_claims:
            lines.append(f"已被使用：{len(active_claims)} 处启用占用\n")
            lines.append("─" * 72 + "\n")
            for claim in active_claims:
                lines.append(f"  · 节点：{claim.node_label}\n")
                lines.append(f"    来源：{claim.source_text}\n")
                lines.append(f"    配置：func={claim.func_text}    bias={claim.bias_text}\n")
                lines.append(f"    位置：{self._claim_location(claim)}\n")
        elif disabled_claims:
            lines.append("实际未启用：只有已禁用节点引用该 GPIO\n")
            lines.append("─" * 72 + "\n")
            for claim in disabled_claims:
                lines.append(f"  · 节点：{claim.node_label}（{claim.status}）\n")
                lines.append(f"    来源：{claim.source_text}\n")
                lines.append(f"    位置：{self._claim_location(claim)}\n")
        else:
            lines.append("未被使用：没有任何节点通过 pinctrl 或 *-gpios 占用该 GPIO。\n")
            lines.append("\n提示：DTS 源文件可能存在条件编译或未包含的分支，最终结论建议用 .dtb 复核。\n")

        self._set_text(self.detail_text, "".join(lines))

    def _clear_query_tree(self) -> None:
        self.query_tree.delete(*self.query_tree.get_children())
        self._query_pins.clear()

    def _clear_query_result(self, keep_entry: bool = True) -> None:
        self._clear_query_tree()
        if not keep_entry:
            self.query_var.set("")
        self.query_badge.configure(text="尚未查询", fg=COLOR_MUTED)

    def on_clear_query(self) -> None:
        self._clear_query_result(keep_entry=True)
        self.query_badge.configure(text="尚未查询", fg=COLOR_MUTED)

    def _on_query_double_click(self, _event=None) -> None:
        selection = self.query_tree.selection()
        if not selection:
            return
        pin = self._query_pins.get(selection[0])
        if pin is not None and self.result is not None and pin in self.result.conflicts:
            iid = pin.name
            if iid in self._conflict_pins:
                self.notebook.select(self.tab_conflicts)
                self.conflict_tree.selection_set(iid)
                self.conflict_tree.focus(iid)
                self.conflict_tree.see(iid)
                self._show_conflict_detail(pin)

    # ========================================================
    # 导出 / 帮助 / 退出
    # ========================================================

    def on_export(self, include_all: bool = False) -> None:
        if self.busy:
            return
        if self.result is None:
            messagebox.showinfo(APP_TITLE, "请先打开并解析一个 .dts / .dtb 文件。")
            return
        path = filedialog.asksaveasfilename(
            title="导出冲突报告",
            initialdir=self._last_dir,
            initialfile=default_report_name(self.result),
            defaultextension=".md",
            filetypes=[("Markdown 报告", "*.md"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            content = build_markdown(self.result, include_all_pins=include_all)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"写入报告失败：\n{exc}")
            return
        messagebox.showinfo(APP_TITLE, f"报告已导出：\n{path}")

    def on_help(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "1. 点击「打开设备树…」，选择 .dts / .dtsi / .dtb 文件。\n\n"
            "2. 程序自动解析：#include 链（DTS）或 phandle（DTB），\n"
            "   收集 pinctrl-N 与 *-gpios 占用，计算冲突。\n\n"
            "   .dtb 的解析方式可在工具栏「DTB 解析方式」中选择：\n"
            "     · 同名 DTS 解析（默认）：改用同目录同名 .dts 做文本解析，\n"
            "       能看到文件行号与 include 链，便于修改源码时定位；\n"
            "       若找不到同名 .dts，会提示并可回退到二进制解析。\n"
            "     · DTB 二进制解析：直接读编译结果，最贴近实际运行状态。\n\n"
            "3. 「冲突列表」显示争用同一 GPIO 的不同节点；\n"
            "   点击一行可在右侧查看来源、配置和文件行号。\n\n"
            "4. 「全部占用」可按引脚/节点/属性过滤完整占用列表。\n\n"
            "5. 底部 GPIO 查询支持：\n"
            "   GPIO4_RK_PB2、GPIO4_B2、4 RK_PB2、4 10、gpio4 10、\n"
            "   bank4 pin10、RK_PB2（查询所有 bank）。\n\n"
            "提示：DTS 是文本解析，最终结论建议用编译后的 .dtb 复核。",
        )

    def on_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE}\n\n"
            "在原命令行工具 check_pinctrl_conflict.py 的基础上增加了：\n"
            "  · .dtb 二进制解析（无需 dtc）\n"
            "  · .dtb 默认按同名 DTS 源文件解析，可切换为二进制解析\n"
            "  · 图形化冲突列表与详情\n"
            "  · 指定 GPIO 占用查询\n"
            "  · Markdown 报告导出\n\n"
            "仅使用 Python 标准库。",
        )

    def _set_empty_state(self) -> None:
        self.kind_badge.configure(text="未加载", bg=COLOR_MUTED)
        self.file_label.configure(text="尚未打开设备树文件")
        self.summary_label.configure(text="")
        self.query_badge.configure(text="尚未查询", fg=COLOR_MUTED)
        self._set_text(
            self.detail_text,
            "欢迎使用 Rockchip 设备树 GPIO 冲突检测器\n\n"
            "1. 点击左上角「打开设备树…」选择 .dts / .dtsi / .dtb；\n"
            "   打开 .dtb 时默认按同名 DTS 解析，可在工具栏切换为二进制解析；\n"
            "2. 在「冲突列表」查看当前冲突关系；\n"
            "3. 在底部 GPIO 查询框输入引脚，检查是否已被使用。\n\n"
            "支持的查询格式：GPIO4_RK_PB2、GPIO4_B2、4 RK_PB2、4 10、gpio4 10、RK_PB2。",
        )
        self._set_text(self.log_text, "等待打开设备树文件…\n")

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(APP_TITLE, "正在解析，确定要退出吗？"):
                return
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()

    initial_path: Optional[str] = None
    initial_query: Optional[str] = None
    initial_dtb_mode: Optional[str] = None

    def dtb_mode_from(value: str) -> str:
        return MODE_SIBLING_DTS if value.strip().lower() in ("dts", "source", "same-dts") else MODE_BINARY

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--query", "-q") and i + 1 < len(args):
            initial_query = args[i + 1]
            i += 2
            continue
        if arg in ("--dtb-mode", "--dtb-source") and i + 1 < len(args):
            initial_dtb_mode = dtb_mode_from(args[i + 1])
            i += 2
            continue
        if arg.startswith("--query="):
            initial_query = arg.split("=", 1)[1]
        elif arg.startswith("--dtb-mode="):
            initial_dtb_mode = dtb_mode_from(arg.split("=", 1)[1])
        elif arg.startswith("--dtb-source="):
            initial_dtb_mode = dtb_mode_from(arg.split("=", 1)[1])
        elif initial_path is None:
            initial_path = arg
        i += 1

    root = tk.Tk()
    app = DtsGuiApp(
        root,
        initial_path=initial_path,
        initial_query=initial_query,
        initial_dtb_mode=initial_dtb_mode,
    )
    # 防止 app 被垃圾回收
    root.app = app  # type: ignore[attr-defined]
    root.mainloop()


if __name__ == "__main__":
    main()
