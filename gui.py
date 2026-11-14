#!/usr/bin/env python3
"""llama-shell GUI — 轻量原生 Tkinter。

功能：
  * 模型库浏览（models_dir 下 .gguf，后台解析，不卡 UI）
  * 选中模型 → 自动推荐推理参数（移植 ToshLLM 启发式，recommend.py）
  * 双引擎：vanilla b12212 / ToshLLM（自动或手动选；MoE 自动走 tosh）
  * 生成后端命令、一键运行、实时日志、/health 就绪指示、测速
  * 停止

零第三方 GUI 依赖（仅标准库 tkinter + 本目录 recommend/gguf_fast）。
运行：python3 gui.py
"""
import os, re, sys, glob, time, queue, threading, subprocess, signal, shlex, json, urllib.request, psutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import gguf_fast, recommend  # noqa: E402

IS_WIN = sys.platform == "win32"
MONO = "Consolas" if IS_WIN else "Menlo"

if IS_WIN:
    MODELS_DIR_DEFAULT = os.path.join(BASE, "models")
    VANILLA_BIN_DEFAULT = os.path.join(BASE, "bin", "llama-b12212", "llama-server.exe")
    # 兼容 1.0.0 Vulkan 版本：若 bin 目录不存在，回退到同级 llama-server.exe
    if not os.path.exists(VANILLA_BIN_DEFAULT):
        VANILLA_BIN_DEFAULT = os.path.join(BASE, "llama-server.exe")
    TOSH_BIN_DEFAULT = ""
else:
    MODELS_DIR_DEFAULT = "/Volumes/CT-nvme/llama-bin-win-vulkan-x64/models"
    VANILLA_BIN_DEFAULT = "bin/llama-b12212/llama-server"
    TOSH_BIN_DEFAULT = "/Applications/ToshLLM.app/Contents/Resources/bin/llama-server"
SKIP = ("mmproj", "clip", "whisper")

import json as _json
CONFIG_FILE = os.path.join(BASE, "llama-shell-config.json")

def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_config(cfg: dict):
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass

_cfg = _load_config()
MODELS_DIR = _cfg.get("models_dir", MODELS_DIR_DEFAULT)
VANILLA_ABS = _cfg.get("vanilla_bin", os.path.abspath(VANILLA_BIN_DEFAULT))
TOSH_ABS = _cfg.get("tosh_bin", TOSH_BIN_DEFAULT)
VRAM_OVERRIDE = _cfg.get("vram_override_gb")
GPU_CACHE = _cfg.get("gpu_cache")
STATE_FILE = os.path.join(BASE, "gui_state.json")
PRESET_FILE = os.path.join(BASE, "gui_presets.json")

FREE_EDIT_KEYS = {"ngl", "threads", "spec_draft_model", "spec_draft_n_max", "spec_type",
                  "mmproj", "mmproj_device", "moe_cache_mib"}

def _build_mtp_cands(models_dir: str) -> list:
    cands = sorted(glob.glob(os.path.join(models_dir, "*MTP*.gguf")))
    return cands if cands else ["(无MTP模型，可手输路径)"]

def _build_mmproj_cands(models_dir: str) -> tuple:
    cands = sorted(glob.glob(os.path.join(models_dir, "mmproj*.gguf")))
    display = ["off"] + [os.path.basename(p)[:30] for p in cands]
    m_map = {os.path.basename(p)[:30]: p for p in cands}
    m_map["off"] = ""
    m_map["auto"] = ""
    return cands, display, m_map

MTP_CANDS = _build_mtp_cands(MODELS_DIR)
MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP = _build_mmproj_cands(MODELS_DIR)

def binary_for(engine: str) -> str:
    return TOSH_ABS if engine == "tosh" else VANILLA_ABS


def _shell_str(tokens) -> str:
    out = []
    for t in tokens:
        t = str(t)
        is_gguf = t.lower().endswith(".gguf")
        needs = (not t or is_gguf or any(c in t for c in ' "') or any(ord(c) > 127 for c in t))
        if needs:
            out.append('"' + t.replace('"', '\\"') + '"')
        else:
            out.append(t)
    return " ".join(out)


def health(port: int, host: str = "127.0.0.1") -> bool:
    try:
        return urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1).status == 200
    except Exception:
        return False


