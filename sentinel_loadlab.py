import csv
import json
import math
import os
import platform
import queue
import statistics
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from html import escape

import customtkinter as ctk
import requests
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import filedialog, messagebox

# ------------------------------------------------------------
# Sentinel LoadLab V4
# Premium, bounded, authorized-only load/stress testing utility.
# ------------------------------------------------------------
MAX_WORKERS = 20
MAX_RPS = 25.0
MAX_DURATION = 300
MAX_WARMUP = 10
MAX_TIMEOUT = 30.0
MAX_VISIBLE_LOG_LINES = 700
CHART_WINDOW = 120

PRESETS = {
    "Custom": None,
    "Smoke": {"duration": 15, "warmup": 1, "workers": 2, "rps": 2.0, "timeout": 8},
    "Baseline": {"duration": 30, "warmup": 2, "workers": 5, "rps": 5.0, "timeout": 10},
    "Peak Safe": {"duration": 60, "warmup": 3, "workers": 10, "rps": 15.0, "timeout": 12},
    "Max Bounded": {"duration": 60, "warmup": 3, "workers": 20, "rps": 25.0, "timeout": 15},
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


@dataclass
class RequestResult:
    timestamp: str
    elapsed_s: float
    method: str
    status: int | str
    latency_ms: float
    bytes_received: int
    error: str


class SentinelLoadLab(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Sentinel LoadLab V4 — Authorized Web Stress Tester")
        self.geometry("1580x980")
        self.minsize(1280, 820)

        self.colors = {
            "bg": "#07111F",
            "panel": "#0C1728",
            "panel2": "#101D31",
            "panel3": "#13233A",
            "panel4": "#162840",
            "glass": "#0A1726",
            "border": "#213A5A",
            "text": "#EAF2FF",
            "muted": "#8293AE",
            "accent": "#20D4A7",
            "accent_soft": "#143E3B",
            "accent2": "#4D8DFF",
            "accent2_soft": "#172C4A",
            "danger": "#FF5D73",
            "warning": "#FFB84D",
            "success": "#43D17A",
            "purple": "#A78BFA",
            "cyan": "#58D6FF",
            "surface": "#091624",
        }
        self.configure(fg_color=self.colors["bg"])

        self.stop_event = threading.Event()
        self.test_thread = None
        self.queue = queue.Queue()
        self.results: list[RequestResult] = []
        self.status_counts = Counter()
        self.running = False
        self.test_started_at = None
        self.test_ended_at = None
        self.active_config = None
        self.test_id = "—"
        self.thread_local = threading.local()

        self.chart_time = deque(maxlen=CHART_WINDOW)
        self.chart_latency = deque(maxlen=CHART_WINDOW)
        self.rps_time = deque(maxlen=CHART_WINDOW)
        self.rps_values = deque(maxlen=CHART_WINDOW)
        self._last_rps_sample_time = 0.0
        self._last_rps_sample_count = 0

        self._build_ui()
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.bind_all("<Shift-MouseWheel>", self._on_shift_mousewheel, add="+")
        self.bind_all("<Control-Return>", lambda _e: self.start_test(), add="+")
        self.bind_all("<Escape>", lambda _e: self.stop_test(), add="+")
        self.bind_all("<Control-l>", lambda _e: self._focus_url(), add="+")
        self.bind_all("<Control-s>", lambda _e: self.save_config(), add="+")
        self.bind_all("<Home>", lambda _e: self._scroll_fraction(0.0), add="+")
        self.bind_all("<End>", lambda _e: self._scroll_fraction(1.0), add="+")
        self.after(150, self._poll_queue)

    # ----------------------------- UI helpers -----------------------------
    def _on_mousewheel(self, event):
        """Smooth page scrolling without stealing scroll from text widgets."""
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return
        cls = widget.winfo_class().lower()
        if "text" in cls or "listbox" in cls:
            return
        try:
            canvas = self.main_scroll._parent_canvas
            units = -3 if event.delta > 0 else 3
            canvas.yview_scroll(units, "units")
        except Exception:
            pass

    def _on_shift_mousewheel(self, event):
        try:
            canvas = self.main_scroll._parent_canvas
            units = -3 if event.delta > 0 else 3
            canvas.xview_scroll(units, "units")
        except Exception:
            pass

    def _scroll_fraction(self, fraction):
        try:
            self.main_scroll._parent_canvas.yview_moveto(max(0.0, min(1.0, fraction)))
        except Exception:
            pass

    def _scroll_to(self, widget):
        """Scroll main dashboard so the requested section is near the top."""
        try:
            self.update_idletasks()
            canvas = self.main_scroll._parent_canvas
            total = max(1, self.main_scroll.winfo_reqheight())
            y = max(0, widget.winfo_y() - 18)
            canvas.yview_moveto(min(1.0, y / total))
        except Exception:
            pass

    def _focus_url(self):
        try:
            self.url_entry.focus_set()
            self.url_entry.icursor("end")
        except Exception:
            pass

    def _separator(self, parent, pady=(8, 8)):
        line = ctk.CTkFrame(parent, height=1, fg_color=self.colors["border"], corner_radius=0)
        line.pack(fill="x", padx=2, pady=pady)
        return line

    def _card(self, parent, **kwargs):
        kwargs.setdefault("fg_color", self.colors["panel"])
        kwargs.setdefault("border_color", self.colors["border"])
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("corner_radius", 14)
        return ctk.CTkFrame(parent, **kwargs)

    def _label(self, parent, text, size=12, weight="normal", color=None, **kwargs):
        return ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=size, weight=weight),
            text_color=color or self.colors["text"],
            **kwargs,
        )

    def _field_label(self, parent, text):
        return self._label(parent, text.upper(), 10, "bold", self.colors["muted"])

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=255, corner_radius=0, fg_color="#081522")
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.grid_rowconfigure(20, weight=1)

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(24, 12))
        badge = ctk.CTkLabel(
            brand,
            text="S",
            width=46,
            height=46,
            corner_radius=12,
            fg_color=self.colors["accent"],
            text_color="#041B16",
            font=ctk.CTkFont(size=23, weight="bold"),
        )
        badge.pack(side="left")
        bt = ctk.CTkFrame(brand, fg_color="transparent")
        bt.pack(side="left", padx=10)
        self._label(bt, "Sentinel", 19, "bold").pack(anchor="w")
        self._label(bt, "LOADLAB V4", 9, "bold", self.colors["accent"]).pack(anchor="w")

        self._label(side, "TEST PRESET", 9, "bold", self.colors["muted"]).grid(
            row=1, column=0, sticky="w", padx=20, pady=(20, 6)
        )
        self.preset_var = ctk.StringVar(value="Baseline")
        self.preset_menu = ctk.CTkOptionMenu(
            side,
            values=list(PRESETS.keys()),
            variable=self.preset_var,
            command=self._apply_preset,
            fg_color=self.colors["panel3"],
            button_color=self.colors["accent2"],
            button_hover_color="#376FCC",
            dropdown_fg_color=self.colors["panel2"],
            corner_radius=9,
            height=36,
        )
        self.preset_menu.grid(row=2, column=0, sticky="ew", padx=18)

        self._label(side, "SESSION", 9, "bold", self.colors["muted"]).grid(
            row=3, column=0, sticky="w", padx=20, pady=(24, 8)
        )
        self.test_id_var = ctk.StringVar(value="Test ID  —")
        self.state_var = ctk.StringVar(value="● READY")
        self.state_detail_var = ctk.StringVar(value="Waiting for configuration")
        self._label(side, "", 11).grid(row=4, column=0)
        self.state_label = self._label(side, "", 12, "bold", self.colors["accent2"])
        self.state_label.configure(textvariable=self.state_var)
        self.state_label.grid(row=4, column=0, sticky="w", padx=20)
        detail = self._label(side, "", 10, color=self.colors["muted"], wraplength=210, justify="left")
        detail.configure(textvariable=self.state_detail_var)
        detail.grid(row=5, column=0, sticky="w", padx=20, pady=(4, 0))
        tid = self._label(side, "", 9, color=self.colors["muted"])
        tid.configure(textvariable=self.test_id_var)
        tid.grid(row=6, column=0, sticky="w", padx=20, pady=(7, 0))

        self._label(side, "TOOLS", 9, "bold", self.colors["muted"]).grid(
            row=7, column=0, sticky="w", padx=20, pady=(24, 8)
        )
        for idx, (text, cmd) in enumerate(
            [
                ("Connection check", self.connection_check),
                ("Save config", self.save_config),
                ("Load config", self.load_config),
                ("Copy summary", self.copy_summary),
            ],
            start=8,
        ):
            ctk.CTkButton(
                side,
                text=text,
                command=cmd,
                height=34,
                corner_radius=8,
                fg_color=self.colors["panel3"],
                hover_color="#18304C",
                border_width=1,
                border_color=self.colors["border"],
                anchor="w",
            ).grid(row=idx, column=0, sticky="ew", padx=18, pady=4)

        self._label(side, "NAVIGATION", 9, "bold", self.colors["muted"]).grid(
            row=13, column=0, sticky="w", padx=20, pady=(22, 6)
        )
        self.nav_buttons = {}
        nav_items = [
            ("Configuration", lambda: self._scroll_to(self.config_card)),
            ("Metrics", lambda: self._scroll_to(self.metrics_section)),
            ("Performance", lambda: self._scroll_to(self.performance_section)),
            ("Request log", lambda: self._scroll_to(self.log_section)),
        ]
        for i, (label, command) in enumerate(nav_items, start=14):
            btn = ctk.CTkButton(
                side, text=label, command=command, height=32, corner_radius=8,
                fg_color="transparent", hover_color=self.colors["panel3"],
                border_width=0, anchor="w", text_color=self.colors["muted"]
            )
            btn.grid(row=i, column=0, sticky="ew", padx=18, pady=2)
            self.nav_buttons[label] = btn

        safety = ctk.CTkFrame(side, fg_color="#0D1C2B", corner_radius=12, border_width=1, border_color="#234159")
        safety.grid(row=21, column=0, sticky="sew", padx=14, pady=16)
        self._label(safety, "BOUNDED MODE", 10, "bold", self.colors["accent"]).pack(anchor="w", padx=12, pady=(11, 3))
        self._label(
            safety,
            "25 RPS max\n20 workers max\n5 min max\nGET / HEAD only",
            9,
            color=self.colors["muted"],
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 11))

    def _build_main(self):
        self.main_scroll = ctk.CTkScrollableFrame(
            self, fg_color=self.colors["bg"], corner_radius=0,
            scrollbar_button_color=self.colors["panel4"],
            scrollbar_button_hover_color=self.colors["accent2"]
        )
        main = self.main_scroll
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)

        # Header
        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 12))
        head.grid_columnconfigure(0, weight=1)
        title_wrap = ctk.CTkFrame(head, fg_color="transparent")
        title_wrap.grid(row=0, column=0, sticky="w")
        self._label(title_wrap, "Authorized Web Stress Testing", 27, "bold").pack(side="left")
        ctk.CTkLabel(title_wrap, text="  V4 PREMIUM  ", height=24, corner_radius=8, fg_color=self.colors["accent_soft"], text_color=self.colors["accent"], font=ctk.CTkFont(size=10, weight="bold")).pack(side="left", padx=(10,0))
        self._label(
            head,
            "Premium local dashboard for safe, controlled performance testing of systems you own or are explicitly authorized to assess.",
            11,
            color=self.colors["muted"],
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        system_badge = ctk.CTkFrame(head, fg_color=self.colors["panel2"], corner_radius=12,
                                    border_width=1, border_color=self.colors["border"])
        system_badge.grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 0))
        self._label(system_badge, "LOCAL ENGINE", 9, "bold", self.colors["accent"]).pack(
            anchor="e", padx=12, pady=(8, 1)
        )
        self._label(
            system_badge,
            f"{platform.system()} • Python {platform.python_version()}",
            9, color=self.colors["muted"]
        ).pack(anchor="e", padx=12, pady=(0, 8))

        # Config card
        cfg = self._card(main)
        self.config_card = cfg
        cfg.grid(row=1, column=0, sticky="ew", padx=22, pady=8)
        for col in range(4):
            cfg.grid_columnconfigure(col, weight=1)
        self._label(cfg, "Test configuration", 15, "bold").grid(row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(15, 3))
        self._label(cfg, "Tune workload, transport behavior, and request headers.", 9, color=self.colors["muted"]).grid(row=0, column=1, columnspan=3, sticky="e", padx=16, pady=(15, 3))

        self.url_var = ctk.StringVar(value="https://example.com/")
        self.method_var = ctk.StringVar(value="GET")
        self.duration_var = ctk.StringVar(value="30")
        self.warmup_var = ctk.StringVar(value="2")
        self.workers_var = ctk.StringVar(value="5")
        self.rps_var = ctk.StringVar(value="5")
        self.timeout_var = ctk.StringVar(value="10")

        self._field_label(cfg, "Target URL").grid(row=1, column=0, sticky="w", padx=16)
        self.url_entry = ctk.CTkEntry(cfg, textvariable=self.url_var, height=38, corner_radius=8, fg_color=self.colors["panel3"], border_color=self.colors["border"])
        self.url_entry.grid(row=2, column=0, columnspan=3, sticky="ew", padx=(16, 8), pady=(4, 10))
        self._field_label(cfg, "Method").grid(row=1, column=3, sticky="w", padx=(8, 16))
        self.method_menu = ctk.CTkOptionMenu(cfg, variable=self.method_var, values=["GET", "HEAD"], height=38, fg_color=self.colors["panel3"], button_color=self.colors["accent2"])
        self.method_menu.grid(row=2, column=3, sticky="ew", padx=(8, 16), pady=(4, 10))

        fields = [
            ("Duration (s)", self.duration_var),
            ("Warm-up (s)", self.warmup_var),
            ("Workers", self.workers_var),
            ("Target RPS", self.rps_var),
            ("Timeout (s)", self.timeout_var),
        ]
        self.numeric_entries = []
        for i, (label, var) in enumerate(fields):
            col = i % 4
            row = 3 + (i // 4) * 2
            self._field_label(cfg, label).grid(row=row, column=col, sticky="w", padx=16, pady=(4, 0))
            entry = ctk.CTkEntry(cfg, textvariable=var, height=36, fg_color=self.colors["panel3"], border_color=self.colors["border"])
            entry.grid(row=row + 1, column=col, sticky="ew", padx=16, pady=(4, 10))
            self.numeric_entries.append(entry)

        self.verify_tls_var = ctk.BooleanVar(value=True)
        self.redirects_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(cfg, text="Verify TLS certificate", variable=self.verify_tls_var, fg_color=self.colors["accent"], hover_color=self.colors["accent"]).grid(
            row=5, column=1, sticky="w", padx=16, pady=(4, 10)
        )
        ctk.CTkCheckBox(cfg, text="Follow redirects", variable=self.redirects_var, fg_color=self.colors["accent"], hover_color=self.colors["accent"]).grid(
            row=5, column=2, sticky="w", padx=16, pady=(4, 10)
        )

        self._field_label(cfg, "Headers (JSON)").grid(row=6, column=0, sticky="nw", padx=16, pady=(7, 0))
        self.headers_text = ctk.CTkTextbox(cfg, height=92, fg_color="#07101C", border_width=1, border_color=self.colors["border"], font=("Cascadia Mono", 10))
        self.headers_text.grid(row=7, column=0, columnspan=4, sticky="ew", padx=16, pady=(4, 10))
        self.headers_text.insert(
            "1.0",
            json.dumps(
                {
                    "User-Agent": "SentinelLoadLab/4.0",
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
                indent=2,
            ),
        )

        auth_box = ctk.CTkFrame(cfg, fg_color="#0E2030", corner_radius=9, border_width=1, border_color="#284C62")
        auth_box.grid(row=8, column=0, columnspan=4, sticky="ew", padx=16, pady=(0, 15))
        self.auth_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            auth_box,
            text="I confirm I own this target or have explicit authorization to load-test it.",
            variable=self.auth_var,
            fg_color=self.colors["accent"],
            hover_color=self.colors["accent"],
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(10, 4))
        self._label(auth_box, "Authorization is required before any test or connection check starts.", 9, color=self.colors["muted"]).pack(anchor="w", padx=38, pady=(0, 10))

        # Controls
        controls = self._card(main, fg_color=self.colors["glass"])
        controls.grid(row=2, column=0, sticky="ew", padx=22, pady=8)
        controls.grid_columnconfigure(6, weight=1)
        self.start_btn = ctk.CTkButton(controls, text="▶  START TEST", command=self.start_test, height=42, fg_color=self.colors["accent"], hover_color="#18B991", text_color="#041B16", font=ctk.CTkFont(size=12, weight="bold"))
        self.start_btn.grid(row=0, column=0, padx=(14, 6), pady=13)
        self.stop_btn = ctk.CTkButton(controls, text="■  STOP", command=self.stop_test, state="disabled", height=42, fg_color=self.colors["danger"], hover_color="#DF4E62", font=ctk.CTkFont(size=12, weight="bold"))
        self.stop_btn.grid(row=0, column=1, padx=6, pady=13)
        ctk.CTkButton(controls, text="CLEAR", command=self.clear_results, height=42, fg_color=self.colors["panel3"], hover_color="#18304C").grid(row=0, column=2, padx=6, pady=13)
        ctk.CTkButton(controls, text="EXPORT CSV", command=self.export_csv, height=42, fg_color=self.colors["panel3"], hover_color="#18304C").grid(row=0, column=3, padx=6, pady=13)
        ctk.CTkButton(controls, text="EXPORT JSON", command=self.export_json, height=42, fg_color=self.colors["panel3"], hover_color="#18304C").grid(row=0, column=4, padx=6, pady=13)
        ctk.CTkButton(controls, text="HTML REPORT", command=self.export_html, height=42, fg_color=self.colors["panel3"], hover_color="#18304C").grid(row=0, column=5, padx=6, pady=13)
        self.progress = ctk.CTkProgressBar(controls, height=12, fg_color=self.colors["panel3"], progress_color=self.colors["accent"])
        self.progress.grid(row=0, column=6, sticky="ew", padx=(18, 8), pady=13)
        self.progress.set(0)
        self.progress_pct_var = ctk.StringVar(value="0%")
        self._label(controls, "", 11, "bold").configure(textvariable=self.progress_pct_var)
        pp = self._label(controls, "", 11, "bold")
        pp.configure(textvariable=self.progress_pct_var)
        pp.grid(row=0, column=7, padx=(0, 14))

        # Metrics
        metrics = ctk.CTkFrame(main, fg_color="transparent")
        self.metrics_section = metrics
        metrics.grid(row=3, column=0, sticky="ew", padx=17, pady=4)
        for i in range(6):
            metrics.grid_columnconfigure(i, weight=1, uniform="metric")
        self.metric_vars = {}
        names = ["Elapsed", "Completed", "Success %", "Errors", "Achieved RPS", "Avg latency", "P50", "P95", "P99", "Data", "Throughput", "4xx / 5xx"]
        metric_accents = [
            self.colors["accent2"], self.colors["cyan"], self.colors["success"], self.colors["danger"],
            self.colors["accent"], self.colors["purple"], self.colors["accent2"], self.colors["accent2"],
            self.colors["accent2"], self.colors["cyan"], self.colors["accent"], self.colors["warning"],
        ]
        for i, name in enumerate(names):
            card = ctk.CTkFrame(metrics, fg_color=self.colors["panel2"], corner_radius=12,
                                border_width=1, border_color=self.colors["border"])
            card.grid(row=i // 6, column=i % 6, sticky="nsew", padx=5, pady=5)
            ctk.CTkFrame(card, height=3, corner_radius=8, fg_color=metric_accents[i]).pack(fill="x", padx=8, pady=(7, 2))
            self._label(card, name.upper(), 9, "bold", self.colors["muted"]).pack(anchor="w", padx=11, pady=(5, 2))
            var = ctk.StringVar(value="—")
            self.metric_vars[name] = var
            lbl = self._label(card, "", 15, "bold")
            lbl.configure(textvariable=var)
            lbl.pack(anchor="w", padx=11, pady=(0, 10))

        # Charts + telemetry
        lower = ctk.CTkFrame(main, fg_color="transparent")
        self.performance_section = lower
        lower.grid(row=4, column=0, sticky="nsew", padx=22, pady=8)
        lower.grid_columnconfigure(0, weight=3)
        lower.grid_columnconfigure(1, weight=2)

        chart_card = self._card(lower)
        chart_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self._label(chart_card, "Live performance", 14, "bold").pack(anchor="w", padx=14, pady=(13, 2))
        self._label(chart_card, "Latency and achieved RPS over time", 9, color=self.colors["muted"]).pack(anchor="w", padx=14, pady=(0, 4))
        self.figure = Figure(figsize=(8, 4.4), dpi=100, facecolor=self.colors["panel"])
        self.ax_latency = self.figure.add_subplot(211)
        self.ax_rps = self.figure.add_subplot(212)
        self.figure.subplots_adjust(left=0.08, right=0.97, top=0.95, bottom=0.12, hspace=0.38)
        self._style_axes()
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_card)
        self.canvas.get_tk_widget().configure(bg=self.colors["panel"], highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=(0, 9))

        status_card = self._card(lower)
        status_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self._label(status_card, "Response distribution", 14, "bold").pack(anchor="w", padx=14, pady=(13, 2))
        self._label(status_card, "HTTP codes observed during the run", 9, color=self.colors["muted"]).pack(anchor="w", padx=14, pady=(0, 7))
        self.status_text = ctk.CTkTextbox(status_card, height=320, fg_color="#07101C", border_width=1, border_color=self.colors["border"], font=("Cascadia Mono", 11))
        self.status_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.status_text.configure(state="disabled")

        log_card = self._card(main)
        self.log_section = log_card
        log_card.grid(row=5, column=0, sticky="ew", padx=22, pady=(8, 20))
        top = ctk.CTkFrame(log_card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 6))
        self._label(top, "Live request log", 14, "bold").pack(side="left")
        live = ctk.CTkLabel(top, text="● LIVE", height=24, corner_radius=8, fg_color=self.colors["accent_soft"], text_color=self.colors["accent"], font=ctk.CTkFont(size=9, weight="bold"))
        live.pack(side="right")
        self.log = ctk.CTkTextbox(log_card, height=280, fg_color="#07101C", border_width=1, border_color=self.colors["border"], font=("Cascadia Mono", 10))
        self.log.pack(fill="x", padx=12, pady=(0, 12))
        self.log.configure(state="disabled")

        footer = ctk.CTkFrame(main, fg_color="transparent")
        footer.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 22))
        footer.grid_columnconfigure(0, weight=1)
        self._label(footer, "Sentinel LoadLab V4  •  bounded mode  •  authorized testing only", 9, color=self.colors["muted"]).grid(row=0, column=0, sticky="w")
        self._label(footer, "Ctrl+Enter Start  •  Esc Stop  •  Ctrl+L URL  •  Ctrl+S Save", 9, color=self.colors["muted"]).grid(row=0, column=1, sticky="e")

        self._apply_preset("Baseline")

    def _style_axes(self):
        for ax in (self.ax_latency, self.ax_rps):
            ax.set_facecolor(self.colors["panel"])
            ax.tick_params(colors="#8FA1BC", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("#29425E")
            ax.grid(True, alpha=0.18)
        self.ax_latency.set_ylabel("Latency ms", color="#8FA1BC", fontsize=8)
        self.ax_rps.set_ylabel("RPS", color="#8FA1BC", fontsize=8)
        self.ax_rps.set_xlabel("Elapsed s", color="#8FA1BC", fontsize=8)

    # ----------------------------- config -----------------------------
    def _apply_preset(self, name):
        preset = PRESETS.get(name)
        if not preset:
            return
        self.duration_var.set(str(preset["duration"]))
        self.warmup_var.set(str(preset["warmup"]))
        self.workers_var.set(str(preset["workers"]))
        self.rps_var.set(str(preset["rps"]))
        self.timeout_var.set(str(preset["timeout"]))

    def _read_headers(self):
        raw = self.headers_text.get("1.0", "end").strip() or "{}"
        try:
            headers = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Headers JSON is invalid: {exc}") from exc
        if not isinstance(headers, dict):
            raise ValueError("Headers must be a JSON object.")
        return {str(k): str(v) for k, v in headers.items()}

    def _get_config(self, require_auth=True):
        if require_auth and not self.auth_var.get():
            raise ValueError("Authorization confirmation is required.")

        url = self.url_var.get().strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Target URL must be a valid http:// or https:// URL.")

        method = self.method_var.get().strip().upper()
        if method not in ("GET", "HEAD"):
            raise ValueError("Only GET and HEAD are supported in bounded mode.")

        try:
            duration = int(float(self.duration_var.get()))
            warmup = int(float(self.warmup_var.get()))
            workers = int(float(self.workers_var.get()))
            rps = float(self.rps_var.get())
            timeout = float(self.timeout_var.get())
        except ValueError as exc:
            raise ValueError("Duration, warm-up, workers, RPS, and timeout must be numeric.") from exc

        if not 1 <= duration <= MAX_DURATION:
            raise ValueError(f"Duration must be 1-{MAX_DURATION} seconds.")
        if not 0 <= warmup <= MAX_WARMUP:
            raise ValueError(f"Warm-up must be 0-{MAX_WARMUP} seconds.")
        if not 1 <= workers <= MAX_WORKERS:
            raise ValueError(f"Workers must be 1-{MAX_WORKERS}.")
        if not 0.2 <= rps <= MAX_RPS:
            raise ValueError(f"Target RPS must be 0.2-{MAX_RPS}.")
        if not 1 <= timeout <= MAX_TIMEOUT:
            raise ValueError(f"Timeout must be 1-{MAX_TIMEOUT} seconds.")

        return {
            "url": url,
            "method": method,
            "duration": duration,
            "warmup": warmup,
            "workers": workers,
            "rps": rps,
            "timeout": timeout,
            "verify_tls": bool(self.verify_tls_var.get()),
            "follow_redirects": bool(self.redirects_var.get()),
            "headers": self._read_headers(),
        }

    def save_config(self):
        if self.running:
            messagebox.showwarning("Test running", "Stop the test before saving configuration.")
            return
        try:
            cfg = self._get_config(require_auth=False)
        except Exception as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return
        path = filedialog.asksaveasfilename(title="Save configuration", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        Path(path).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        messagebox.showinfo("Saved", f"Configuration saved to:\n{path}")

    def load_config(self):
        if self.running:
            messagebox.showwarning("Test running", "Stop the test before loading configuration.")
            return
        path = filedialog.askopenfilename(title="Load configuration", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
            self.url_var.set(str(cfg.get("url", self.url_var.get())))
            self.method_var.set(str(cfg.get("method", "GET")))
            self.duration_var.set(str(cfg.get("duration", 30)))
            self.warmup_var.set(str(cfg.get("warmup", 2)))
            self.workers_var.set(str(cfg.get("workers", 5)))
            self.rps_var.set(str(cfg.get("rps", 5)))
            self.timeout_var.set(str(cfg.get("timeout", 10)))
            self.verify_tls_var.set(bool(cfg.get("verify_tls", True)))
            self.redirects_var.set(bool(cfg.get("follow_redirects", True)))
            self.headers_text.delete("1.0", "end")
            self.headers_text.insert("1.0", json.dumps(cfg.get("headers", {}), indent=2))
            self.preset_var.set("Custom")
            self._get_config(require_auth=False)
        except Exception as exc:
            messagebox.showerror("Load failed", f"Could not load configuration:\n{exc}")

    # ----------------------------- requests -----------------------------
    def _get_session(self):
        session = getattr(self.thread_local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self.thread_local.session = session
        return session

    def _single_request(self, config, start_time, count_result=True):
        started = time.perf_counter()
        status: int | str = "ERR"
        body_size = 0
        error = ""
        try:
            session = self._get_session()
            resp = session.request(
                method=config["method"],
                url=config["url"],
                headers=config["headers"],
                timeout=config["timeout"],
                verify=config["verify_tls"],
                allow_redirects=config["follow_redirects"],
                stream=(config["method"] == "HEAD"),
            )
            status = int(resp.status_code)
            if config["method"] == "GET":
                body_size = len(resp.content)
            else:
                try:
                    body_size = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    body_size = 0
            if status >= 400:
                error = f"HTTP {status}"
            resp.close()
        except requests.RequestException as exc:
            error = str(exc)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        latency_ms = (time.perf_counter() - started) * 1000.0
        if count_result:
            result = RequestResult(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                elapsed_s=max(0.0, time.monotonic() - start_time),
                method=config["method"],
                status=status,
                latency_ms=latency_ms,
                bytes_received=body_size,
                error=error,
            )
            self.queue.put(("result", result))
        return status, latency_ms, error

    def connection_check(self):
        if self.running:
            messagebox.showwarning("Test running", "Stop the current test first.")
            return
        try:
            cfg = self._get_config(require_auth=True)
        except Exception as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return

        self._set_state("RUNNING", "Checking one authorized request…")
        self._append_log(f"[CHECK] {cfg['method']} {cfg['url']}\n")

        def worker():
            try:
                status, latency, error = self._single_request(cfg, time.monotonic(), count_result=False)
                self.queue.put(("check", (status, latency, error)))
            except Exception as exc:
                self.queue.put(("check", ("ERR", 0.0, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------------- lifecycle -----------------------------
    def start_test(self):
        if self.running:
            return
        try:
            cfg = self._get_config(require_auth=True)
        except Exception as exc:
            messagebox.showerror("Invalid test setup", str(exc))
            return

        self.clear_results(ask=False)
        self.active_config = cfg
        self.stop_event.clear()
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.set(0)
        self.progress_pct_var.set("0%")
        self.test_started_at = time.monotonic()
        self.test_ended_at = None
        self.test_id = uuid.uuid4().hex[:8].upper()
        self.test_id_var.set(f"Test ID  {self.test_id}")
        self._last_rps_sample_time = self.test_started_at
        self._last_rps_sample_count = 0
        self._set_state("RUNNING", f"{cfg['method']} • {cfg['rps']} RPS • {cfg['workers']} workers")
        self._append_log(
            f"[{self.test_id}] Starting authorized test | {cfg['method']} {cfg['url']} | "
            f"{cfg['workers']} workers | {cfg['rps']} RPS | {cfg['duration']}s\n"
        )
        self.test_thread = threading.Thread(target=self._run_test, args=(cfg,), daemon=True)
        self.test_thread.start()

    def stop_test(self):
        if self.running:
            self.stop_event.set()
            self._set_state("STOPPING", "No new requests will be scheduled")
            self._append_log("Stop requested…\n")

    def _warm_up(self, config):
        warmup = config["warmup"]
        if warmup <= 0:
            return True
        self.queue.put(("log", f"Warm-up for {warmup}s (excluded from metrics)…\n"))
        warmup_start = time.monotonic()
        next_request = warmup_start
        interval = max(0.2, 1.0 / min(config["rps"], 5.0))
        while not self.stop_event.is_set() and (time.monotonic() - warmup_start) < warmup:
            now = time.monotonic()
            if now >= next_request:
                self._single_request(config, warmup_start, count_result=False)
                next_request += interval
            else:
                time.sleep(min(0.05, next_request - now))
        return not self.stop_event.is_set()

    def _run_test(self, config):
        try:
            if not self._warm_up(config):
                self.queue.put(("finished", "Stopped during warm-up."))
                return
            start = time.monotonic()
            end = start + config["duration"]
            interval = 1.0 / config["rps"]
            next_launch = start
            pending = set()
            max_pending = max(config["workers"] * 2, 2)

            with ThreadPoolExecutor(max_workers=config["workers"]) as executor:
                while not self.stop_event.is_set() and time.monotonic() < end:
                    now = time.monotonic()
                    if len(pending) >= max_pending:
                        _, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                        continue
                    if now >= next_launch:
                        pending.add(executor.submit(self._single_request, config, start, True))
                        next_launch += interval
                        if next_launch < now - interval:
                            next_launch = now + interval
                    else:
                        time.sleep(max(0.0, min(0.05, next_launch - now, end - now)))
                    done = {f for f in pending if f.done()}
                    pending.difference_update(done)

                while pending and not self.stop_event.is_set():
                    _, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)

            self.queue.put(("finished", "Test stopped by user." if self.stop_event.is_set() else "Test completed."))
        except Exception as exc:
            self.queue.put(("finished", f"Test ended with error: {exc}"))

    # ----------------------------- UI updates -----------------------------
    def _poll_queue(self):
        changed = False
        finished = None
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "result":
                    self.results.append(payload)
                    self.status_counts[str(payload.status)] += 1
                    self.chart_time.append(payload.elapsed_s)
                    self.chart_latency.append(payload.latency_ms)
                    changed = True
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "finished":
                    finished = payload
                elif kind == "check":
                    status, latency, error = payload
                    if error:
                        self._set_state("ERROR", f"Connection check failed: {error[:80]}")
                        self._append_log(f"[CHECK] status={status} latency={latency:.1f} ms error={error}\n")
                    else:
                        self._set_state("READY", f"Connection OK • HTTP {status} • {latency:.1f} ms")
                        self._append_log(f"[CHECK] HTTP {status} • {latency:.1f} ms\n")
        except queue.Empty:
            pass

        now = time.monotonic()
        if self.running and self.test_started_at is not None and self.active_config:
            total = self.active_config["duration"] + self.active_config["warmup"]
            elapsed = max(0.0, now - self.test_started_at)
            fraction = min(1.0, elapsed / max(total, 0.001))
            self.progress.set(fraction)
            self.progress_pct_var.set(f"{fraction * 100:.0f}%")

            if now - self._last_rps_sample_time >= 1.0:
                dt = max(0.001, now - self._last_rps_sample_time)
                delta = len(self.results) - self._last_rps_sample_count
                self.rps_time.append(max(0.0, elapsed - self.active_config["warmup"]))
                self.rps_values.append(delta / dt)
                self._last_rps_sample_time = now
                self._last_rps_sample_count = len(self.results)
                changed = True

        if changed:
            self._refresh_metrics()
            self._refresh_status()
            self._refresh_chart()
            self._refresh_log_tail()

        if finished is not None:
            self.running = False
            self.test_ended_at = time.monotonic()
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.progress.set(1)
            self.progress_pct_var.set("100%")
            self._refresh_metrics(final=True)
            self._refresh_status()
            self._refresh_chart()
            state = "ERROR" if "error" in finished.lower() else "DONE"
            self._set_state(state, finished)
            self._append_log(f"{finished}\n")

        self.after(150, self._poll_queue)

    def _refresh_metrics(self, final=False):
        if self.test_started_at is None:
            return
        end = self.test_ended_at if (final and self.test_ended_at) else time.monotonic()
        warmup = (self.active_config or {}).get("warmup", 0)
        elapsed = max(0.001, end - self.test_started_at - warmup)
        completed = len(self.results)
        successes = sum(1 for r in self.results if isinstance(r.status, int) and 200 <= r.status < 400 and not r.error)
        errors = completed - successes
        latencies = [r.latency_ms for r in self.results]
        total_bytes = sum(r.bytes_received for r in self.results)
        success_pct = (successes / completed * 100.0) if completed else 0.0
        fourxx = sum(1 for r in self.results if isinstance(r.status, int) and 400 <= r.status < 500)
        fivexx = sum(1 for r in self.results if isinstance(r.status, int) and 500 <= r.status < 600)

        self.metric_vars["Elapsed"].set(f"{elapsed:.1f} s")
        self.metric_vars["Completed"].set(str(completed))
        self.metric_vars["Success %"].set(f"{success_pct:.1f}%")
        self.metric_vars["Errors"].set(str(errors))
        self.metric_vars["Achieved RPS"].set(f"{completed / elapsed:.2f}")
        self.metric_vars["Data"].set(self._format_bytes(total_bytes))
        self.metric_vars["Throughput"].set(f"{self._format_bytes(total_bytes / elapsed)}/s")
        self.metric_vars["4xx / 5xx"].set(f"{fourxx} / {fivexx}")

        if latencies:
            vals = sorted(latencies)
            self.metric_vars["Avg latency"].set(f"{statistics.fmean(latencies):.1f} ms")
            self.metric_vars["P50"].set(f"{self._percentile(vals, 50):.1f} ms")
            self.metric_vars["P95"].set(f"{self._percentile(vals, 95):.1f} ms")
            self.metric_vars["P99"].set(f"{self._percentile(vals, 99):.1f} ms")
        else:
            for k in ("Avg latency", "P50", "P95", "P99"):
                self.metric_vars[k].set("—")

    def _refresh_status(self):
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        if not self.status_counts:
            self.status_text.insert("end", "No responses yet.\n")
        else:
            total = max(1, sum(self.status_counts.values()))
            for code, count in sorted(self.status_counts.items(), key=lambda x: x[0]):
                pct = count / total * 100.0
                bar = "█" * max(1, min(24, int(pct / 4)))
                self.status_text.insert("end", f"{code:>5}   {count:>6}   {pct:>6.1f}%  {bar}\n")
        self.status_text.configure(state="disabled")

    def _refresh_chart(self):
        self.ax_latency.clear()
        self.ax_rps.clear()
        self._style_axes()
        if self.chart_time:
            self.ax_latency.plot(list(self.chart_time), list(self.chart_latency), linewidth=1.5)
        if self.rps_time:
            self.ax_rps.plot(list(self.rps_time), list(self.rps_values), linewidth=1.5)
            if self.active_config:
                self.ax_rps.axhline(self.active_config["rps"], linestyle="--", linewidth=1.0, alpha=0.6)
        self.canvas.draw_idle()

    def _refresh_log_tail(self):
        if not self.results:
            return
        r = self.results[-1]
        err = f" | {r.error}" if r.error else ""
        self._append_log(
            f"[{r.timestamp}] {r.method:<4} {str(r.status):>4} | {r.latency_ms:>8.1f} ms | {r.bytes_received:>8} B{err}\n"
        )

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        try:
            lines = len(self.log.get("1.0", "end").splitlines())
            if lines > MAX_VISIBLE_LOG_LINES:
                self.log.delete("1.0", "100.0")
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_state(self, state, detail):
        state = state.upper()
        mapping = {
            "READY": ("● READY", self.colors["accent2"]),
            "RUNNING": ("● RUNNING", self.colors["accent"]),
            "STOPPING": ("● STOPPING", self.colors["warning"]),
            "DONE": ("● COMPLETE", self.colors["success"]),
            "ERROR": ("● ERROR", self.colors["danger"]),
        }
        label, color = mapping.get(state, (f"● {state}", self.colors["muted"]))
        self.state_var.set(label)
        self.state_label.configure(text_color=color)
        self.state_detail_var.set(detail)

    def clear_results(self, ask=True):
        if self.running:
            messagebox.showwarning("Test running", "Stop the current test before clearing results.")
            return
        if ask and self.results and not messagebox.askyesno("Clear results", "Clear current results?"):
            return
        self.results.clear()
        self.status_counts.clear()
        self.chart_time.clear()
        self.chart_latency.clear()
        self.rps_time.clear()
        self.rps_values.clear()
        self.test_started_at = None
        self.test_ended_at = None
        self.active_config = None
        self.test_id = "—"
        self.test_id_var.set("Test ID  —")
        self.progress.set(0)
        self.progress_pct_var.set("0%")
        for var in self.metric_vars.values():
            var.set("—")
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._refresh_chart()
        self._set_state("READY", "Waiting for configuration")

    # ----------------------------- exports -----------------------------
    def _summary_dict(self):
        completed = len(self.results)
        latencies = [r.latency_ms for r in self.results]
        successes = sum(1 for r in self.results if isinstance(r.status, int) and 200 <= r.status < 400 and not r.error)
        total_bytes = sum(r.bytes_received for r in self.results)
        cfg = self.active_config or {}
        elapsed = 0.0
        if self.test_started_at is not None:
            end = self.test_ended_at or time.monotonic()
            elapsed = max(0.0, end - self.test_started_at - cfg.get("warmup", 0))
        return {
            "test_id": self.test_id,
            "target": cfg.get("url"),
            "method": cfg.get("method"),
            "duration_s": cfg.get("duration"),
            "workers": cfg.get("workers"),
            "target_rps": cfg.get("rps"),
            "elapsed_measured_s": round(elapsed, 3),
            "completed": completed,
            "successes": successes,
            "errors": completed - successes,
            "success_rate_pct": round((successes / completed * 100.0) if completed else 0.0, 2),
            "achieved_rps": round(completed / max(elapsed, 0.001), 3) if completed else 0.0,
            "avg_latency_ms": round(statistics.fmean(latencies), 3) if latencies else None,
            "p50_ms": round(self._percentile(sorted(latencies), 50), 3) if latencies else None,
            "p95_ms": round(self._percentile(sorted(latencies), 95), 3) if latencies else None,
            "p99_ms": round(self._percentile(sorted(latencies), 99), 3) if latencies else None,
            "bytes_received": total_bytes,
            "status_counts": dict(self.status_counts),
        }

    def copy_summary(self):
        if not self.results:
            messagebox.showinfo("No results", "Run a test first.")
            return
        summary = self._summary_dict()
        text = "\n".join(f"{k}: {v}" for k, v in summary.items())
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("Copied", "Test summary copied to clipboard.")

    def export_csv(self):
        if not self.results:
            messagebox.showinfo("No results", "Run a test first.")
            return
        path = filedialog.asksaveasfilename(title="Export CSV", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        fieldnames = list(asdict(self.results[0]).keys())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for result in self.results:
                writer.writerow(asdict(result))
        messagebox.showinfo("Export complete", f"Saved {len(self.results)} rows to:\n{path}")

    def export_json(self):
        if not self.results:
            messagebox.showinfo("No results", "Run a test first.")
            return
        path = filedialog.asksaveasfilename(title="Export JSON", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        payload = {
            "summary": self._summary_dict(),
            "results": [asdict(r) for r in self.results],
        }
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        messagebox.showinfo("Export complete", f"Saved JSON report to:\n{path}")

    def export_html(self):
        if not self.results:
            messagebox.showinfo("No results", "Run a test first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export HTML report",
            defaultextension=".html",
            filetypes=[("HTML report", "*.html")],
        )
        if not path:
            return

        summary = self._summary_dict()
        generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for r in self.results[-500:]:
            status = escape(str(r.status))
            err = escape(r.error or "")
            rows.append(
                f"<tr><td>{escape(r.timestamp)}</td><td>{escape(r.method)}</td>"
                f"<td>{status}</td><td>{r.latency_ms:.1f}</td>"
                f"<td>{r.bytes_received}</td><td>{err}</td></tr>"
            )

        cards = "".join(
            f'<div class="card"><span>{escape(str(k)).replace("_", " ").title()}</span>'
            f'<strong>{escape(str(v))}</strong></div>'
            for k, v in summary.items()
            if k not in {"status_counts", "target"}
        )

        status_items = "".join(
            f"<li><span>HTTP {escape(str(code))}</span><strong>{count}</strong></li>"
            for code, count in sorted(self.status_counts.items())
        ) or "<li>No response codes recorded</li>"

        target = escape(str(summary.get("target") or "—"))
        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sentinel LoadLab V4 Report</title>
<style>
:root{{--bg:#07111f;--panel:#0c1728;--panel2:#101d31;--border:#213a5a;--text:#eaf2ff;--muted:#8293ae;--accent:#20d4a7;--blue:#4d8dff}}
*{{box-sizing:border-box}} body{{margin:0;background:linear-gradient(180deg,#07111f,#081522);color:var(--text);font:14px/1.5 Inter,Segoe UI,Arial,sans-serif}}
.wrap{{max-width:1250px;margin:auto;padding:42px 24px}} h1{{font-size:32px;margin:0 0 6px}} .sub{{color:var(--muted);margin-bottom:26px}}
.hero{{padding:22px;border:1px solid var(--border);border-radius:18px;background:var(--panel);margin-bottom:20px}}
.pill{{display:inline-block;padding:5px 9px;border-radius:999px;background:#143e3b;color:var(--accent);font-weight:700;font-size:12px}}
.target{{font-family:Consolas,monospace;color:#b7c8e6;word-break:break-all}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;margin:18px 0}}
.card{{background:var(--panel2);border:1px solid var(--border);border-radius:14px;padding:14px}} .card span{{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}} .card strong{{display:block;font-size:18px;margin-top:5px}}
.section{{margin-top:20px;background:var(--panel);border:1px solid var(--border);border-radius:18px;padding:18px}}
table{{width:100%;border-collapse:collapse;font-size:12px}} th,td{{padding:9px 8px;border-bottom:1px solid #1a304b;text-align:left}} th{{color:#9fb0ca}} tr:hover td{{background:#0e1b2e}}
ul{{padding:0;list-style:none}} li{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a304b}}
.footer{{color:var(--muted);font-size:12px;margin-top:20px}}
</style>
</head>
<body><div class="wrap">
<div class="hero">
<span class="pill">AUTHORIZED • BOUNDED MODE</span>
<h1>Sentinel LoadLab V4</h1>
<div class="sub">Performance test report generated {escape(generated)}</div>
<div class="target">{target}</div>
</div>
<div class="grid">{cards}</div>
<div class="section"><h2>Response distribution</h2><ul>{status_items}</ul></div>
<div class="section"><h2>Recent request samples</h2><div style="overflow:auto"><table>
<thead><tr><th>Timestamp</th><th>Method</th><th>Status</th><th>Latency ms</th><th>Bytes</th><th>Error</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></div>
<div class="footer">Sentinel LoadLab V4 • authorized testing only • GET/HEAD • max 25 RPS • max 20 workers</div>
</div></body></html>"""
        Path(path).write_text(html, encoding="utf-8")
        messagebox.showinfo("Export complete", f"HTML report saved to:\\n{path}")

    @staticmethod
    def _percentile(values, p):
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        k = (len(values) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return values[int(k)]
        return values[f] + (values[c] - values[f]) * (k - f)

    @staticmethod
    def _format_bytes(n):
        value = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024.0 or unit == "GB":
                return f"{value:.1f} {unit}"
            value /= 1024.0
        return f"{value:.1f} GB"


if __name__ == "__main__":
    app = SentinelLoadLab()
    app.mainloop()