def kill_port(port: int) -> list:
    killed = []
    if sys.platform == "win32":
        # 方法1: netstat 找 PID
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, timeout=5, stderr=subprocess.DEVNULL)
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and f":{port} " in parts[1] + " " and parts[3] == "LISTENING":
                    try:
                        pids.add(int(parts[4]))
                    except ValueError:
                        pass
            for pid in pids:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   timeout=3, capture_output=True)
                    killed.append(pid)
                except Exception:
                    pass
        except Exception:
            pass
        # 方法2: psutil 兜底
        try:
            for c in psutil.net_connections(kind="inet"):
                try:
                    if c.laddr and c.laddr.port == port and c.pid and c.pid > 0:
                        if c.pid not in killed:
                            psutil.Process(c.pid).terminate()
                            killed.append(c.pid)
                except (psutil.NoSuchProcess, AttributeError, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return killed
    try:
        out = subprocess.check_output(["lsof", "-ti", str(port)],
                                      text=True, timeout=3, stderr=subprocess.DEVNULL)
        pids = [int(x) for x in out.split() if x.strip()]
        for pid in pids:
            try:
                subprocess.run(["kill", "-9", str(pid)],
                               timeout=2, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        return pids
    except Exception:
        pass
    return killed


def bench_once(port: int, host: str = "127.0.0.1", n: int = 48) -> float:
    body = {"prompt": "Explain in a few sentences how a CPU pipeline works.",
            "max_tokens": n, "stream": False, "temperature": 0}
    req = urllib.request.Request(f"http://{host}:{port}/completion",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    return d.get("n_tokens_predicted", n) / (time.time() - t0)


def scan_models(models_dir: str = None) -> list:
    if models_dir is None:
        models_dir = MODELS_DIR
    out = []
    for p in sorted(glob.glob(os.path.join(models_dir, "*.gguf"))):
        b = os.path.basename(p)
        if b.startswith("._") or any(s in b.lower() for s in SKIP):
            continue
        try:
            h = gguf_fast.parse_gguf_header(p)
            st = recommend.model_stats(h)
        except Exception as e:
            st = {"name": b, "path": p, "file_gb": 0, "is_moe": False,
                  "total_b": 0, "active_b": 0, "expert_count": 0,
                  "error": str(e)}
        out.append(st)
    return out


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.proc = None
        self._reader_stop = False
        self._health_stop = False
        self._dynamic_moe_used = False
        self._auto_retried = False
        self._last_cmd = ""
        self._last_argv = []
        self.hw = recommend.detect_hardware(
            vram_override_gb=VRAM_OVERRIDE, gpu_cache=GPU_CACHE)
        try:
            if getattr(self.hw, "gpu_probe_src", None):
                _save_config(_load_config() | {
                    "gpu_cache": {"ts": time.time(), "src": self.hw.gpu_probe_src,
                                  "gpus": self.hw.gpus}})
        except Exception:
            pass
        self.st_cache = {}
        self.selected = None
        self._restoring = False
        self._user_stopped = False
        self._mem_total = None
        self.models_dir = MODELS_DIR
        self._build()
        self._refresh_hw_bar()
        self._scan_async()
        self.root.after(80, self._poll)
        self.root.after(2000, self._hw_tick)
        self._gpu_last = None
        if sys.platform == "win32" and self.hw.vram_gb > 0.2:
            threading.Thread(target=self._gpu_sampler, daemon=True).start()

    def _build(self):
        root = self.root
        root.title("llama-shell · 模型库 + 参数推荐")
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.hw_lbl = tk.Label(root, anchor="w", padx=10, pady=4,
                               bg="#d7e0ee", fg="#16202e",
                               font=(MONO, 9), relief="flat")
        self.hw_lbl.pack(fill="x")

        # 顶部栏：引擎路径（非Windows）+ 模型库目录 + 服务地址
        topbar = ttk.Frame(root)
        topbar.pack(fill="x", padx=8, pady=(4, 2))
        if not IS_WIN:
            ttk.Label(topbar, text="vanilla:", width=8, anchor="w").pack(side="left")
            self.vanilla_var = tk.StringVar(value=VANILLA_ABS)
            ttk.Entry(topbar, textvariable=self.vanilla_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
            ttk.Button(topbar, text="…", width=3,
                       command=lambda: self._browse_engine("vanilla")).pack(side="left")
            ttk.Label(topbar, text="tosh:", width=8, anchor="w").pack(side="left")
            self.tosh_var = tk.StringVar(value=TOSH_ABS)
            ttk.Entry(topbar, textvariable=self.tosh_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
            ttk.Button(topbar, text="…", width=3,
                       command=lambda: self._browse_engine("tosh")).pack(side="left")

        dirbar = ttk.Frame(root)
        dirbar.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(dirbar, text="模型库:", width=8, anchor="w").pack(side="left")
        self.dir_var = tk.StringVar(value=self.models_dir)
        dir_entry = ttk.Entry(dirbar, textvariable=self.dir_var)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(dirbar, text="浏览…", width=8,
                   command=self._browse_models_dir).pack(side="left")
        ttk.Button(dirbar, text="🔄 刷新", width=8,
                   command=self._scan_async).pack(side="left", padx=(4, 0))

        # 服务地址栏：Host / Port / 自定义 llama-server 二进制路径
        srvbar = ttk.Frame(root)
        srvbar.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(srvbar, text="服务地址:", width=8, anchor="w").pack(side="left")
        ttk.Label(srvbar, text="Host").pack(side="left", padx=(4, 2))
        self.host_var = tk.StringVar(value="127.0.0.1")
        ttk.Combobox(srvbar, textvariable=self.host_var,
                     values=["127.0.0.1", "0.0.0.0"], width=10, state="readonly").pack(side="left")
        ttk.Label(srvbar, text="端口").pack(side="left", padx=(12, 2))
        self.port_var = tk.StringVar(value="8080")
        ttk.Spinbox(srvbar, from_=1000, to=65535, textvariable=self.port_var,
                   width=7).pack(side="left")
        ttk.Label(srvbar, text="llama引擎路径").pack(side="left", padx=(12, 2))
        self.custom_srv_var = tk.StringVar(value=_cfg.get("custom_srv", _cfg.get("vanilla_bin", "")))
        self.custom_srv_var.trace_add("write", lambda *_: (self._refresh_hw_bar(), self._save_custom_srv()))
        ttk.Entry(srvbar, textvariable=self.custom_srv_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(srvbar, text="…", width=3,
                   command=lambda: self._browse_engine("custom")).pack(side="left")
        ttk.Label(srvbar, text="(指定 llama-server 路径，留空则用引擎默认)", foreground="#888", font=(MONO, 8)).pack(side="left", padx=(4, 0))

        # 主区域：左=模型库+日志，右=参数面板
        main = ttk.Frame(root)
        main.pack(fill="x", padx=6, pady=2)

        # LEFT: 模型库列表（上）+ 日志（下）
        leftcol = ttk.Frame(main, width=600)
        leftcol.pack(side="left", fill="y", padx=(0, 4))
        leftcol.pack_propagate(False)

        left_top = ttk.LabelFrame(leftcol, text=" 模型库列表 ")
        left_top.pack(side="top", fill="x", pady=(0, 4))
        # 新列：Model, GB, B, Layers, MTP/MoE（可排序）
        cols = ("name", "gb", "b", "layers", "mtpmoe")
        self.tree = ttk.Treeview(left_top, columns=cols, show="headings", height=10)
        headings = (("name", "Model", 250), ("gb", "GB", 46), ("b", "B", 40),
                    ("layers", "Layers", 44), ("mtpmoe", "MTP/MoE", 72))
        for c, t, w in headings:
            self.tree.heading(c, text=t, anchor="w")
            self.tree.column(c, width=w, anchor="w")
        self._sortable_cols = ("name", "gb", "b")
        self._sort_dirs = {}
        self._sort_active_col = "name"
        for c in self._sortable_cols:
            self.tree.heading(c, command=lambda *a, col=c: self._sort_col(col))
        vsb = ttk.Scrollbar(left_top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        left_bot = ttk.LabelFrame(leftcol, text=" 日志 ")
        left_bot.pack(side="top", fill="both", expand=True)
        self.log = tk.Text(left_bot, height=12, font=(MONO, 9), wrap="word", state="disabled", bg="#1e1e1e", fg="#d4d4d4")
        self.log.pack(side="left", fill="both", expand=True, padx=4, pady=2)
        log_sb = ttk.Scrollbar(left_bot, orient="vertical", command=self.log.yview)
        log_sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_sb.set)

        # RIGHT: 引擎选择 + 参数面板（无滚动条）+ 自定义覆盖(3行) + 控制按钮 + 硬件负载+后端命令
        right = ttk.Frame(main)
        right.pack(side="left", fill="y", padx=4)

        f_eng = ttk.Frame(right)
        f_eng.pack(fill="x")
        ttk.Label(f_eng, text="引擎").pack(side="left", padx=(0, 4))
        self.engine_var = tk.StringVar(value="vanilla" if IS_WIN else "auto")
        self.engine_var.trace_add("write", lambda *_: self._save_state_entry())
        if IS_WIN:
            ttk.Label(f_eng, text="vanilla（Windows 固定）", foreground="#555").pack(side="left", padx=2)
        else:
            for v in ("auto", "vanilla", "tosh"):
                ttk.Radiobutton(f_eng, text=v, variable=self.engine_var,
                                value=v, command=self._refresh_recommend).pack(side="left", padx=2)
        self.dm_var = tk.BooleanVar(value=False)
        self.dm_var.trace_add("write", lambda *_: self._save_state_entry())
        if not IS_WIN:
            ttk.Checkbutton(f_eng, text="--dynamic-moe", variable=self.dm_var,
                            command=self._refresh_recommend).pack(side="left", padx=(10, 0))

        # 三列参数面板：原始布局（37参数），无滚动条，全部显示
        style = ttk.Style()
        panel_bg = style.lookup("TLabelFrame", "background") or "#f0f0f0"

        f_p = ttk.LabelFrame(right, text=" 参数 ")
        f_p.pack(fill="x", pady=(4, 0))
        self.pv = {}
        self.widgets = {}
        hw = self.hw
        tdef = str(hw.physical_cores) if hw.physical_cores else "auto"
        specs = [("① CPU/MoE",[
            ("ngl","ngl GPU层数 -ngl",["default","auto","0","4","8","12","16","24","32","40","48","64","96","999"], "auto"),
            ("threads", "threads -t",
                     ["default","auto","4","6","8","12"], tdef),
            ("cpu_moe", "cpu-moe", ["default","off","on"], "off"),
            ("moe_cache_mib", "moe-cache-mib",
                     ["default","0","512","1024","1536","2048","2560","3072","3584","4096"], "0"),
            ("moe_cache_cpu_share", "moe-cache-cpu-share",
                     ["default","0.0","0.25","0.5","1.0"], "0.0"),
            ("batch", "batch -b", ["default","256","512","1024","2048","4096"], "512"),
            ("ubatch", "ubatch -ub", ["default","256","512","1024","2048"], "512"),
            ("no_warmup", "no-warmup", ["default","auto","off","on"], "auto"),
            ("mmproj", "视觉塔 -mm", ["default"] + MMPROJ_DISPLAY, "off"),
            ("mmproj_device", "--mmproj-device", ["default","none","cpu","gpu"], "none"),
            ("image_min_tokens", "图像最小token", ["default","256","512","1024","2048"], "256"),
            ("image_max_tokens", "图像最大token", ["default","1024","2048","4096","8192","16384"], "4096"),
            ("agent", "agent", ["default","off","on"], "on"),
        ]),
        ("② 上下文/缓存", [("ctx","ctx -c",["default","4096","8192","16384","32768","65536","131072"], "65536"),
            ("flash_attn", "flash-attn -fa", ["default","auto","on","off"], "on"),
            ("cache_type_k", "K量化 -ctk", ["default","f16","q8_0","q5_1","q5_0","q4_0","iq4_nl"], "q8_0"),
            ("cache_type_v", "V量化 -ctv", ["default","f16","q8_0","q5_1","q5_0","q4_0","iq4_nl"], "q8_0"),
            ("spec_type", "MTP -st", ["default","off","draft-mtp","draft-eagle3","draft-simple","ngram-simple","ngram-cache"], "off"),
            ("spec_draft_model", "MTP draft -md", ["default"] + MTP_CANDS, "default"),
            ("spec_draft_n_max", "MTP n-max", ["default","1","2","4","8","16","32"], "default"),
            ("spec_draft_cache_type_k", "MTP draft K量化 -ctkd", ["default","f16","q8_0","q5_1","q5_0","q4_0","iq4_nl"], "q8_0"),
            ("spec_draft_cache_type_v", "MTP draft V量化 -ctvd", ["default","f16","q8_0","q5_1","q5_0","q4_0","iq4_nl"], "q8_0"),
            ("cache_reuse", "cache-reuse", ["default","0","512","2048","4096"], "0"),
            ("swa_full", "swa-full (off=按计划)", ["default","auto","off","on"], "auto"),
            ("load_mode", "load-mode -lm", ["default","auto","mmap","mlock","mmap+mlock","none","dio"], "auto"),
        ]),
        ("③ 采样/思考", [("temp","temp",["default","0.0","0.2","0.5","0.7","0.75","0.8","1.0","1.2"], "0.75"),
            ("top_k", "top-k", ["default","40","20","5","100","0"], "40"),
            ("top_p", "top-p", ["default","0.95","0.90","0.85","0.99","1.0"], "0.95"),
            ("min_p", "min-p", ["default","0.05","0","0.1","0.2"], "0.05"),
            ("repeat_penalty", "repeat-penalty", ["default","1.0","1.05","1.1","1.15","1.2","1.3"], "1.1"),
            ("presence_penalty", "presence-penalty", ["default","0","0.05","0.1","0.2","0.5","1.0"], "0.05"),
            ("frequency_penalty", "frequency-penalty", ["default","0","0.1","0.2","0.5","1.0"], "0"),
            ("seed", "seed (-1=随机)", ["default","-1","42","1234"], "-1"),
            ("reasoning", "思考模式 reasoning", ["default","auto","on","off"], "auto"),
            ("reasoning_effort", "reasoning-effort", ["default","minimal","low","medium","high","max"], "default"),
            ("reasoning_format", "reasoning-format", ["default","auto","none","deepseek","deepseek-legacy"], "auto"),
            ("jinja", "jinja", ["default","off","on"], "on"),
        ]),
        ]
        self.param_defaults = {}
        for title, items in specs:
            col = tk.LabelFrame(f_p, text=" " + title + " ", bg=panel_bg, fg="#000", font=(MONO, 9))
            col.pack(side="left", fill="both", expand=True, padx=2, pady=2)
            col.grid_columnconfigure(1, weight=1)
            for i, (key, label, choices, default) in enumerate(items):
                var = tk.StringVar(value=default)
                self.pv[key] = var
                self.param_defaults[key] = default
                var.trace_add("write", lambda *_: self._save_state_entry())
                ttk.Label(col, text=label).grid(row=i, column=0, sticky="e", padx=(4, 2), pady=1)
                cb = ttk.Combobox(col, textvariable=var, values=choices, width=12, state="readonly")
                cb.grid(row=i, column=1, sticky="e", padx=2)
                self.widgets[key] = cb
                var.set(default)
            if title.startswith("③"):
                col.grid_columnconfigure(2, minsize=13)

        # 自定义覆盖：3行
        f_ovr = ttk.LabelFrame(right, text=" 自定义覆盖（原生 flag，空行=默认/不下发） ")
        f_ovr.pack(fill="x", pady=(4, 0))
        self.ovr = tk.Text(f_ovr, height=3, font=(MONO, 10), wrap="none")
        self.ovr.pack(fill="x", padx=4, pady=2)
        ovr_sb = ttk.Scrollbar(f_ovr, orient="horizontal", command=self.ovr.xview)
        ovr_sb.pack(fill="x")
        self.ovr.configure(xscrollcommand=ovr_sb.set)

        # 控制按钮
        ctrl = ttk.Frame(right)
        ctrl.pack(fill="x", pady=(4, 0))
        self.run_cv = tk.Canvas(ctrl, height=36, width=90, highlightthickness=0)
        self.run_cv.pack(side="left", padx=(0, 4))
        self.run_bg = self.run_cv.create_rectangle(2, 2, 88, 34, fill="#9e9e9e", outline="#757575", tags="run_bg")
        self.run_txt = self.run_cv.create_text(45, 18, text="▶ 运行", fill="white", font=(MONO, 12, "bold"), tags="run_txt")
        self.run_cv.tag_bind("run_bg", "<Button-1>", lambda e: self._spawn())
        self.run_cv.tag_bind("run_txt", "<Button-1>", lambda e: self._spawn())
        self.run_cv.bind("<Button-1>", lambda e: self._spawn())
        self.run_cv.bind("<Button-1>", lambda e: self._spawn(), add="+")

        self.stop_cv = tk.Canvas(ctrl, height=36, width=90, highlightthickness=0)
        self.stop_cv.pack(side="left", padx=4)
        self.stop_bg = self.stop_cv.create_rectangle(2, 2, 88, 34, fill="#9e9e9e", outline="#757575", tags="stop_bg")
        self.stop_txt = self.stop_cv.create_text(45, 18, text="■ 停止", fill="white", font=(MONO, 12, "bold"), tags="stop_txt")
        self.stop_cv.tag_bind("stop_bg", "<Button-1>", lambda e: self._stop())
        self.stop_cv.tag_bind("stop_txt", "<Button-1>", lambda e: self._stop())
        self.stop_cv.bind("<Button-1>", lambda e: self._stop())
        self.stop_cv.bind("<Button-1>", lambda e: self._stop(), add="+")

        self.clear_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="清空模式", variable=self.clear_mode_var,
                        command=lambda: (self._refresh_recommend(), self._save_state_entry())).pack(side="left", padx=8)

        ttk.Label(ctrl, text="预设:").pack(side="left", padx=(12, 2))
        self.preset_var = tk.StringVar()
        self.preset_cb = ttk.Combobox(ctrl, textvariable=self.preset_var, width=18, state="readonly")
        self.preset_cb.pack(side="left")
        self.preset_cb.bind("<<ComboboxSelected>>", lambda e: self._load_preset())

        ttk.Button(ctrl, text="保存预设", width=10, command=self._save_preset).pack(side="left", padx=2)
        ttk.Button(ctrl, text="导入 JSON", width=10, command=self._import).pack(side="right", padx=(2, 0))
        ttk.Button(ctrl, text="导出 JSON", width=10, command=self._export).pack(side="right", padx=(0, 6))

        # 生成/复制命令 单独一行（靠右，与导入/导出对齐）
        cmd_btns = ttk.Frame(right)
        cmd_btns.pack(fill="x", pady=(2, 0))
        ttk.Button(cmd_btns, text="复制命令", width=10, command=self._copy_cmd).pack(side="right", padx=(2, 0))
        ttk.Button(cmd_btns, text="生成命令", width=10, command=self._refresh_recommend).pack(side="right", padx=(0, 6))

        # 硬件负载 + 后端命令（最底部）
        bottom_right = ttk.Frame(right)
        bottom_right.pack(fill="x", pady=(2, 0))

        hwf = ttk.LabelFrame(bottom_right, text=" 硬件负载 ")
        hwf.pack(side="left", fill="y", padx=(0, 4))
        self.hw_canvas = tk.Canvas(hwf, width=200, height=80, highlightthickness=0)
        self.hw_canvas.pack(padx=4, pady=2)

        cmdf = ttk.LabelFrame(bottom_right, text=" 后端命令 ")
        cmdf.pack(side="left", fill="both", expand=True)
        self.cmd = tk.Text(cmdf, height=3, font=(MONO, 9), wrap="word", state="disabled", bg="#f5f5f5")
        self.cmd.pack(fill="both", expand=True, padx=4, pady=2)

        self._refresh_preset_names()
        self._refresh_dynamic_combos()

    def _scan_async(self):
        self._log(f"扫描模型库 {self.models_dir} …")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            models = scan_models(self.models_dir)
        except Exception as e:
            self.q.put(f"[错误] 扫描失败: {e}")
            return
        for st in models:
            self.st_cache[st["path"]] = st
        self.q.put("__SCAN_DONE__" + json.dumps([
            {k: st.get(k) for k in ("name", "total_b", "is_moe", "file_gb", "path", "layers", "mtp_layers")}
            for st in models]))

    def _browse_models_dir(self):
        import tkinter.filedialog as fd
        d = fd.askdirectory(initialdir=self.models_dir, title="选择模型库目录")
        if d:
            self.models_dir = d
            self.dir_var.set(d)
            _save_config(_load_config() | {"models_dir": d})
            self._log(f"模型库已切换 → {d}")
            global MTP_CANDS, MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP
            MTP_CANDS = _build_mtp_cands(d)
            MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP = _build_mmproj_cands(d)
            for key, widget in self.widgets.items():
                if key == "spec_draft_model":
                    widget["values"] = MTP_CANDS
                elif key == "mmproj":
                    widget["values"] = MMPROJ_DISPLAY
            self._scan_async()

    def _browse_engine(self, which: str):
        import tkinter.filedialog as fd
        if which == "custom":
            cur = self.custom_srv_var.get()
        else:
            cur = self.vanilla_var.get() if which == "vanilla" else self.tosh_var.get()
        p = fd.askopenfilename(initialdir=os.path.dirname(cur) if cur else BASE,
                               title=f"选择 {which} 引擎二进制" if which != "custom" else "选择 llama-server 二进制",
                               filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if p:
            if which == "vanilla":
                self.vanilla_var.set(p)
            elif which == "tosh":
                self.tosh_var.set(p)
            elif which == "custom":
                self.custom_srv_var.set(p)
            cfg = _load_config()
            if which == "custom":
                cfg["custom_srv"] = p
            else:
                cfg[which + "_bin"] = p
            _save_config(cfg)
            global VANILLA_ABS, TOSH_ABS
            if which == "vanilla":
                VANILLA_ABS = p
            elif which == "tosh":
                TOSH_ABS = p
            self._refresh_hw_bar()
            self._log(f"引擎路径已更新: {which} → {p}")

    def _fill_tree(self, models):
        self._rows = []
        for st in models:
            b = st.get("total_b", 0)
            layers = st.get("layers", 0)
            flags = []
            if st.get("mtp_layers"): flags.append("MTP")
            if st.get("is_moe"): flags.append("MoE")
            mtpmoe = "+".join(flags) if flags else "-"
            row = {
                "name": st.get("name", "?"),
                "gb": round(st.get("file_gb", 0), 2),
                "b": b,
                "layers": layers,
                "mtpmoe": mtpmoe,
                "path": st.get("path", ""),
                "st": st,
            }
            self._rows.append(row)
        self._resort_and_fill()

    def _resort_and_fill(self):
        col = self._sort_active_col
        direction = self._sort_dirs.get(col, 1 if col == "name" else 1)
        if col in ("gb", "b"):
            key = lambda r: r[col]
        else:
            key = lambda r: r["name"].lower()
        rows = sorted(self._rows, key=key, reverse=(direction < 0))
        self.tree.delete(*self.tree.get_children())
        prev_sel_path = self.selected.get("path") if self.selected else None
        sel_iid = None
        for r in rows:
            iid = self.tree.insert("", "end",
                                   values=(r["name"],
                                           f'{r["gb"]:.2f}',
                                           f"{r['b']:.0f}" if r["b"] else "-",
                                           f"{r['layers']}",
                                           r["mtpmoe"],
                                           r["path"]))
            if prev_sel_path and r["path"] == prev_sel_path:
                sel_iid = iid
        for c, t, w in (("name", "Model", 250), ("gb", "GB", 46), ("b", "B", 40),
                        ("layers", "Layers", 44), ("mtpmoe", "MTP/MoE", 72)):
            arrow = ""
            if c == col:
                arrow = " ↑" if direction > 0 else " ↓"
            self.tree.heading(c, text=t + arrow)
        if sel_iid:
            self.tree.selection_set(sel_iid)
            self.tree.focus(sel_iid)
            self.tree.see(sel_iid)

    def _sort_col(self, col):
        if col not in self._sortable_cols:
            return
        default_dir = 1 if col == "name" else 1
        cur = self._sort_dirs.get(col, default_dir)
        self._sort_dirs[col] = -cur
        self._sort_active_col = col
        self._resort_and_fill()

    def _on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        pid = self.tree.item(sel[0], "values")[-1]
        st = self.st_cache.get(pid)
        if not st:
            return
        self.selected = st
        key = st.get("path")
        saved = (self._load_state().get(key) or {}).get("params") or {}
        if saved:
            self._restore_model_state()
        else:
            self._apply_prefill()
        self._refresh_recommend()

    def _resolve_engine(self):
        if not self.selected:
            return "vanilla", VANILLA_ABS, "（未选模型）"
        engine, _bin, notes = recommend.pick_engine(self.selected, self.hw,
                                                     forced=self.engine_var.get())
        binary = binary_for(engine)
        return engine, binary, notes

    def _params(self) -> dict:
        p = {k: v.get() for k, v in self.pv.items()}
        mm = p.get("mmproj", "")
        if mm in MMPROJ_MAP:
            p["mmproj"] = MMPROJ_MAP[mm]
        p["dynamic_moe"] = "on" if self.dm_var.get() else "off"
        return p

    def _on_param_change(self, key):
        self._save_state_entry()
        self._refresh_recommend()

    def _on_ovr_edit(self, _e=None):
        self._refresh_recommend()
        self._save_state_entry()

    def _state_key(self):
        return self.selected.get("path") if self.selected else None

    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    def _save_state(self, data: dict):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass

    def _save_custom_srv(self):
        """保存 llama引擎路径到配置，启动时恢复。"""
        try:
            val = self.custom_srv_var.get().strip()
            cfg = _load_config()
            if val:
                cfg["custom_srv"] = val
            else:
                cfg.pop("custom_srv", None)
            _save_config(cfg)
        except Exception:
            pass

    def _save_state_entry(self):
        key = self._state_key()
        if not key:
            return
        d = self._load_state()
        panel = {k: self.pv[k].get() for k in self.pv}
        ovr_text = self.ovr.get("1.0", "end").strip()
        d[key] = {"params": panel,
                  "override": ovr_text,
                  "engine": self.engine_var.get(),
                  "port": self.port_var.get(),
                  "host": self.host_var.get(),
                  "clear_mode": self.clear_mode_var.get(),
                  "dynamic_moe": self.dm_var.get()}
        self._save_state(d)

    def _restore_model_state(self):
        key = self._state_key()
        if not key:
            return
        d = self._load_state()
        entry = d.get(key)
        if not entry:
            return
        self._restoring = True
        try:
            for k, v in (entry.get("params") or {}).items():
                if k in self.pv:
                    self.pv[k].set(v)
            self.engine_var.set(entry.get("engine", "vanilla" if IS_WIN else "auto"))
            self.port_var.set(entry.get("port", "8080"))
            self.host_var.set(entry.get("host", "127.0.0.1"))
            self.clear_mode_var.set(entry.get("clear_mode", False))
            self.dm_var.set(entry.get("dynamic_moe", False))
        finally:
            self._restoring = False
        self._refresh_recommend()

    def _apply_prefill(self):
        if not self.selected:
            return
        plan = recommend.recommend(self.selected, self.hw, ctx=65536, kv_type="q8_0", ncmoe=None)
        for k, v in plan.items():
            if k in self.pv and self.pv[k].get() in ("default", "", "不配置"):
                self.pv[k].set(str(v))

    def _refresh_recommend(self):
        if not self.selected or self._restoring:
            return
        engine, binary, notes = self._resolve_engine()
        params = self._params()
        ovr = self.ovr.get("1.0", "end").strip()
        clear_mode = self.clear_mode_var.get()
        # 自定义二进制路径：优先使用 custom_srv_var
        if self.custom_srv_var.get().strip():
            binary = self.custom_srv_var.get().strip()
        # Host/端口始终用于生成命令
        use_host, use_port = self.host_var.get(), int(self.port_var.get())
        try:
            if clear_mode:
                cmd_list = recommend.build_command_clear(self.selected, binary,
                                                use_port, use_host,
                                                override_args=ovr)
                cmd_str = _shell_str(cmd_list)
                self._set_cmd(cmd_str)
                self._log(f"推荐引擎: {engine} | {notes} (清空模式)")
            else:
                plan = recommend.recommend(self.selected, self.hw, ctx=int(params.get("ctx", 65536)) if params.get("ctx", "default").isdigit() else 65536,
                                       kv_type=params.get("cache_type", "q8_0"), ncmoe=None)
                cmd_list = recommend.build_command(self.selected, self.hw, plan, binary,
                                          use_port, use_host,
                                          params=params, override_args=ovr)
                cmd_str = _shell_str(cmd_list)
                self._set_cmd(cmd_str)
                self._log(f"推荐引擎: {engine} | {notes}")
        except Exception as e:
            self._set_cmd(f"# 生成命令失败: {e}")
            self._log(f"[错误] {e}")


    def _set_cmd(self, text: str):
        self.cmd.config(state="normal")
        self.cmd.delete("1.0", "end")
        self.cmd.insert("1.0", text)
        self.cmd.config(state="disabled")

    def _spawn(self, argv=None, cmd_str=None):
            if self.proc:
                return
            if argv is None:
                cmd_text = self.cmd.get("1.0", "end").strip()
                if not cmd_text or cmd_text.startswith("#"):
                    self._log("[错误] 无有效命令")
                    return
                argv = shlex.split(cmd_text)
            self._user_stopped = False
            self._reader_stop = False
            self._health_stop = False
            # 先清理端口，再启动
            killed = kill_port(int(self.port_var.get()))
            if killed:
                self._log(f"[端口清理] 已终止 PID: {killed}")
                time.sleep(0.5)  # 等待端口释放
            self._log(f"启动: {cmd_str or ' '.join(str(x) for x in argv)}")
            self._set_run_ui(True, False)  # 启动中，未就绪
            self._health_port = self.port_var.get()
            self._health_host = self.host_var.get()
            threading.Thread(target=self._run_worker, args=(argv,), daemon=True).start()

    def _run_worker(self, argv):
        try:
            # cwd 设为二进制所在目录，确保 llama-server 能找到同目录的 DLL
            bin_dir = os.path.dirname(argv[0]) if argv else BASE
            if IS_WIN:
                self.proc = subprocess.Popen(
                    argv, cwd=bin_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=0x08000000,
                    text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
            else:
                self.proc = subprocess.Popen(
                    ' '.join(argv), shell=True, cwd=bin_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                    text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
        except Exception as e:
            self.q.put(f"OUT:[错误] 启动失败: {e}")
            return
        self._reader_stop = False
        threading.Thread(target=self._reader, daemon=True).start()
        self._health_stop = False
        threading.Thread(target=self._health_poll, daemon=True).start()

    def _reader(self):
        p = self.proc
        from collections import deque
        recent = deque(maxlen=4)
        for line in iter(p.stdout.readline, ""):
            if self._reader_stop:
                break
            stripped = line.rstrip()
            # 去重：如果和最近4行中任何一行相同则跳过
            if stripped in recent:
                continue
            recent.append(stripped)
            self.q.put("OUT:" + line)
        if ("TOSH_AUTO found no configuration that fits" in line
                or "PLAN_UNSUPPORTED" in line):
            if self._dynamic_moe_used and not self._auto_retried:
                self._auto_retried = True
                self.q.put("__NOOP__")
        rc = p.wait()
        self.q.put(f"PROC_EXIT:{p.pid}:{rc}")

    def _health_poll(self):
        port = self._health_port
        host = self._health_host
        while not self._health_stop:
            time.sleep(0.5)
            try:
                if health(port, host):
                    self.q.put("HEALTH:1")
                    break
            except Exception:
                pass

    def _stop(self):
        if not self.proc:
            return
        self._user_stopped = True
        self._reader_stop = True
        self._health_stop = True
        kill_port(int(self.port_var.get()))
        try:
            self.proc.terminate()
        except Exception:
            pass
        self._log("已发送停止信号")

    def _set_run_ui(self, running: bool, ready: bool = False):
        if running and ready:
        # 正在运行且已就绪：绿色"正在运行"，红色"停止"
            self.run_cv.itemconfig(self.run_bg, fill="#2e7d32", outline="#1b5e20")
            self.run_cv.itemconfig(self.run_txt, text="正在运行")
            self.stop_cv.itemconfig(self.stop_bg, fill="#c62828", outline="#b71c1c")
            self.stop_cv.itemconfig(self.stop_txt, text="停止")
        elif running:
            # 启动中未就绪：橙色"启动中…"
            self.run_cv.itemconfig(self.run_bg, fill="#f57c00", outline="#e65100")
            self.run_cv.itemconfig(self.run_txt, text="启动中…")
            self.stop_cv.itemconfig(self.stop_bg, fill="#c62828", outline="#b71c1c")
            self.stop_cv.itemconfig(self.stop_txt, text="■ 停止")
        else:
            # 未运行：灰色"▶ 运行"，灰色"■ 停止"
            self.run_cv.itemconfig(self.run_bg, fill="#9e9e9e", outline="#757575")
            self.run_cv.itemconfig(self.run_txt, text="▶ 运行")
            self.stop_cv.itemconfig(self.stop_bg, fill="#9e9e9e", outline="#757575")
            self.stop_cv.itemconfig(self.stop_txt, text="■ 停止")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item == "__NOOP__":
                    break
                if item.startswith("__SCAN_DONE__"):
                    self._fill_tree(_json.loads(item[len("__SCAN_DONE__"):]))
                    self._log(f"已载入 {len(self.st_cache)} 个模型")
                elif item.startswith("OUT:"):
                    self._log(item[4:])
                elif item.startswith("HEALTH:"):
                    ok = item[7:] == "1"
                    self._set_run_ui(True, ok)  # 健康检查通过=ready
                elif item.startswith("PROC_EXIT:"):
                    pidd, _, rc = item[len("PROC_EXIT:"):].partition(":")
                    rc = rc.strip() or "?"
                    cur = str(self.proc.pid) if self.proc else ""
                    if pidd and cur and pidd != cur:
                        self._log(f"[内部] 旧进程事件已忽略 rc={rc} 原始={item!r}")
                    else:
                        self.proc = None
                        self._set_run_ui(False)
                        if rc in ("0", ""):
                            self._log("进程已退出 rc=0（正常）")
                        elif getattr(self, "_user_stopped", False):
                            self._log(f"已停止（用户停止，rc={rc}，正常）")
                        else:
                            try:
                                port = (self.port_var.get() or "").strip() or "8080"
                            except tk.TclError:
                                port = "8080"
                            self._log(f"进程已退出 rc={rc}（非零）。常见原因：端口 {port} 被其他实例占用 / "
                                      f"模型路径错误 / OOM / Vulkan 驱动异常")
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _log(self, msg: str):
        # 去重：如果最后一行和 msg 相同则跳过（防止 stderr 合并导致重复）
        last = self.log.get("end-2c", "end-1c").strip()
        if last == msg.strip():
            return
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _refresh_hw_bar(self):
        hw = self.hw
        gpus = hw.gpus
        if gpus:
            gpu_str = ", ".join(f"{g['name']} {g['vram_gb']:.0f}G{'i' if g.get('integrated') else ''}" for g in gpus)
        else:
            gpu_str = "无 GPU"
        engine_label = "vanilla" if IS_WIN else self.engine_var.get()
        bin_path = binary_for(engine_label)
        # 如果用户指定了 llama引擎路径，覆盖显示
        custom = self.custom_srv_var.get().strip() if hasattr(self, "custom_srv_var") else ""
        if custom:
            bin_path = custom
        self.hw_lbl.config(text=f"CPU: {hw.cpu_brand} ({hw.physical_cores}C/{hw.logical_cores}T) | "
                                f"RAM: {hw.ram_gb:.0f}G | GPU: {gpu_str} | Engine: {engine_label} ({bin_path})")

    def _hw_tick(self):
        self._update_hwload()
        self.root.after(2000, self._hw_tick)

    def _update_hwload(self):
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            ram_pct = mem.percent
        except Exception:
            cpu_pct = ram_pct = None
        gpu_pct = None
        if IS_WIN:
            try:
                import wmi
                w = wmi.WMI(namespace=r"root\CIMV2")
                for gpu in w.Win32_VideoController():
                    if "AMD" in gpu.Name or "Radeon" in gpu.Name:
                        gpu_pct = 0
                        break
            except Exception:
                pass
        c = self.hw_canvas
        c.delete("all")
        w, h = 190, 80
        # CPU
        c.create_text(5, 2, anchor="nw", text=f"CPU: {cpu_pct:.0f}%" if cpu_pct is not None else "CPU: n/a",
                      font=(MONO, 9), fill="#16202e")
        c.create_rectangle(5, 14, w-5, 22, fill="#e0e0e0", outline="#999")
        if cpu_pct is not None:
            c.create_rectangle(5, 14, 5 + (w-10) * cpu_pct / 100, 22, fill="#2e7d32", outline="")
        # RAM
        c.create_text(5, 26, anchor="nw", text=f"RAM: {ram_pct:.0f}%" if ram_pct is not None else "RAM: n/a",
                      font=(MONO, 9), fill="#16202e")
        c.create_rectangle(5, 38, w-5, 46, fill="#e0e0e0", outline="#999")
        if ram_pct is not None:
            c.create_rectangle(5, 38, 5 + (w-10) * ram_pct / 100, 46, fill="#1565c0", outline="")
        # GPU
        c.create_text(5, 50, anchor="nw", text=f"GPU: {gpu_pct:.0f}%" if gpu_pct is not None else "GPU: n/a",
                      font=(MONO, 9), fill="#16202e")
        c.create_rectangle(5, 62, w-5, 70, fill="#e0e0e0", outline="#999")
        if gpu_pct is not None:
            c.create_rectangle(5, 62, 5 + (w-10) * gpu_pct / 100, 70, fill="#f57c00", outline="")

    def _gpu_sampler(self):
        while not self._health_stop:
            time.sleep(2)

    def _copy_cmd(self):
        txt = self.cmd.get("1.0", "end").strip()
        if txt:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self._log("命令已复制到剪贴板")

    def _refresh_preset_names(self):
        try:
            with open(PRESET_FILE, encoding="utf-8") as f:
                data = _json.load(f)
            names = list(data.keys())
        except Exception:
            names = []
        self.preset_cb["values"] = names
        if names and not self.preset_var.get():
            self.preset_var.set(names[0])

    def _load_preset(self):
        name = self.preset_var.get()
        if not name:
            return
        try:
            with open(PRESET_FILE, encoding="utf-8") as f:
                data = _json.load(f)
            preset = data.get(name)
            if not preset:
                return
            self._restoring = True
            try:
                for k, v in (preset.get("params") or {}).items():
                    if k in self.pv:
                        self.pv[k].set(v)
                self.ovr.delete("1.0", "end")
                self.ovr.insert("1.0", preset.get("override", ""))
                self.engine_var.set(preset.get("engine", "vanilla" if IS_WIN else "auto"))
                self.port_var.set(preset.get("port", "8080"))
                self.host_var.set(preset.get("host", "127.0.0.1"))
                self.clear_mode_var.set(preset.get("clear_mode", False))
                self.dm_var.set(preset.get("dynamic_moe", False))
            finally:
                self._restoring = False
            self._refresh_recommend()
            self._log(f"已加载预设: {name}")
        except Exception as e:
            self._log(f"[错误] 加载预设失败: {e}")

    def _save_preset(self):
        name = simpledialog.askstring("保存预设", "预设名称:", parent=self.root)
        if not name:
            return
        try:
            with open(PRESET_FILE, encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            data = {}
        data[name] = {"params": {k: self.pv[k].get() for k in self.pv},
                      "override": self.ovr.get("1.0", "end").strip(),
                      "engine": self.engine_var.get(),
                      "port": self.port_var.get(),
                      "host": self.host_var.get(),
                      "clear_mode": self.clear_mode_var.get(),
                      "dynamic_moe": self.dm_var.get()}
        with open(PRESET_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        self._refresh_preset_names()
        self.preset_var.set(name)
        self._log(f"已保存预设: {name}")

    def _export(self):
        if not self.selected:
            return
        model_name = ((self.selected or {}).get("name") or "llama-shell").replace(".gguf", "")
        default = f"{model_name}_llama-shell.json"
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json",
                                            initialfile=default, filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = {"params": {k: self.pv[k].get() for k in self.pv},
                "override": self.ovr.get("1.0", "end").strip(),
                "engine": self.engine_var.get(),
                "port": self.port_var.get(),
                "host": self.host_var.get(),
                "clear_mode": self.clear_mode_var.get(),
                "dynamic_moe": self.dm_var.get()}
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        self._log(f"已导出: {path}")

    def _import(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            self._restoring = True
            try:
                for k, v in (data.get("params") or {}).items():
                    if k in self.pv:
                        self.pv[k].set(v)
                self.ovr.delete("1.0", "end")
                self.ovr.insert("1.0", data.get("override", ""))
                self.engine_var.set(data.get("engine", "vanilla" if IS_WIN else "auto"))
                self.port_var.set(data.get("port", "8080"))
                self.host_var.set(data.get("host", "127.0.0.1"))
                self.clear_mode_var.set(data.get("clear_mode", False))
                self.dm_var.set(data.get("dynamic_moe", False))
            finally:
                self._restoring = False
            self._refresh_recommend()
            self._log(f"已导入: {path}")
        except Exception as e:
            self._log(f"[错误] 导入失败: {e}")

    def _refresh_dynamic_combos(self):
        for key, widget in self.widgets.items():
            if key == "spec_draft_model":
                widget["values"] = MTP_CANDS
            elif key == "mmproj":
                widget["values"] = MMPROJ_DISPLAY

    def _on_close(self):
        self._stop()
        self._reader_stop = True
        self._health_stop = True
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
