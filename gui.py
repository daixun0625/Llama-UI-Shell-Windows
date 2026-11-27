#!/usr/bin/env python3
"""llama-shell GUI — 轻量原生 Tkinter。

功能：
  * 模型库浏览（/Volumes/CT/.../models 下 .gguf，后台解析，不卡 UI）
  * 选中模型 → 自动推荐推理参数（移植 ToshLLM 启发式，recommend.py）
  * 三引擎：vanilla（自动探测 bin/llama-b* 最高编号）/ ToshLLM（Metal GPU）/ ik（ik_llama.cpp 纯 CPU，本机 35B MoE 最快；初始默认）
  * 生成后端命令、一键运行、实时日志、/health 就绪指示、测速
  * llama.cpp 联网升级：查 GitHub 最新/稳定 → 下载到 bin/llama-bNNNN/ → vanilla_bin 指向新版
  * 停止

零第三方 GUI 依赖（仅标准库 tkinter + 本目录 recommend/gguf_fast）。
运行：python3 gui.py
"""
import os, re, sys, glob, time, queue, threading, subprocess, signal, json, ctypes, urllib.request, psutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

# PyInstaller frozen：BASE 指向 exe 所在目录（配置/状态/预设都随 exe 目录走，符合主人 dist\llama-shell\ 的用法）
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
# PyInstaller 6.x onedir：数据文件在 _internal/（5.x 时代在根目录）——两种布局都找
_INTERNAL = os.path.join(BASE, "_internal")
if os.path.isdir(_INTERNAL):
    sys.path.insert(0, _INTERNAL)
import gguf_fast, recommend  # noqa: E402

IS_WIN = sys.platform == "win32"
try:
    from treelist import TreeList        # Windows 专用：Treeview 行距 bug 的 Canvas 替身
except ImportError:
    TreeList = None
MONO = "Consolas" if IS_WIN else "Menlo"   # 等宽字体按平台（Windows 无 Menlo）

# 平台默认：Windows 双引擎基座（vanilla=主人现役 Vulkan 构建 + ik=本地 AVX2 构建；tosh 不可用）；
# macOS 三引擎基座（vanilla + tosh(Metal) + ik(纯 CPU AVX2)）
if IS_WIN:
    MODELS_DIR_DEFAULT = r"D:\models"
    TOSH_BIN_DEFAULT = ""                       # tosh（Metal）仅 macOS
    IK_BIN_DEFAULT = r"D:\ik-win\bin\llama-server.exe"   # ik_llama.cpp Windows 构建（MSVC CPU AVX2）
else:
    MODELS_DIR_DEFAULT = "/Volumes/CT/models"
    TOSH_BIN_DEFAULT = "/Applications/ToshLLM.app/Contents/Resources/bin/llama-server"
    IK_BIN_DEFAULT = "/Volumes/CT/ik/bin/llama-server"   # 第三引擎 ik_llama.cpp（纯 CPU AVX2）
# 系统默认 vanilla llama-server 探测链（11-23 起 ② 优先 D:\llama-vulkan-x64-bNNNN 最高版本目录）：
# ① 系统 PATH（which llama-server，conda/brew/源码安装都受益）
# ② Windows：主人现役生产 Vulkan 构建——版本化目录 D:\llama-vulkan-x64-bNNNN 中编号最高者
#    （无编号后缀的 D:\llama-vulkan-x64 仅在无版本化目录时兜底）
# ③ 本地兜底 bin/llama-b*/llama-server 中编号最高者
VANILLA_BIN_FALLBACK = recommend.latest_vanilla_bin()
def _find_default_llama_server() -> str:
    import shutil
    p = shutil.which("llama-server")
    if p:
        return p
    if IS_WIN:
        # 最高编号版本化目录 D:\llama-vulkan-x64-bNNNN（内联扫描：本函数模块级立即执行，
        # 不能前向引用后面定义的 win_vulkan_builds；版本化目录是 D:\ 顶层兄弟目录）
        ddrive = "D:\\"
        best = None
        if os.path.isdir(ddrive):
            for d in os.listdir(ddrive):
                m = re.match(r"^llama-vulkan-x64-b(\d+)$", d)
                if m and os.path.isfile(os.path.join(ddrive, d, "llama-server.exe")):
                    n = int(m.group(1))
                    if best is None or n > best[0]:
                        best = (n, os.path.join(ddrive, d, "llama-server.exe"))
        if best:
            return best[1]
        prod = r"D:\llama-vulkan-x64\llama-server.exe"
        if os.path.exists(prod):
            return prod
    return VANILLA_BIN_FALLBACK
VANILLA_BIN_DEFAULT = _find_default_llama_server()
SKIP = ("mmproj", "clip", "whisper", "mtp", "lora")   # mtp=独立 MTP draft / lora=LoRA 适配器，均不进主模型库

# 配置文件（模型库路径、引擎路径等）
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
# 0.1→0.2 迁移：旧配置 custom_srv（自定义服务路径）并入 vanilla_bin；
# vanilla_bin 指向已删除的旧版（如 1.0.0 路径）而 custom_srv 可用时 → 以 custom_srv 为准（主人现役构建）
if _cfg.get("custom_srv"):
    vb = (_cfg.get("vanilla_bin") or "").strip()
    if (not vb or not os.path.exists(os.path.abspath(vb))) \
            and os.path.exists(os.path.abspath(_cfg["custom_srv"])):
        _cfg["vanilla_bin"] = _cfg["custom_srv"]
        _cfg.pop("custom_srv", None)
        _save_config(_cfg)
MODELS_DIR = _cfg.get("models_dir", MODELS_DIR_DEFAULT)
VANILLA_ABS = _cfg.get("vanilla_bin", os.path.abspath(VANILLA_BIN_DEFAULT))
TOSH_ABS = _cfg.get("tosh_bin", TOSH_BIN_DEFAULT)
IK_ABS = _cfg.get("ik_bin", IK_BIN_DEFAULT)   # 第三引擎 ik_llama.cpp（Windows=本地构建 D:\ik-win）
VRAM_OVERRIDE = _cfg.get("vram_override_gb")   # 用户显式声明的独显显存（GiB），覆盖探测值（最高优先级）
GPU_CACHE = _cfg.get("gpu_cache")             # GPU 探测缓存（30 天快路径，跳过 10-20s 的 dxdiag/WMI）
STATE_FILE = os.path.join(BASE, "gui_state.json")   # 按模型持久化参数面板/覆盖/引擎
PRESET_FILE = os.path.join(BASE, "gui_presets.json")  # 命名预设（可跨模型复用）

# 允许推荐计划写入"下拉表之外的具体数字"的项 → 用 Combobox（可下拉 + 手动输入）
FREE_EDIT_KEYS = {"ngl", "threads", "spec_draft_model", "spec_draft_n_max", "spec_type",
                  "mmproj", "mmproj_device", "lora",
                  "moe_cache_mib", "cache_ram"}   # moe_cache_mib/cache_ram 允许输入候选表之外的具体数字

def _build_mtp_cands(models_dir: str) -> tuple:
    """返回 (MTP_CANDS, MTP_DISPLAY, MTP_MAP)。
    MTP 独立 draft 文件自动识别（同视觉塔逻辑）：文件名含 'mtp'（大小写不敏感）即命中，
    覆盖 mtpdraft-* / *-MTP* / *-mtp.gguf 等命名；SKIP 中的附属文件除外。
    显示名截断 30 字符，MTP_MAP 显示名→全路径；default/off → 空串（省略 --spec-draft-model）。
    目录不存在时返回空候选，不抛异常。"""
    empty_map = {"default": "", "off": ""}
    if not os.path.isdir(models_dir):
        return [], ["off"], empty_map
    cands = sorted(os.path.join(models_dir, b) for b in os.listdir(models_dir)
                   if b.lower().endswith(".gguf") and not b.startswith(".")
                   and "mtp" in b.lower()
                   and not any(s in b.lower() for s in SKIP if s != "mtp"))
    m_map = {os.path.basename(p)[:30]: p for p in cands}
    m_map["default"] = ""
    m_map["off"] = ""
    return cands, ["off"] + [os.path.basename(p)[:30] for p in cands], m_map

def _build_mmproj_cands(models_dir: str) -> tuple:
    """返回 (MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP)"""
    cands = sorted(glob.glob(os.path.join(models_dir, "mmproj*.gguf")))
    display = ["off"] + [os.path.basename(p)[:30] for p in cands]
    m_map = {os.path.basename(p)[:30]: p for p in cands}
    m_map["off"] = ""
    m_map["auto"] = ""
    return cands, display, m_map

# 模块级初始候选（基于配置目录），GUI 切换目录后动态更新
MTP_CANDS, MTP_DISPLAY, MTP_MAP = _build_mtp_cands(MODELS_DIR)
def _build_lora_cands(models_dir: str) -> tuple:
    """LoRA 适配器文件自动识别（同 MTP）：文件名含 'lora'（大小写不敏感，排除 mmproj）。
    返回 (LORA_CANDS, LORA_DISPLAY, LORA_MAP)；default/off → 空串（省略 --lora）。"""
    empty_map = {"default": "", "off": ""}
    if not os.path.isdir(models_dir):
        return [], ["off"], empty_map
    cands = sorted(os.path.join(models_dir, b) for b in os.listdir(models_dir)
                   if b.lower().endswith(".gguf") and not b.startswith(".")
                   and "lora" in b.lower() and "mmproj" not in b.lower())
    l_map = {os.path.basename(p)[:30]: p for p in cands}
    l_map["default"] = ""
    l_map["off"] = ""
    return cands, ["off"] + [os.path.basename(p)[:30] for p in cands], l_map

MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP = _build_mmproj_cands(MODELS_DIR)
LORA_CANDS, LORA_DISPLAY, LORA_MAP = _build_lora_cands(MODELS_DIR)

def binary_for(engine: str) -> str:
    if engine == "ik":
        return IK_ABS
    return TOSH_ABS if engine == "tosh" else VANILLA_ABS


def health(port: int, host: str = "127.0.0.1") -> bool:
    try:
        return urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1).status == 200
    except Exception:
        return False


def _shell_str(tokens) -> str:
    """token 列表 → 显示串（双引号规则，供显示/复制/粘贴到 cmd 或自定义覆盖框）。
    双轨制：显示串套引号；_spawn 解析回 token 用 posix shlex（去外层引号、保留引号内反斜杠），
    Popen 拿到的 token 不带引号（Windows 原生调用不解析引号，带引号=文件名含引号字符→打不开）。
    含反斜杠（Windows 路径）/空格/中文的 token 必须套双引号：posix shlex 引号内反斜杠是字面量，
    引号外反斜杠是转义符（会吃掉 反斜杠+l→l），故反斜杠路径必须引号保护。"""
    out = []
    for t in tokens:
        t = str(t)
        is_gguf = t.lower().endswith(".gguf")
        needs = (not t or is_gguf or (" " in t) or ('"' in t) or (chr(92) in t)
                 or any(ord(c) > 127 for c in t))
        if needs:
            out.append('"' + t.replace('"', '\\"') + '"')
        else:
            out.append(t)
    return " ".join(out)


def kill_port(port: int) -> list:
    """清理占用指定端口的所有进程，返回被杀的 PID 列表。
    - Windows：netstat -ano 找监听 PID + taskkill /F /PID（psutil 兜底）
    - POSIX：lsof -ti :port → kill -9（11-13 修复：lsof 必须带冒号，
      \":8080\"=按端口查；\"8080\"=按 PID 查→永远空——旧写法让兜底没真正杀过孤儿，
      是 35B 停不掉/内存残留的深根因）"""
    import subprocess
    if sys.platform == "win32":
        killed = []
        # 方法1: netstat -ano 找监听 PID
        try:
            # encoding/errors 必须显式：中文 Windows 的 netstat 表头是 GBK 本地化字节，
            # 测试/主机进程若为 utf-8 locale，默认严格解码会让 Popen 内部读线程崩
            # （UnicodeDecodeError 0xbb）→ 本函数抛异常 → 端口清理整体失效
            out = subprocess.check_output(["netstat", "-ano"], text=True, timeout=5,
                                          stderr=subprocess.DEVNULL,
                                          encoding="utf-8", errors="replace")
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and (f":{port} " in (parts[1] + " ")) and parts[3] == "LISTENING":
                    try:
                        pids.add(int(parts[4]))
                    except ValueError:
                        pass
            for pid in pids:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   timeout=3, capture_output=True,
                                   creationflags=0x08000000)
                    killed.append(pid)
                except Exception:
                    pass
        except Exception:
            pass
        # 方法2: psutil 兜底（netstat 拿不全/权限不足时）
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
        out = subprocess.check_output(["lsof", "-ti", f":{port}"],
                                      text=True, timeout=3, stderr=subprocess.DEVNULL)
        pids = [int(x) for x in out.split() if x.strip()]
        if pids:
            for pid in pids:
                try:
                    subprocess.run(["kill", "-9", str(pid)],
                                   timeout=2, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            return pids
    except Exception:
        pass
    return []


def bench_once(port: int, host: str = "127.0.0.1", n: int = 48) -> float:
    """对运行中的 server 生成 n token，返回 t/s。"""
    body = {"prompt": "Explain in a few sentences how a CPU pipeline works.",
            "max_tokens": n, "stream": False, "temperature": 0}
    req = urllib.request.Request(f"http://{host}:{port}/completion",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    return d.get("n_tokens_predicted", n) / (time.time() - t0)


def scan_models(models_dir: str = None) -> list:
    """后台线程用：解析所有主模型头，返回 st 列表。"""
    if models_dir is None:
        models_dir = MODELS_DIR
    out = []
    for p in sorted(glob.glob(os.path.join(models_dir, "*.gguf"))):
        b = os.path.basename(p)
        if any(s in b.lower() for s in SKIP):
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


# ---------------- llama.cpp 联网升级（11-19） ----------------
# 2026-11 起 llama.cpp 发版结构（本机实测 b12541）：
#   * 夜标签 bXXXX 携带各平台预编译件：llama-bXXXX-bin-macos-{x64,arm64}.tar.gz
#     / llama-bXXXX-bin-win-cpu-{x64,arm64}.zip（tar 内含 llama-bXXXX/ 顶层目录，
#     dylib 版本链为相对符号链接 → 解包必须保留 symlink，py3.12 用 filter="data"）
#   * semver 发布（v1.0.0）无二进制，assets 里的 nightly-tag.txt 指向「当前稳定」构建号
# 升级流：查 GitHub → 下到 bin/llama-bXXXX/ → 校验 --version → 配置 vanilla_bin 指向新版
#         → 轮次删除旧版（只留最近 1 个旧版，正在运行的版本不删）。
GH_API = "https://api.github.com/repos/ggml-org/llama.cpp"


def _plat_arch() -> tuple:
    """返回 (os_name, arch)：darwin→x64/arm64，win32→x64/arm64，其他平台 (None, None)
    （本工具暂只服务 macOS/Windows 双端）。"""
    import platform as _pf
    if sys.platform == "darwin":
        return "darwin", ("x64" if _pf.machine() in ("x86_64", "amd64") else "arm64")
    if sys.platform == "win32":
        return "win32", ("x64" if _pf.machine() in ("AMD64", "x86_64") else "arm64")
    return None, None


def _gh_get(url: str, timeout: int = 60, raw: bool = False):
    """GitHub API / 下载资源读取（urllib 零依赖；必须带 UA，否则 API 403）。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "llama-shell/upgrade",
                     "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data)


def platform_asset(release: dict, os_name: str, arch: str):
    """在单个 release（含 assets 列表）里找本平台预编译件。
    11-27 修复：win32 优先 **win-vulkan** 资产（主人规矩：vanilla 行 = Vulkan 构建
    D:\\llama-vulkan-x64-bNNNN）；无 vulkan 包才回落 win-cpu（老 release 无 vulkan
    资产）。旧代码一律取 win-cpu → GUI 升级把引擎静默降级成 CPU（"能跑但慢"，不报错）。
    返回 {"num","url","asset","variant"}；没有匹配资产返回 None。"""
    tag = str(release.get("tag_name", ""))
    m = re.search(r"b(\d+)", tag)
    if not m:
        return None
    b = m.group(1)
    if os_name == "darwin":
        want = [f"llama-b{b}-bin-macos-{arch}.tar.gz"]
    elif os_name == "win32":
        want = [f"llama-b{b}-bin-win-vulkan-{arch}.zip",
                f"llama-b{b}-bin-win-cpu-{arch}.zip"]   # 回落：老 release 无 vulkan 包
    else:
        return None
    have = {a.get("name"): a for a in release.get("assets", [])}
    for w in want:
        if w in have:
            return {"num": int(b), "url": have[w]["browser_download_url"], "asset": w,
                    "variant": w.split("bin-")[-1].replace(".zip", "").replace(".tar.gz", "")}
    return None


_LLAMA_SRV = "llama-server.exe" if os.name == "nt" else "llama-server"   # 平台引擎文件名

# 11-23 主人定规：Windows 升级包版本化目录根（D:\llama-vulkan-x64-bNNNN\llama-server.exe）；
# 「检查并升级」下载/解包到新版目录（不再用 BASE/bin/.dl），保留最近 2 个版本目录、滚动删除。
WIN_VULKAN_ROOT = r"D:\llama-vulkan-x64"


def win_vulkan_builds() -> list:
    """Windows 版本化目录清单（D:\\llama-vulkan-x64-bNNNN\\llama-server.exe 存在的才计），
    (build_num, 目录) 降序。注意版本化目录是 D:\\ 顶层兄弟目录，不在 D:\\llama-vulkan-x64 之内。"""
    out = []
    ddrive = os.path.dirname(WIN_VULKAN_ROOT)   # D:\
    if os.path.isdir(ddrive):
        for d in os.listdir(ddrive):
            m = re.match(r"^llama-vulkan-x64-b(\d+)$", d)
            if m and os.path.isfile(os.path.join(ddrive, d, _LLAMA_SRV)):
                out.append((int(m.group(1)), os.path.join(ddrive, d)))
    return sorted(out, reverse=True)


def rotate_win_vulkan_builds(keep_num: int, running_bin: str = None) -> list:
    """Windows 版本化目录轮次删除（11-23 主人定规）：保留 新版 + 最近 1 个旧版（共 2 个），
    其余 D:\\llama-vulkan-x64-bNNNN 删掉；正在运行引擎所在目录不删。返回实际删除目录列表。
    注意：无 b 编号后缀的旧生产目录 D:\\llama-vulkan-x64 不在轮次范围（主人令才动）。"""
    import shutil as _sh
    dropped = []
    olds = [p for n, p in win_vulkan_builds() if n != keep_num]   # 降序
    for p in olds[1:]:                                            # 保留最近 1 个旧版，其余删
        if running_bin and os.path.abspath(running_bin).startswith(p + os.sep):
            continue
        _sh.rmtree(p, ignore_errors=True)
        dropped.append(p)
    return dropped


def local_llama_builds(bindir: str = None) -> list:
    """本地 bin/llama-bNNNN 目录清单（含 llama-server(.exe) 的才计），(build_num, 目录) 降序。"""
    bindir = bindir or os.path.join(BASE, "bin")
    srv_names = ("llama-server", "llama-server.exe") if os.name == "nt" else ("llama-server",)
    out = []
    if os.path.isdir(bindir):
        for d in os.listdir(bindir):
            m = re.match(r"^llama-b(\d+)$", d)
            if m and any(os.path.isfile(os.path.join(bindir, d, x)) for x in srv_names):
                out.append((int(m.group(1)), os.path.join(bindir, d)))
    return sorted(out, reverse=True)


def current_vanilla_num(cfg: dict = None) -> tuple:
    """当前 vanilla 引擎的 build 号：优先解析配置 vanilla_bin 路径里的 b(\\d+)，
    其次本地 bin/ 最高号；都没有 → (None, "")。返回 (num, 来源路径)。"""
    if cfg is None:
        cfg = _load_config()
    p = (cfg.get("vanilla_bin") or "").strip()
    if p:
        m = re.search(r"b(\d+)", p)
        if m:
            return int(m.group(1)), p
    builds = local_llama_builds()
    if builds:
        return builds[0][0], os.path.join(builds[0][1], _LLAMA_SRV)
    return None, ""


def upgrade_check(progress=None) -> dict:
    """联网查 GitHub：最近有本平台预编译件的 release（=最新）+ 稳定指针 + 本地现值。
    稳定指针两级：① releases/latest 自身带本平台二进制（semver 发布直出二进制的形态）；
    ② 其 nightly-tag.txt 指向的稳定构建号（v1.0.0 时代形态，11-19 实测 b12359）。
    progress(msg)：可选进度行回调（11-27：本机 GitHub API 单 list ~35s，无进度行=体感卡死；
    检查 worker 传 self.q.put → 落 GUI 日志）。
    返回 {cur_num, cur_src, latest_num, latest_url, latest_variant,
          stable_num, stable_url, stable_variant}；查不到抛 RuntimeError。"""
    def _say(msg):
        if progress:
            progress(msg)
    os_name, arch = _plat_arch()
    if os_name is None:
        raise RuntimeError(f"平台 {sys.platform} 无预编译件选择逻辑（仅支持 macOS/Windows）")
    _say("正在查询 GitHub release 列表…（本机 API 慢时约 30–60s）")
    t0 = time.time()
    rels = _gh_get(f"{GH_API}/releases?per_page=50")
    _say(f"release 列表已取（{time.time() - t0:.0f}s，{len(rels)} 条），定位最新版本…")
    latest = None
    for r in rels:
        pa = platform_asset(r, os_name, arch)
        if pa:
            latest = pa
            break
    if not latest:
        raise RuntimeError("GitHub 最近 50 个 release 均无本平台预编译件，无法自动升级")
    latest_num, latest_url = latest["num"], latest["url"]
    # 稳定指针（可选，失败不影响「最新」升级）
    stable_num, stable_url, stable_variant = None, None, None
    try:
        _say("正在查稳定指针（nightly-tag）…")
        lat = _gh_get(f"{GH_API}/releases/latest")
        pa = platform_asset(lat, os_name, arch)
        if pa:
            stable_num, stable_url, stable_variant = pa["num"], pa["url"], pa["variant"]
        else:
            for a in lat.get("assets", []):
                if a.get("name") == "nightly-tag.txt":
                    txt = _gh_get(a["browser_download_url"], raw=True).decode("utf-8", "ignore")
                    m = re.search(r"b(\d+)", txt)
                    if m:
                        stable_num = int(m.group(1))
                        for r in rels:
                            if r.get("tag_name") and m.group(0) in r["tag_name"]:
                                pa2 = platform_asset(r, os_name, arch)
                                if pa2:
                                    stable_url, stable_variant = pa2["url"], pa2["variant"]
                                break
                        if not stable_url:
                            # 稳定构建比 50 个夜标签更旧（超出列表窗口）→ 按 tag 定向查
                            st_rel = _gh_get(f"{GH_API}/releases/tags/{m.group(0)}")
                            pa2 = platform_asset(st_rel, os_name, arch)
                            if pa2:
                                stable_url, stable_variant = pa2["url"], pa2["variant"]
    except Exception:
        pass
    cur_num, cur_src = current_vanilla_num()
    _say("版本核对完成。")
    return {"cur_num": cur_num, "cur_src": cur_src,
            "latest_num": latest_num, "latest_url": latest_url,
            "latest_variant": latest["variant"],
            "stable_num": stable_num, "stable_url": stable_url,
            "stable_variant": stable_variant}


def _http_download(url: str, dest: str, progress=None) -> str:
    """流式下载到 dest（256KB 分块；progress(pct, got, total) 可选回调）。失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "llama-shell/upgrade"})
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, -1
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and progress:
                    pct = got * 100 // total
                    if pct >= 100 or pct >= last + 20:
                        last = pct
                        progress(pct, got, total)
    return dest


def rotate_old_builds(keep_num: int, running_bin: str = None, bindir: str = None) -> list:
    """安装 keep_num 后的轮次删除：保留 新版 + 最近 1 个旧版，其余 bin/llama-bNNNN 删掉。
    running_bin（正在运行的引擎二进制）落在待删目录内时保留该目录（不删在用版本）。
    返回实际删除的目录路径列表。"""
    import shutil as _sh
    bindir = bindir or os.path.join(BASE, "bin")
    builds = [n for n, _ in local_llama_builds(bindir)]
    olds = [n for n in builds if n != keep_num]   # 降序
    drop = [n for n in olds[1:]]                   # 最近 1 个旧版=olds[0]，其余删
    dropped = []
    for n in drop:
        p = os.path.join(bindir, f"llama-b{n}")
        if running_bin and os.path.abspath(running_bin).startswith(p + os.sep):
            continue
        _sh.rmtree(p, ignore_errors=True)
        dropped.append(p)
    return dropped


# ================= 文生图（T2I）页 模块级工具 =================
def _find_default_sdcli() -> str:
    """文生图引擎（sd-cli）探测：
    - Windows：① LocModel engine.json 声明的 build 目录（同目录 sd-cli.exe）
               ② 该目录下 glob 最新 master-* build（LocModel 未装时提示手选）
    - macOS：ToshLLM 自带 bin-image/sd-cli"""
    if IS_WIN:
        import glob as _g
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "LocModel", "engines", "sd")
        try:
            ej = os.path.join(base, "engine.json")
            if os.path.exists(ej):
                with open(ej, encoding="utf-8") as f:
                    d = json.load(f)
                sp = d.get("server_path") or ""
                cli = os.path.join(os.path.dirname(sp), "sd-cli.exe") if sp else ""
                if cli and os.path.exists(cli):
                    return cli
        except Exception:
            pass
        cands = _g.glob(os.path.join(base, "master-*", "sd-cli.exe"))
        if cands:
            return sorted(cands)[-1]
    return "/Applications/ToshLLM.app/Contents/Resources/bin-image/sd-cli"
SDCLI_DEFAULT = _find_default_sdcli()
TOSHLLM_PLIST = "/Users/dx/Library/Preferences/dev.engel.toshllm.plist"   # macOS 专属（Windows 无 → prefill 空）
IMAGE_MODELS_DIR_DEFAULT = r"D:\models\image" if IS_WIN else "/Volumes/CT/models/image"
IMAGE_OUT_DIR = os.path.join(BASE, "img_out")   # 生成图默认落盘（预览区指向这里）
IMAGE_ASPECTS = ["16:9", "1:1", "9:16", "3:2", "4:3", "4:5", "5:4"]
IMAGE_RES = ["768", "512", "1024", "1536", "2048"]
IMAGE_CFG = ["default", "1.0", "1.1", "1.5", "2.0", "3.5", "6.5", "7.0"]
IMAGE_STEPS = ["default", "4", "8", "12", "16", "24", "32", "50"]
IMAGE_SEEDS = ["default", "-1", "42", "1234", "777"]
IMAGE_SAMPLERS = ["default", "euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m"]
IMAGE_FMTS = ["png", "jpg", "webp"]
# 文生图引擎候选（图引擎=sd-cli；预留=空/其他）+ 引擎默认
IMG_ENGINE_DEFAULT = SDCLI_DEFAULT
IMG_RESERVE_DEFAULT = TOSH_BIN_DEFAULT

def _aspect_wh(aspect: str, base: int) -> tuple:
    """宽高比 × 基础尺寸(短边) → (宽, 高)；长边=base，取 8 的倍数（sd-cli 要求）。"""
    try:
        a, b = str(aspect).split(":"); a, b = float(a), float(b)
        if a <= 0 or b <= 0:
            return int(base), int(base)
    except Exception:
        return int(base), int(base)
    base = int(base)
    if a >= b:                       # 横或方：宽=base
        w, h = base, int(round(base * b / a))
    else:                            # 竖：高=base
        h, w = base, int(round(base * a / b))
    w, h = max(8, w // 8 * 8), max(8, h // 8 * 8)
    return w, h

def load_imagen_instances() -> list:
    """读 toshllm plist 的 imagenInstances（JSON，plist 里存为 bytes）；失败返回 []。"""
    import plistlib
    try:
        with open(TOSHLLM_PLIST, "rb") as f:
            d = plistlib.load(f)
        raw = d.get("imagenInstances", "")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        return json.loads(raw) if raw else []
    except Exception:
        return []

def _norm(mpath: str) -> str:
    return (mpath or "").replace("\\", "").strip()

def imagen_prefill(model_basename: str) -> dict:
    """按模型 gguf basename 匹配 plist 实例 → 返回 {cfg,aspect,res,steps,seed,format,prompt,neg}。"""
    for it in load_imagen_instances():
        if os.path.basename(_norm(it.get("customModelPath"))) == model_basename:
            return {
                "cfg": it.get("customCfg"), "aspect": it.get("aspect"),
                "res": it.get("baseSize"), "steps": it.get("steps"),
                "seed": it.get("seed"), "format": it.get("format"),
                "prompt": it.get("prompt"), "neg": it.get("negativePrompt"),
            }
    return {}

def _pick_encoder(bundle_dir: str, common_dir: str) -> str:
    """从 common/ 里挑文本编码器：优先文件名含 bundle 主名 token（逐级缩短族前缀，
    11-23 主人令：minimax-h3-image 族 → qwen3vl_32b_minimax_h3（32B 编码器）），
    其次 Qwen3-4B，再次最大。"""
    if not os.path.isdir(common_dir):
        return ""
    encs = [f for f in os.listdir(common_dir)
            if f.endswith(".gguf") and not f.lower().startswith(("clip", "t5"))]
    if not encs:
        return ""
    token = bundle_dir.lower().replace("-", "_")
    parts = token.split("_")
    # 逐级缩短族前缀匹配（minimax_h3_image → minimax_h3 → minimax；短于 3 字符的跳过防误配）
    for i in range(len(parts), 0, -1):
        pre = "_".join(parts[:i])
        if len(pre) < 3:
            continue
        for e in encs:
            if pre in e.lower().replace("-", "_"):
                return os.path.join(common_dir, e)
    for e in encs:
        if ("Qwen3-4B" in e) or ("Qwen3VL-4B" in e):
            return os.path.join(common_dir, e)
    encs.sort(key=lambda f: -os.path.getsize(os.path.join(common_dir, f)))
    return os.path.join(common_dir, encs[0])

def scan_image_models(image_dir: str) -> list:
    """列出 image 目录下的可用文生图模型（子目录含非 .part 的扩散 .gguf）。
    每条目 {name, dir, model, model_variants, encoder, encoders, vae, vae_cands, loras,
            edit_capable, prefill}。编码器/VAE 优先取模型目录内同名文件（自包含布局），
    回退 common/ 池（旧布局兼容）。edit_capable：MiniMax-H3-Image 具备图改图。"""
    out = []
    if not os.path.isdir(image_dir):
        return out
    common = os.path.join(image_dir, "common")
    loras = os.path.join(image_dir, "loras")
    _ENC_PREFIX = ("qwen", "clip", "t5")    # 编码器/视觉前缀（非扩散）
    for d in sorted(os.listdir(image_dir)):
        if d in ("common", "loras", "imagen", "hub", "xet") or d.startswith("."):
            continue
        mdir = os.path.join(image_dir, d)
        if not os.path.isdir(mdir):
            continue
        allf = os.listdir(mdir)
        # 扩散 gguf：排除编码器前缀 + .part；多量化选一个（优先 Q4，其次体积最小=快）
        ggufs = [f for f in allf
                 if f.endswith(".gguf") and not f.endswith(".part")
                 and not f.startswith("._")
                 and not f.lower().startswith(_ENC_PREFIX)]
        if not ggufs:
            continue
        ggufs.sort(key=lambda f: (0 if "Q4" in f else 1, os.path.getsize(os.path.join(mdir, f))))
        model_base = ggufs[0]
        mb = os.path.splitext(model_base)[0]
        mb = re.sub(r"-Q[0-9A-Za-z_]+$", "", mb)          # 去量化后缀 → 显示名
        disp = mb.replace("_pruned", "").replace("_", " ")
        # 编码器：本目录内（自包含）优先，回退 common/ 池
        enc_local = [os.path.join(mdir, f) for f in sorted(allf)
                     if f.lower().startswith(_ENC_PREFIX) and f.endswith(".gguf")
                     and not f.endswith(".part") and not f.startswith("._")]
        if enc_local:
            encs, encoder = enc_local, enc_local[0]
        else:
            encs = ([os.path.join(common, f) for f in sorted(os.listdir(common))
                     if f.endswith(".gguf") and not f.lower().startswith(("clip", "t5"))]
                    if os.path.isdir(common) else [])
            encoder = _pick_encoder(d, common)
        # VAE：本目录内 .safetensors 优先（MiniMax 官方大 VAE / z-image ae.safetensors），回退 common/
        vae_local = [os.path.join(mdir, f) for f in sorted(allf)
                     if f.endswith(".safetensors") and not f.startswith("._")]
        if vae_local:
            vaes, vae = vae_local, vae_local[0]
        else:
            c_vae = os.path.join(common, "ae.safetensors")
            # 11-23：MiniMax-H3 族需专用 2D VAE（sd-cli 强校验，用通用 ae.safetensors 报
            # "requires its 2D image VAE"）：common/ 池里先找族前缀匹配的 VAE 文件
            fam_vae = []
            if os.path.isdir(common):
                cands = sorted(f for f in os.listdir(common)
                               if f.endswith(".safetensors") and not f.startswith("._"))
                fam_tok = d.lower().replace("-", "_").split("_")
                for i in range(len(fam_tok), 0, -1):
                    pre = "_".join(fam_tok[:i])
                    if len(pre) < 3:
                        continue
                    fam_vae = [os.path.join(common, f) for f in cands if pre in f.lower()]
                    if fam_vae:
                        break
            base = [c_vae] if os.path.isfile(c_vae) else []
            rest = ([os.path.join(common, f) for f in sorted(os.listdir(common))
                     if f.endswith(".safetensors")] if os.path.isdir(common) else [])
            vaes = fam_vae + [x for x in base + rest if x not in fam_vae]
            vae = vaes[0] if vaes else ""
        entry = {
            "name": disp,
            "dir": mdir,
            "model": os.path.join(mdir, model_base),
            "model_variants": [os.path.join(mdir, g) for g in ggufs],
            "encoder": encoder,
            "encoders": encs,
            "vae": vae,
            "vae_cands": vaes,
            "loras": loras if os.path.isdir(loras) else "",
            "edit_capable": "minimax" in d.lower(),   # MiniMax-H3-Image 支持图改图（-r 参考图）
        }
        entry["prefill"] = imagen_prefill(model_base)
        out.append(entry)
    return out

def list_loras(lora_dir: str) -> list:
    """列 lora 目录下可用的 LoRA（.safetensors/.gguf，非 .part），返回文件名主名（按名序）。"""
    if not lora_dir or not os.path.isdir(lora_dir):
        return []
    out = []
    for f in sorted(os.listdir(lora_dir)):
        if f.endswith((".safetensors", ".gguf")) and not f.endswith(".part"):
            out.append(os.path.splitext(f)[0])
    return out

def build_image_command(model: dict, p: dict, out_dir: str, img_engine: str,
                        offload: bool = False):
    """组 sd-cli 文生图命令。model=scan_image_models 条目；p=面板 T2I 参数 dict。
    返回 (cmd_list, out_file_pattern)。"""
    res = p.get("res", "1024")
    try:
        base = int(res)
    except (ValueError, TypeError):
        base = 1024
    w, h = _aspect_wh(p.get("aspect", "1:1"), base)
    fmt = (str(p.get("format", "png") or "png").strip() or "png")
    steps = str(p.get("steps") or "").strip()
    cfg = str(p.get("cfg") or "").strip()
    seed = str(p.get("seed") or "").strip()
    prompt = str(p.get("prompt") or "").strip()
    neg = str(p.get("neg") or "").strip()
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%H%M%S")
    out = os.path.join(out_dir, f"toshi_{ts}_%d.{fmt}")
    cmd = [img_engine,
           "--diffusion-model", model.get("model", ""),
           "--llm", model.get("encoder", ""),
           "--vae", model.get("vae", ""),
           "--diffusion-fa", "--vae-tiling", "--params-backend", "te=cpu",
           "-W", str(w), "-H", str(h)]
    if model.get("edit_capable"):
        # 11-23：MiniMax-H3 族 32B 编码器(18G)+扩散(11.4G) 下 Vulkan VAE 解码
        # "segment 1/25 weight preparation failed" → VAE 切 CPU 后端（A/B 实证 rc=0）
        cmd += ["--backend", "vae=cpu"]
    # 11-23 修复：面板 "default"/空 不下发字面值（sd-cli 报 invalid parameter: "default"，
    # 如 MiniMax-H3 一选就 rc=1 提错）；省略 flag = 走 sd-cli 内置默认
    if steps not in ("", "default"):
        cmd += ["--steps", steps]
    if cfg not in ("", "default"):
        cmd += ["--cfg-scale", cfg]
    if seed not in ("", "default"):
        cmd += ["--seed", seed]
    cmd += ["-o", out]
    sam = str(p.get("sampler", "") or "").strip()
    if sam and sam != "default":
        cmd += ["--sampling-method", sam]
    if offload:
        cmd.append("--offload-to-cpu")
    if str(p.get("lora_dir") or "").strip():
        cmd += ["--lora-model-dir", str(p["lora_dir"]).strip()]
    # 图改图（仅 edit_capable 模型，MiniMax-H3-Image 走 Ref2VA）：参考图 -r + 改图强度（非 default 才传）。
    # 注：本 build 的 --sigmas 只接受逗号分隔值、无 "minimax-h3-image" 命名曲线 → 不传 --sigmas，走默认调度。
    ref = str(p.get("ref") or "").strip()
    strength = str(p.get("strength") or "default").strip()
    if ref and p.get("edit_capable"):
        cmd += ["-r", ref]
        if strength not in ("", "default"):
            cmd += ["--strength", strength]
    if prompt:
        cmd += ["-p", prompt]
    if neg:
        cmd += ["-n", neg]
    elif model.get("edit_capable"):
        # 11-23：MiniMax-H3 族（32B Qwen3-VL 编码器）空负向提示词会报
        # "failed to encode negative prompt" → 面板 neg 留空时注入默认负向词
        cmd += ["-n", "blurry, low quality, watermark, text, logo"]
    return cmd, out


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[str]" = queue.Queue()
        self.proc = None
        self._reader_stop = False
        self._health_stop = False
        self._dynamic_moe_used = False
        self._auto_retried = False
        self._last_cmd = ""
        self._upgrading = False     # llama.cpp 联网升级进行中（查/下载/安装）
        self._running_bin = None    # 正在运行引擎的二进制路径（轮次删除旧版时保护在用版本）
        self._user_stopped = False  # 本次进程是否用户主动停止（停止后的 rc!=0 不算崩溃）
        # Windows 高 DPI：固定像素布局常量按窗口 DPI 缩放（175% 屏 ×1.75），
        # 否则树列/左栏按 96dpi 像素定死 → 高分缩放下文字截断重叠（11-23 主人反馈）。
        # winfo_fpixels("1i") = 窗口所在监视器像素/英寸（PMDv2 精确到屏；纯 Tk 实现，
        # 不依赖 user32/shcore 导出——本机新 build 已裁掉 GetDeviceCaps 等旧导出）。
        self._dsc = 1.0
        if IS_WIN:
            try:
                f = root.winfo_fpixels("1i")
                if f > 100:
                    self._dsc = round(f / 96.0, 2)
            except Exception:
                pass
        # 硬件探测：用户显式 VRAM 覆盖 + 30 天 GPU 缓存（Windows 快路径跳过 10-20s 的 dxdiag/WMI）
        self.hw = recommend.detect_hardware(
            vram_override_gb=VRAM_OVERRIDE, gpu_cache=GPU_CACHE)
        try:
            if getattr(self.hw, "gpu_probe_src", None):
                _save_config(_load_config() | {
                    "gpu_cache": {"ts": time.time(), "src": self.hw.gpu_probe_src,
                                  "gpus": self.hw.gpus}})
        except Exception:
            pass
        self.st_cache = {}      # path -> st
        self.selected = None    # 当前 st
        self._restoring = False # 恢复模型参数期间抑制保存
        self._mem_total = None  # sysctl hw.memsize 缓存（硬件负载面板用）
        self.models_dir = MODELS_DIR  # 当前模型库路径（可配置）
        self._text_models_dir = MODELS_DIR          # 文字页模型库目录（切页独立记忆，防 image 页残留污染）
        self._image_models_dir = IMAGE_MODELS_DIR_DEFAULT  # 图片页模型库目录
        self._closing = False         # 关闭窗口时置 True，阻止 after 回调再入主循环
        self._poll_id = None         # 80ms 日志轮询定时器句柄
        self._hw_tick_id = None      # 2s 硬件负载定时器句柄
        self.widgets = {}       # key -> 控件（Combobox/OptionMenu，用于动态更新候选值）
        self.param_defaults = {}  # key -> 全局默认值
        # ---- 文生图（T2I）页状态 ----
        self.page = "text"       # "text"（llama 文字页）| "image"（sd-cli 文生图页）
        self.ipv = {}            # T2I 参数 StringVar（模型/vae/编码器/宽高比/分辨率/cfg/步数/种子/格式）
        self.iwidgets = {}       # T2I 参数控件（与文字页 self.widgets 隔离，避免 37-param 门误伤）
        self.itext = {}          # T2I 提示词/负面提示词 Text 控件
        self._img_model = None   # 当前选中的文生图模型条目（scan_image_models 元素）
        self._rowcfg = {}        # frame -> pack kwargs（切页时按序重 pack）
        self._active_img = None  # 最近一次生成的图片路径（预览区显示）
        self.img_out_dir = IMAGE_OUT_DIR
        self._build()
        self._refresh_hw_bar()
        self._refresh_engbar()   # 启动时把配置里的引擎路径回显到 engbar（含「自动探测」提示）
        self._scan_async()
        self._poll_id    = self.root.after(80,   self._poll)
        self._hw_tick_id = self.root.after(2000, self._hw_tick)
        # 11-13 自愈：GUI 曾被强杀/停止失败时，35B 服务成孤儿（ppid=1）占端口+内存，
        # 导致下次启动 bind 8080 失败→"加载失败"。启动即清场；退出（含异常）atexit 兜底。
        self._sweep_orphans()
        import atexit
        atexit.register(self._atexit_stop)
        # Windows：GPU 采样放后台线程（powershell Get-Counter 耗时 1.5-3s，放主线程会每 2s 冻住 UI）
        self._gpu_last = None
        if IS_WIN and self.hw.vram_gb > 0.2:
            threading.Thread(target=self._gpu_sampler, daemon=True).start()

    def _sweep_orphans(self):
        """启动自愈：清掉上次会话遗留、正占用当前端口的孤儿进程（13.5GB 级 35B 不再残留）。"""
        try:
            port = int(self.port_var.get() or 8080)
        except Exception:
            port = 8080
        self._last_port = port
        pids = kill_port(port)
        if pids:
            self._log(f"启动自愈：已清理占用端口 {port} 的残留进程 PIDs {pids}（上次会话孤儿，内存不再残留）")

    def _atexit_stop(self):
        """解释器退出时（正常关窗 / 未捕获异常路径）兜底杀进程组 + 清端口。
        GUI 若被 SIGKILL 则本钩子不执行——由下次启动的 _sweep_orphans 接力清场。"""
        try:
            proc = getattr(self, "proc", None)
            if proc is not None and proc.poll() is None:
                if IS_WIN:
                    # Windows 无进程组：taskkill /T 杀整棵树（llama-server 单进程+线程）
                    try:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                       capture_output=True, timeout=5,
                                       creationflags=0x08000000)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    for _ in range(25):
                        if proc.poll() is not None:
                            break
                        time.sleep(0.2)
                    if proc.poll() is None:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
            try:
                kill_port(getattr(self, "_last_port", 8080))
            except Exception:
                pass
        except Exception:
            pass

    # ---------------- UI ----------------
    def _build(self):
        root = self.root
        root.title("llama-shell · 模型库 + 参数推荐 + 双引擎" if IS_WIN
                     else "llama-shell · 模型库 + 参数推荐 + 三引擎")
        # 初始显示框不硬编码：_build 末尾按内容需求尺寸收紧，
        # 使底边框=「后端命令」框下沿再下移 6 行小字（Menlo 11，见 _fit_frame）、右边框紧贴 ③ 参数窗右沿（见 _fit_frame）
        # 窗口关闭协议：先清理端口/状态，再销毁（防 macOS Tk 死锁）
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 顶部硬件条（raw tk.Label 在 macOS Aqua 下自定义 bg 会把 fg 变白→白底白字，
        # 故显式给深色 fg + 加深一档 bg，保证可读）
        topbar = ttk.Frame(root)
        topbar.pack(fill="x")
        self.hw_lbl = tk.Label(topbar, anchor="w", padx=10, pady=4,
                               bg="#d7e0ee", fg="#16202e",
                               font=(MONO, 12), relief="flat")
        self.hw_lbl.pack(side="left", fill="x", expand=True)
        # 运行状态指示（READY / 运行中… / 已停止）—— _poll 的 HEALTH 事件刷新它
        self.status = tk.Label(topbar, anchor="e", padx=10, pady=4,
                                bg="#d7e0ee", fg="#16202e",
                                font=(MONO, 12), relief="flat")
        self.status.config(text="就绪")
        self.status.pack(side="right", fill="x", expand=False)
        # 导航切页：文字 / 图片（11-20 新增；先 pack 图片→贴 status 左侧，再 pack 文字→在图片左侧，
        # 使从左到右为「…引擎信息 | 文字 | 图片」）
        self._nav_img_btn = ttk.Button(topbar, text="图片", width=4,
                                       command=lambda: self._switch_page("image"))
        self._nav_text_btn = ttk.Button(topbar, text="文字", width=4,
                                        command=lambda: self._switch_page("text"))
        self._nav_img_btn.pack(side="right")
        self._nav_text_btn.pack(side="right")
        topbar.pack(fill="x")

        # 引擎路径配置条（文字页：vanilla / tosh / ik 三引擎各带路径控件；图片页换 img_engbar）
        engbar = ttk.Frame(root)
        engbar.pack(fill="x", padx=8, pady=(0, 2))
        self.engbar = engbar
        self._track_row(engbar, fill="x", padx=8, pady=(0, 2))
        ttk.Label(engbar, text="vanilla:", width=8, anchor="w").pack(side="left")
        self.vanilla_var = tk.StringVar(value=VANILLA_ABS)
        ttk.Entry(engbar, textvariable=self.vanilla_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(engbar, text="…", width=3,
                   command=lambda: self._browse_engine("vanilla")).pack(side="left", padx=(0, 4))
        # llama.cpp 联网升级按钮（11-23 主人令：不单独占行，移至 vanilla ⋯ 右边；版本信息走日志）
        self.upg_btn = ttk.Button(engbar, text="检查并升级", width=10,
                                  command=self._on_upgrade_click)
        self.upg_btn.pack(side="left", padx=(0, 6))
        self.tosh_var = tk.StringVar(value=TOSH_ABS)
        if not IS_WIN:   # tosh（Metal）仅 macOS；Windows 双引擎基座隐藏此行（变量保留，供 _apply_engbar 等）
            ttk.Label(engbar, text="tosh:", width=6, anchor="w").pack(side="left")
            ttk.Entry(engbar, textvariable=self.tosh_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
            ttk.Button(engbar, text="…", width=3,
                       command=lambda: self._browse_engine("tosh")).pack(side="left", padx=(0, 6))
        ttk.Label(engbar, text="ik:", width=5, anchor="w").pack(side="left")
        self.ik_var = tk.StringVar(value=IK_ABS)
        ttk.Entry(engbar, textvariable=self.ik_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(engbar, text="…", width=3,
                   command=lambda: self._browse_engine("ik")).pack(side="left", padx=(0, 6))
        ttk.Button(engbar, text="应用", width=5,
                   command=self._apply_engbar).pack(side="left")

        # （11-23 主人令：升级行取消，「检查并升级」已移入上方 engbar 的 vanilla ⋯ 右边，
        # 版本信息改走日志 [升级] 行；升级功能本体 upgrade_check/_http_download 等保留）

        # 模型库路径配置条
        dirbar = ttk.Frame(root)
        dirbar.pack(fill="x", padx=8, pady=(0, 2))
        self.dirbar = dirbar      # 顶层引擎条（engbar/img_engbar）切页归位锚点
        ttk.Label(dirbar, text="模型库:", width=8, anchor="w").pack(side="left")
        self.dir_var = tk.StringVar(value=self.models_dir)
        dir_entry = ttk.Entry(dirbar, textvariable=self.dir_var)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(dirbar, text="浏览…", width=8,
                   command=self._browse_models_dir).pack(side="left")

        # 主区域：grid 两列，左列固定最小宽度，右列自适应
        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True, padx=6, pady=2)
        # 左列 686（11-23 主人定 v4：v3 的 760 收窄 10 字符≈74px；列表/日志框同宽），×_dsc 高 DPI 缩放
        mid.grid_columnconfigure(0, minsize=int(686 * self._dsc))
        mid.grid_columnconfigure(1, weight=1)
        mid.grid_rowconfigure(0, weight=1)   # 网格行撑满 mid 高 → 图片页参数区(fill/expand)填满、无底部留白

        # 左列：模型库 + 日志
        leftcol = ttk.Frame(mid, width=int(690 * self._dsc))
        leftcol.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        leftcol.pack_propagate(False)

        left = ttk.LabelFrame(leftcol, text=" 模型库列表 ")
        self.left = left             # 切页调模型库行数：图片页 5 行（给预览区让位）、文字页 10 行铺满
        left.pack(side="top", fill="both", expand=True)
        cols = ("name", "b", "kind", "gb", "layer")
        # Windows：TreeList（Canvas 自绘列表，行高=字行距×1.35）——本构建 Treeview 行距硬编码
        # 20px < 9pt 字行距 28px，相邻行文字粘连（"堆叠无法辨认"，11-23 主人 4 次反馈）
        self.tree = (TreeList(left, columns=cols, height=10, show="headings")
                     if (IS_WIN and TreeList) else
                     ttk.Treeview(left, columns=cols, show="headings", height=10))
        # 行距：默认行高≈字体行高，高分辨率屏上相邻行笔画粘连→"重叠"观感（11-23 主人反馈）。
        # Treeview padding 垂直分量加高行高（168dpi 实测 pad_v=18→+6px；取 14×_dsc≈24→行高+27%）
        ttk.Style().configure("Treeview", padding=(int(2 * self._dsc), int(14 * self._dsc)))
        self._heading_base = {"name": "模型", "b": "B", "kind": "类型", "gb": "GB", "layer": "层"}
        for c, t, w in (("name", "模型", 360), ("b", "B", 44),
                        ("kind", "类型", 148), ("gb", "GB", 48), ("layer", "层", 48)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=max(int(w * self._dsc), 40), anchor="w",
                             stretch=(c == "name"))   # 模型名吃满左栏剩余宽度，防截断
        # 窗口尺寸/DPI 上下文变化（PMDv2 移屏、拉伸）→ 防抖 300ms 树全量重绘。
        # Tk8.6 高缩放屏偶发：缩放/移屏后旧文字残影叠在新文字上（"文字堆叠"，仅实机出现，
        # headless 几何探针查不出——列 bbox 连续无交叠）。重绘=delete+原 iid 重插，选中态保留。
        self._tree_redraw_id = None
        root.bind("<Configure>", self._on_win_configure)
        # 排序：点击「模型 / B / 类型 / GB」表头切换升/降序
        self._sort_col = None
        self._sort_reverse = False
        self._sort_vals = {}
        for c in ("name", "kind", "b", "gb"):
            self.tree.heading(c, text=self._heading_base[c],
                              command=lambda _c=c: self._on_heading_click(_c))
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        # 日志：最底部固定（文字页；图片页同槽换图像预览区 img_previewf）
        logf = ttk.LabelFrame(leftcol, text=" 日志 ")
        logf.pack(side="bottom", fill="both", pady=(4, 0))
        self.logf = logf
        self._track_row(logf, side="bottom", fill="both", pady=(4, 0))
        log_body = ttk.Frame(logf)
        log_body.pack(fill="both", expand=True, padx=4, pady=4)
        self.log = tk.Text(log_body, height=32, state="disabled",
                           font=(MONO, 11), bg="#0e1116", fg="#d7e0ec")
        log_sb = ttk.Scrollbar(log_body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        # 右列
        right = ttk.Frame(mid)
        right.grid(row=0, column=1, sticky="nsew", padx=4)

        # ① 引擎行（文字页：auto/vanilla/tosh + 端口/Host/模式；图片页换 f_eng_img）
        f_eng = ttk.Frame(right)
        f_eng.pack(fill="x")
        self.f_eng = f_eng
        self._track_row(f_eng, fill="x")
        ttk.Label(f_eng, text="引擎").pack(side="left", padx=(0, 4))
        # Windows 双引擎基座：ik/vanilla（默认 vanilla=主人现役 Vulkan 生产构建）；
        # macOS 三引擎：ik（初始默认，纯 CPU 本机最快）/ vanilla / tosh（Metal）
        self.engine_var = tk.StringVar(value="vanilla" if IS_WIN else "ik")
        self.engine_var.trace_add("write", lambda *_: self._save_state_entry())
        for v in (("ik", "vanilla") if IS_WIN else ("ik", "vanilla", "tosh")):
            ttk.Radiobutton(f_eng, text=v, variable=self.engine_var,
                            value=v, command=self._refresh_recommend).pack(side="left", padx=2)
        self.dm_var = tk.BooleanVar(value=False)
        self.dm_var.trace_add("write", lambda *_: self._save_state_entry())
        if not IS_WIN:   # Windows 二进制无 --dynamic-moe 参数（主人 11-23 确认）；构建器对非 tosh 引擎本就跳过该键（recommend.py 门），此处只藏 UI
            ttk.Checkbutton(f_eng, text="--dynamic-moe", variable=self.dm_var,
                            command=self._refresh_recommend).pack(side="left", padx=(10, 0))
        ttk.Label(f_eng, text="端口").pack(side="left", padx=(12, 2))
        self.port_var = tk.StringVar(value="8080")
        ttk.Spinbox(f_eng, from_=1000, to=65535, textvariable=self.port_var,
                   width=7).pack(side="left")
        ttk.Label(f_eng, text="Host").pack(side="left", padx=(12, 2))
        self.host_var = tk.StringVar(value="127.0.0.1")
        ttk.Combobox(f_eng, textvariable=self.host_var,
                     values=["127.0.0.1", "0.0.0.0"], width=10, state="readonly").pack(side="left")
        # 工作模式：覆盖/清空（Windows spec 两种模式）
        ttk.Label(f_eng, text="模式").pack(side="left", padx=(12, 2))
        self.clear_mode_var = tk.StringVar(value="override")
        self.clear_mode_var.trace_add("write", lambda *_: self._save_state_entry())
        for v, t in (("override", "覆盖"), ("clear", "清空")):
            ttk.Radiobutton(f_eng, text=t, variable=self.clear_mode_var,
                            value=v, command=self._refresh_recommend).pack(side="left", padx=2)

        # ② 3 列参数网格（取值均已从 b12212 两个二进制 --help 实测确认；文字页，图片页换 f_img）
        f_p = ttk.Frame(right)
        f_p.pack(fill="x", pady=2)
        self.f_p = f_p
        self._track_row(f_p, fill="x", pady=2)
        self.pv = {}
        hw = self.hw
        tdef = str(hw.physical_cores) if hw.physical_cores else "auto"

        # 三列规格：全部首项 "default"
        specs = [
            ("① CPU/MoE", [
                ("ngl", "ngl GPU层数 -ngl",
                 ["default", "auto", "0", "4", "8", "12", "16", "24", "32", "40", "48", "64", "96", "999"], "auto"),
                ("threads", "threads -t",
                 ["default", "auto", "4", "6", "8", "12"], "8"),
                ("cpu_moe", "cpu-moe",
                 ["default", "off", "on"], "off"),
                ("moe_cache_mib", "moe-cache-mib",
                 ["default", "0", "512", "1024", "1536", "2048", "2560", "3072", "3584", "4096"], "0"),
                ("batch", "batch -b",
                  ["default", "256", "512", "1024", "2048", "4096"], "2048"),
                ("ubatch", "ubatch -ub",
                  ["default", "256", "512", "1024", "2048"], "2048"),
                ("no_warmup", "no-warmup",
                 ["default", "auto", "off", "on"], "auto"),
                ("mmproj", "视觉塔 -mm",
                 ["default", "off"] + MMPROJ_DISPLAY[1:], "off"),
                ("mmproj_device", "--mmproj-device",
                 ["default", "none", "cpu", "gpu"], "none"),
                ("image_min_tokens", "图像最小token",
                  ["default", "256", "512", "1024", "2048"], "1024"),
                ("image_max_tokens", "图像最大token",
                  ["default", "1024", "2048", "4096", "8192", "16384"], "2048"),
                ("agent", "agent",
                 ["default", "off", "on"], "on"),
            ]),
            ("② 上下文/缓存", [
                ("ctx", "ctx -c",
                 ["default", "4096", "8192", "16384", "32768", "65536", "131072"], "65536"),
                ("flash_attn", "flash-attn -fa",
                 ["default", "auto", "on", "off"], "on"),
                ("cache_type_k", "K量化 -ctk",
                 ["default", "f16", "q8_0", "q5_1", "q5_0", "q4_0", "iq4_nl"], "q8_0"),
                ("cache_type_v", "V量化 -ctv",
                 ["default", "f16", "q8_0", "q5_1", "q5_0", "q4_0", "iq4_nl"], "q8_0"),
                ("spec_type", "MTP/投机解码 -st",
                 ["default", "off", "mtp", "ngram-simple", "ngram-cache"], "off"),
                ("spec_draft_model", "MTP draft -md",
                 ["default", "off"] + MTP_DISPLAY[1:], "default"),
                ("spec_draft_n_max", "MTP n-max",
                 ["default", "1", "2", "4", "8", "16", "32"], "default"),
                ("spec_draft_cache_type_k", "MTP draft K量化 -ctkd",
                 ["default", "f16", "q8_0", "q5_1", "q5_0", "q4_0", "iq4_nl"], "q8_0"),
                ("spec_draft_cache_type_v", "MTP draft V量化 -ctvd",
                 ["default", "f16", "q8_0", "q5_1", "q5_0", "q4_0", "iq4_nl"], "q8_0"),
                ("lora", "--lora LoRA（vanilla）",
                 ["default", "off"] + LORA_DISPLAY[1:], "default"),
                ("cache_ram", "cache-ram -cram (MiB)",
                  ["default", "1024", "2048", "3072", "4096", "6144", "7168", "8192"], "4096"),
                ("load_mode", "load-mode -lm",
                 ["default", "auto", "mmap", "mlock", "mmap+mlock", "none"], "auto"),
            ]),
            ("③ 采样/思考", [
                ("temp", "temp",
                 ["default", "0.0", "0.2", "0.5", "0.7", "0.75", "0.8", "1.0", "1.2"], "0.75"),
                ("top_k", "top-k",
                 ["default", "40", "20", "5", "100", "0"], "40"),
                ("top_p", "top-p",
                 ["default", "0.95", "0.90", "0.85", "0.99", "1.0"], "0.95"),
                ("min_p", "min-p",
                 ["default", "0.05", "0", "0.1", "0.2"], "0.05"),
                ("repeat_penalty", "repeat-penalty",
                 ["default", "1.0", "1.05", "1.1", "1.15", "1.2", "1.3"], "1.1"),
                ("presence_penalty", "presence-penalty",
                 ["default", "0", "0.05", "0.1", "0.2", "0.5", "1.0"], "0.05"),
                ("frequency_penalty", "frequency-penalty",
                 ["default", "0", "0.1", "0.2", "0.5", "1.0"], "0.5"),
                ("seed", "seed (-1=随机)",
                 ["default", "-1", "42", "1234"], "-1"),
                ("repeat_last_n", "repeat-last-n",
                 ["default", "64", "128", "256", "512"], "64"),
                ("reasoning", "思考模式 reasoning",
                 ["default", "auto", "on", "off"], "off"),
                ("reasoning_effort", "reasoning-effort",
                 ["default", "minimal", "low", "medium", "high", "max"], "default"),
                ("jinja", "jinja",
                 ["default", "off", "on"], "on"),
            ]),
        ]

        for title, items in specs:
            col = ttk.LabelFrame(f_p, text=" " + title + " ")
            col.pack(side="left", fill="y", padx=2)
            col.grid_columnconfigure(1, weight=1)
            for i, (key, label, choices, default) in enumerate(items):
                var = tk.StringVar(value=default)
                self.pv[key] = var
                self.param_defaults[key] = default
                var.trace_add("write", lambda *_: self._save_state_entry())
                ttk.Label(col, text=label).grid(row=i, column=0, sticky="e", padx=(4, 2), pady=1)
                if key in FREE_EDIT_KEYS:
                    cb = ttk.Combobox(col, textvariable=var, values=choices, width=12)
                    cb.grid(row=i, column=1, sticky="e", padx=2)
                    self.widgets[key] = cb
                else:
                    # ttk.OptionMenu 吞首项：必须垫首 "default"
                    om = ttk.OptionMenu(col, var, "default", *choices)
                    om.grid(row=i, column=1, sticky="e", padx=2)
                    self.widgets[key] = om
                var.set(default)
            col._param_row_count = len(items)
        # ── REQ3 列宽统一（最终方案）：移除 ③ 区历史遗留 col2 minsize=13；
        # ①② 区 OptionMenu 'default' 候选在 macOS Aqua 主题下 reqwidth≈136px
        # （主题行为），无法在不截断的前提下缩到 ③ 区 89px；
        # 三区内部下拉框均 sticky="e" 右对齐，接受 ①② 略宽于 ③。 ──

        # 预设 / 导出 / 导入 行
        f_ps = ttk.Frame(right)
        f_ps.pack(fill="x", pady=(2, 0))
        self.f_ps = f_ps          # 切页归位锚：参数区（f_eng/f_p/f_eng_img/f_img）排在其上
        ttk.Label(f_ps, text="预设").pack(side="left", padx=(0, 4))
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(f_ps, textvariable=self.preset_var,
                                         width=18, state="readonly")
        self.preset_combo.pack(side="left", padx=2)
        ttk.Button(f_ps, text="加载", width=4, command=self._preset_load).pack(side="left")
        ttk.Button(f_ps, text="另存为…", width=7, command=self._preset_save_as).pack(side="left", padx=2)
        ttk.Button(f_ps, text="删除", width=4, command=self._preset_delete).pack(side="left")
        ttk.Separator(f_ps, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(f_ps, text="导出…", width=6, command=self._export).pack(side="left")
        ttk.Button(f_ps, text="导入…", width=6, command=self._import).pack(side="left", padx=2)
        self.preset_hint = ttk.Label(f_ps, text="", foreground="#888")
        self.preset_hint.pack(side="left", padx=6)
        self._refresh_preset_names()

        # ③ 自定义覆盖（用户输入的参数优先于上面面板；同名/别名自动去重）
        f_ovr = ttk.LabelFrame(right, text=" 自定义覆盖（用户参数优先；同名/别名自动去重，如 -c 覆盖 --ctx-size） ")
        f_ovr.pack(fill="x", pady=2)
        self.f_ovr = f_ovr
        self._track_row(f_ovr, fill="x", pady=2)
        self.ovr = tk.Text(f_ovr, height=2, wrap="word", font=(MONO, 11))
        self.ovr.pack(fill="x", padx=4, pady=2)
        self.ovr.bind("<KeyRelease>", self._on_ovr_edit)
        self.ovr_hint = ttk.Label(f_ovr, anchor="w", foreground="#666")
        self.ovr_hint.pack(fill="x", padx=6)

        # 运行/停止按钮：Canvas 自绘，钉在滚动区与硬件负载之间
        acts = ttk.Frame(right)
        acts.pack(fill="x", pady=(0, 0))
        self.acts = acts          # 切页归位锚：自定义覆盖 f_ovr（文字专属）排在其上

        # 尺寸随 DPI 缩放（150×42 定死在 175% 屏上显 ~86×24 虚拟 px，长标签"● 就绪 · 点击启动"溢出）
        btn_w, btn_h = max(int(150 * self._dsc), 150), max(int(42 * self._dsc), 42)
        _BTNF = ("Helvetica", 13 if IS_WIN else 11, "bold")   # Windows 高缩放屏大按钮 13pt
        self.run_cv = tk.Canvas(acts, width=btn_w, height=btn_h, highlightthickness=0, bd=0)
        self.run_cv.pack(side="left", padx=(0, 4))
        self.stop_cv = tk.Canvas(acts, width=btn_w, height=btn_h, highlightthickness=0, bd=0)
        self.stop_cv.pack(side="left", padx=4)
        # ⚡生成指令（左，紧挨 运行/停止）/ 复制（右，贴窗体边）——与 运行/停止 同款 Canvas 风格
        self.gen_cv = tk.Canvas(acts, width=btn_w, height=btn_h, highlightthickness=0, bd=0)
        self.gen_cv.pack(side="left", padx=4)
        self.copy_cv = tk.Canvas(acts, width=btn_w, height=btn_h, highlightthickness=0, bd=0)
        self.copy_cv.pack(side="right", padx=(4, 0))
        # 状态机变量：_ready=server 健康就绪（运行钮变绿的门）；_cmd_available=命令已生成
        self._ready = False
        self._cmd_available = False
        # 各按钮当前标签（按下反馈用）
        self._btn_labels = {"run": "▶ 运行", "stop": "■ 停止", "gen": "⚡ 生成指令", "copy": "复制"}
        _cv_key = {self.run_cv: "run", self.stop_cv: "stop", self.gen_cv: "gen", self.copy_cv: "copy"}

        def draw_btn(cv, text, bg, fg, key=None):
            cv.delete("all")
            cv.create_rectangle(0, 0, btn_w, btn_h, fill=bg, outline="")
            cv.create_text(btn_w/2, btn_h/2, text=text, fill=fg, font=_BTNF)
            if key:
                self._btn_labels[key] = text

        # 11-23 主人令：选中模型后（cmd 可用，与 ⚡生成/复制 同态）运行/停止亦变深灰 #4a5568；
        # 未选中时初始页四钮统一浅灰 #d5d7dd；就绪绿/运行中红不变
        self._draw_run = lambda running, ready, avail: draw_btn(
            self.run_cv,
            "● 就绪 · 运行中" if (running and ready) else (
                "● 正在加载" if running else
                ("● 就绪 · 点击启动" if ready else "▶ 运行（加载后启动）")),
            "#2e7d32" if ready else ("#4a5568" if avail else "#d5d7dd"),
            "white" if (ready or avail) else "#999", key="run"
        )
        self._draw_stop = lambda running, ready, avail: draw_btn(
            self.stop_cv,
            "■ 停止",
            "#c62828" if (running and ready) else ("#4a5568" if avail else "#d5d7dd"),
            "white" if ((running and ready) or avail) else "#999", key="stop"
        )
        # 生成指令 = 复制 同色（深灰 #4a5568，可用时），禁用时同为浅灰 #d5d7dd
        self._draw_gen = lambda avail: draw_btn(
            self.gen_cv, "⚡ 生成指令",
            "#4a5568" if avail else "#d5d7dd",
            "white" if avail else "#999", key="gen"
        )
        self._draw_copy = lambda avail: draw_btn(
            self.copy_cv, "复制",
            "#4a5568" if avail else "#d5d7dd",
            "white" if avail else "#999", key="copy"
        )
        # 统一重绘四个按钮（ready / running / cmd 任一状态变化时调用）

        def _paint_all():
            running = self.proc is not None and self.proc.poll() is None
            self._draw_run(running, self._ready, self._cmd_available)
            self._draw_stop(running, self._ready, self._cmd_available)
            self._draw_gen(self._cmd_available)
            self._draw_copy(self._cmd_available)
        self._refresh_btns = _paint_all

        # 视觉反馈：<ButtonPress-1> 必须 add="+"，否则顶掉 <Button-1> 点击逻辑
        # 按下：整块加深一档，文字保留原色（松手后 _refresh_btns 自动还原）
        for cv, key in _cv_key.items():
            def _press(e, c=cv, k=key):
                c.delete("all")
                c.create_rectangle(0, 0, btn_w, btn_h, fill="#c9cbd4", outline="")
                c.create_text(btn_w/2, btn_h/2, text=self._btn_labels[k], fill="#111",
                              font=_BTNF)
            cv.bind("<ButtonPress-1>", _press, add="+")
        self.run_cv.bind("<Button-1>", lambda e: self._on_run_click(), add="+")
        self.stop_cv.bind("<Button-1>", lambda e: self._on_stop_click(), add="+")
        self.gen_cv.bind("<Button-1>", lambda e: self._on_gen_click(), add="+")
        self.copy_cv.bind("<Button-1>", lambda e: self._on_copy_click(), add="+")
        self._refresh_btns()

        # 硬件负载（实时 2s）：CPU/内存/GPU 百分比 + 条状图
        hwload = ttk.LabelFrame(right, text=" 硬件负载（实时 2s） ")
        hwload.pack(fill="x", pady=(0, 0))
        self.hwload_rows = {}
        for name in ("CPU", "内存", "GPU"):
            row = ttk.Frame(hwload)
            row.pack(fill="x", padx=4, pady=1)
            ttk.Label(row, text=name, width=5).pack(side="left")
            cv = tk.Canvas(row, height=12, highlightthickness=0, bg="#fbfcfe")
            cv.pack(side="left", fill="x", expand=True, padx=(4, 8))
            pct = ttk.Label(row, text="—", width=8, anchor="e")
            pct.pack(side="right")
            self.hwload_rows[name] = (cv, pct)

        # 后端命令框（钉最底部）
        cmdbox = ttk.LabelFrame(right, text=" 后端命令 ")
        cmdbox.pack(fill="x")
        self.cmdbox = cmdbox      # 右列参数区（f_eng/f_p/f_ovr/f_eng_img/f_img）切页归位锚点
        cmd_body = ttk.Frame(cmdbox)
        cmd_body.pack(fill="x", padx=4, pady=4)
        self.cmd = tk.Text(cmd_body, height=7, wrap="word", font=(MONO, 11))
        cmd_sb = ttk.Scrollbar(cmd_body, orient="vertical", command=self.cmd.yview)
        self.cmd.configure(yscrollcommand=cmd_sb.set)
        self.cmd.pack(side="left", fill="both", expand=True)
        cmd_sb.pack(side="right", fill="y")
        self.info = tk.Label(
            cmdbox, anchor="w", justify="left", fg="#c77700",
            font=(MONO, 10), wraplength=560, height=6)
        self.info.pack(fill="x", padx=4, pady=(0, 4))
        self.info.config(text="（选择左侧模型以查看推荐参数）")
        # 文生图（图片页）控件：初建后隐藏，切「图片」页时显示
        self._build_image_page(root, leftcol, right)
        self._set_page_visible("text")   # 初始 = 文字页
        # 初始框体收紧：底边框贴「后端命令」框下沿、右边框贴 ③ 参数窗右沿
        self._fit_frame()

    def _fit_frame(self):
        """把初始窗口收紧到内容需求尺寸：右沿=③参数窗右沿（最宽行），下沿=后端命令框下沿（右栏最末）。
        高度由两个文本窗口显式行高决定（11-20 主人定）：后端命令 self.cmd=7 行、
        引擎提示说明 self.info=6 行（二者已写进控件构造器，reqheight 自然含之），
        故取 root 的 reqwidth/reqheight 即恰好填满、无多余留白；不再叠加 11-15 旧规的通用 +6 行小字。
        左栏（树/日志）自身无固有高度（fill=both，reqheight=1），整栏被右栏 reqheight 驱动。用户后续仍可自由拉伸放大。"""
        root = self.root
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        if w > 400 and h > 300:  # 防异常值；否则退回 1440x954
            root.geometry(f"{w}x{h}")
            self._text_win_w, self._text_win_h = w, h
        else:
            root.geometry("1560x954")
            self._text_win_w, self._text_win_h = 1560, 954

    # ================= 文生图（图片页） =================
    def _track_row(self, frame, **cfg):
        self._rowcfg[frame] = cfg

    def _build_image_page(self, root, leftcol, right):
        """构建图片页专属控件（初建后由 _set_page_visible 控制显隐）。"""
        self.img_cache = {}
        # 图片页引擎条（第二行；文字页 engbar/upgbar 同槽）
        img_engbar = ttk.Frame(root)
        img_engbar.pack(fill="x", padx=8, pady=(0, 2))
        self.img_engbar = img_engbar
        self._track_row(img_engbar, fill="x", padx=8, pady=(0, 2))
        cfg0 = _load_config()
        self.img_engine_var = tk.StringVar(value=cfg0.get("image_bin") or IMG_ENGINE_DEFAULT)
        ttk.Label(img_engbar, text="图引擎(sd-cli):", width=14, anchor="w").pack(side="left")
        ttk.Entry(img_engbar, textvariable=self.img_engine_var).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(img_engbar, text="…", width=3, command=self._browse_img_engine).pack(side="left", padx=(0, 6))
        self.img_reserve_var = tk.StringVar(value=cfg0.get("reserve_bin") or IMG_RESERVE_DEFAULT)
        ttk.Label(img_engbar, text="预留:", width=6, anchor="w").pack(side="left")
        ttk.Entry(img_engbar, textvariable=self.img_reserve_var).pack(side="left", fill="x")
        ttk.Button(img_engbar, text="应用", width=5, command=self._apply_img_engbar).pack(side="left", padx=(4, 0))

        # 图片页 ① 引擎行（文生图引擎 / 预留第二引擎；无端口/Host/模式）
        f_eng_img = ttk.Frame(right)
        f_eng_img.pack(fill="x")
        self.f_eng_img = f_eng_img
        self._track_row(f_eng_img, fill="x")
        ttk.Label(f_eng_img, text="引擎").pack(side="left", padx=(0, 4))
        self.img_engvar = tk.StringVar(value="img")
        for v, t in (("img", "文生图(sd-cli)"), ("reserve", "预留(第二引擎)")):
            ttk.Radiobutton(f_eng_img, text=t, variable=self.img_engvar, value=v).pack(side="left", padx=2)

        # ── 上块 f_img_top：① 模型/编码器/VAE/LoRA 目录（子列A） + ② 生成参数 + LoRA（子列B）
        #    （取消原 ①②③ 三列并排；严格 2 块：上=文件+生成参数，下=提示词/负面）
        # 外层不再起「① 模型定制 + ② 生成参数」标题——与 colA/colB 自带 ①/② 标题重复，故用普通 Frame
        f_img_top = ttk.Frame(right)
        f_img_top.pack(fill="x", pady=2, before=self.f_ps)
        self.f_img_top = f_img_top
        self._track_row(f_img_top, fill="x", pady=2)
        # ①② 真等宽：两列各 50%。给两列同一 minsize（抬到内容自然宽之上）→ 列宽= minsize+均分余量，
        # 与内容宽解耦，两列恒等（否则 ① 内容宽 > ②，grid 等权仍 ①宽②窄）。
        f_img_top.columnconfigure(0, weight=1, minsize=400)
        f_img_top.columnconfigure(1, weight=1, minsize=400)
        f_img_top.rowconfigure(0, weight=1)
        colA = ttk.LabelFrame(f_img_top, text=" ① 模型/编码器/VAE/LoRA 目录 ")
        colA.grid(row=0, column=0, sticky="nsew", padx=(2, 1), pady=2)   # ① 占 grid 第 0 列（50%）
        colA.grid_columnconfigure(1, weight=1)   # 下拉列撑满 → 尽量宽，路径/目录可辨认
        _dir_items = [("model", "--diffusion-model"), ("encoder", "--llm"),
                      ("vae", "--vae"), ("lora_dir", "--lora-model-dir"),
                      ("ref", "--ref-image")]   # 参考图（图改图；仅 MiniMax edit_capable 生效，留空=纯文生图）
        for i, (key, flag) in enumerate(_dir_items):
            var = tk.StringVar(value="")
            self.ipv[key] = var
            var.trace_add("write", lambda *_: self._refresh_recommend())
            ttk.Label(colA, text=flag).grid(row=i, column=0, sticky="w", padx=(4, 2), pady=1)   # flag 靠左
            cb = ttk.Combobox(colA, textvariable=var, width=12)   # 自然宽小 → ①② grid 真等宽；sticky=we 撑满 50% 列
            cb.grid(row=i, column=1, sticky="we", padx=2)          # 撑满中间列
            self.iwidgets[key] = cb
            ttk.Button(colA, text="…", width=3,                          # 只留 3 个点，无「浏览」
                       command=lambda k=key: self._browse_img_param(k)).grid(row=i, column=2, sticky="e", padx=(2, 4))
        colB = ttk.LabelFrame(f_img_top, text=" ② 生成参数 ")
        colB.grid(row=0, column=1, sticky="nsew", padx=(1, 2), pady=2)   # ② 占 grid 第 1 列（50%）
        colB.grid_columnconfigure(0, weight=1)                      # label 吃左 → OptionMenu 右对齐
        _gen = [("aspect", "宽高比", IMAGE_ASPECTS, "16:9"), ("res", "分辨率", IMAGE_RES, "768"),
                ("cfg", "Cfg", IMAGE_CFG, "1.1"), ("sampler", "采样器", IMAGE_SAMPLERS, "default"),
                ("steps", "步数", IMAGE_STEPS, "16"), ("seed", "种子(-1随机)", IMAGE_SEEDS, "-1"),
                ("format", "格式", IMAGE_FMTS, "png"),
                ("strength", "改图强度", ["default", "0.3", "0.5", "0.7", "0.75", "0.9"], "default")]
        for i, (key, label, choices, default) in enumerate(_gen):
            var = tk.StringVar(value=default)
            self.ipv[key] = var
            var.trace_add("write", lambda *_: self._refresh_recommend())
            ttk.Label(colB, text=label).grid(row=i, column=0, sticky="w", padx=(4, 2), pady=1)
            om = ttk.OptionMenu(colB, var, choices[0], *choices)
            om.grid(row=i, column=1, sticky="e", padx=2)   # 控件右对齐
            self.iwidgets[key] = om
        # LoRA 定制（下拉选择 → 已选列表：各自权重滑杆 + 移除；没选中的不出现）
        _r = len(_gen)
        ttk.Label(colB, text="LoRA（下拉选择，选中见下方）").grid(row=_r, column=0, columnspan=2,
                                                                    sticky="w", padx=(4, 2), pady=(6, 0))
        self.lora_pick_var = tk.StringVar(value="")
        dd = ttk.Combobox(colB, textvariable=self.lora_pick_var, values=[], width=16, state="readonly")
        dd.grid(row=_r + 1, column=0, columnspan=2, sticky="we", padx=2)
        dd.bind("<<ComboboxSelected>>", lambda _e: self._add_lora())
        self.lora_pick_dd = dd
        self.lora_chosen = {}             # name -> DoubleVar(权重 0.0–2.0)，仅已选
        self.lora_w_cache = {}           # 移除/换模型时缓存权重
        self.lora_chosen_frame = ttk.Frame(colB)
        self.lora_chosen_frame.grid(row=_r + 2, column=0, columnspan=2, sticky="we", padx=2)
        self.lora_frame = self.lora_chosen_frame
        colB.grid_rowconfigure(_r + 2, weight=1)
        self._avail_loras = []
        self.lora_dir_cur = ""
        self._rebuild_lora_table(os.path.join(IMAGE_MODELS_DIR_DEFAULT, "loras"))

        # ── 下块 f_img_bot：③ 提示词 + 负面提示词（并排 2 列；expand 撑开消除右列底部空白）
        f_img_bot = ttk.LabelFrame(right, text=" ③ 提示词 / 负面提示词 ")
        f_img_bot.pack(fill="both", expand=True, pady=2, before=self.f_ps)
        self.f_img_bot = f_img_bot
        self._track_row(f_img_bot, side="top", fill="both", expand=True, pady=2)

        def mk_text(parent, key, label):
            wc = ttk.Frame(parent)
            wc.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            ttk.Label(wc, text=label).pack(anchor="w")
            tx = tk.Text(wc, height=6, wrap="word", font=(MONO, 11))
            tx.pack(fill="both", expand=True)
            tx.bind("<KeyRelease>", lambda *_: self._refresh_recommend())
            self.itext[key] = tx
        mk_text(f_img_bot, "prompt", "提示词")
        mk_text(f_img_bot, "neg", "负面提示词")

        # 图像预览区（替换文字页日志 frame，同槽；占模型库 5 行下方剩余空间）
        img_previewf = ttk.LabelFrame(leftcol, text=" 图像预览 ")
        img_previewf.pack(side="bottom", fill="both", pady=(4, 0))
        self.img_previewf = img_previewf
        self._track_row(img_previewf, side="bottom", fill="both", pady=(4, 0))
        # 顶栏（预览窗口上方）：输出目录 + 浏览 + 最近输出 + 刷新 + 打开目录
        ctr = ttk.Frame(img_previewf); ctr.pack(side="top", fill="x", padx=4, pady=(2, 2))
        ttk.Label(ctr, text="输出目录").pack(side="left")
        self.img_out_var = tk.StringVar(value=self.img_out_dir)
        ttk.Entry(ctr, textvariable=self.img_out_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(ctr, text="浏览…", command=self._browse_img_out).pack(side="left")
        self.img_last_lbl = ttk.Label(ctr, text="最近：（无）", anchor="w", foreground="#888")
        self.img_last_lbl.pack(side="left", padx=(6, 2))
        ttk.Button(ctr, text="刷新", command=self._refresh_img_preview).pack(side="left", padx=2)
        ttk.Button(ctr, text="打开目录", command=self._open_img_out).pack(side="left", padx=2)
        # 选中图操作行：点缩略图选中 → 删除 / 另存为 / 查看原图
        selbar = ttk.Frame(img_previewf); selbar.pack(side="top", fill="x", padx=4, pady=(0, 2))
        self.img_sel_lbl = ttk.Label(selbar, text="选中：（未选）", anchor="w", foreground="#888", width=14)
        self.img_sel_lbl.pack(side="left")
        ttk.Button(selbar, text="删除", command=self._img_delete_selected).pack(side="left", padx=(6, 2))
        ttk.Button(selbar, text="另存", command=self._img_save_selected).pack(side="left", padx=2)
        ttk.Button(selbar, text="查看", command=self._img_view_selected).pack(side="left", padx=2)
        # 出图进度行：X/N 步 Progressbar（sd-cli 步数实时解析）
        progbar = ttk.Frame(img_previewf); progbar.pack(side="top", fill="x", padx=4, pady=(0, 2))
        self.img_prog_lbl = ttk.Label(progbar, text="出图：就绪", anchor="w", width=16)
        self.img_prog_lbl.pack(side="left")
        self.img_prog = ttk.Progressbar(progbar, orient="horizontal", mode="determinate")
        self.img_prog.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._img_selected = None
        # 2 列缩略图网格（最近 N 张，纵向可滚；点某张 → 原图大小查看）
        gwrap = ttk.Frame(img_previewf); gwrap.pack(side="top", fill="both", expand=True, padx=4, pady=(0, 4))
        self.img_grid = tk.Canvas(gwrap, bg="black", highlightthickness=0)   # 底色默认黑色
        gs = ttk.Scrollbar(gwrap, orient="vertical", command=self.img_grid.yview)
        self.img_grid.configure(yscrollcommand=gs.set)
        gs.pack(side="right", fill="y")
        self.img_grid.pack(side="left", fill="both", expand=True)
        self.img_grid.bind("<Button-1>", self._on_thumb_click)          # 单击=选中（供 删除/另存/查看）
        self.img_grid.bind("<Double-Button-1>", self._on_thumb_dclick)  # 双击=查看原图
        self._thumb_items = []           # [(x, y, w, h, path), ...]
        self._thumb_photos = []          # PIL/PhotoImage 引用防 GC
        self._thumb_cols = 2            # 2 列
        self._thumb_size = (150, 110)   # 单张缩略 宽×高
        self._thumb_n = 8               # 最近 N 张

    def _set_page_visible(self, page):
        """仅切换可见性 + 导航按钮态；不重扫（重扫在 _switch_page）。
        关键：page-frame 须用 before= 锚定回原槽位——pack_forget 后默认 pack 会落到
        父容器末尾（root 顶层条/右列参数区被挤到底部，版面乱套、切页像失效）。"""
        self.page = page
        for fr in list(self._rowcfg):
            fr.pack_forget()
        # 左列模型库：图片页固定 5 行、不铺满（给预览区让位）；文字页 10 行铺满
        self.left.pack_forget()
        self.tree.configure(height=5 if page == "image" else 10)
        if page == "image":
            self.left.pack(side="top", fill="x")
        else:
            self.left.pack(side="top", fill="both", expand=True)
        active = ([self.engbar, self.f_eng, self.f_p, self.f_ovr, self.logf]
                  if page == "text"
                  else [self.img_engbar, self.f_eng_img, self.f_img_top, self.f_img_bot, self.img_previewf])
        # 稳定锚点：每个 page-frame 归位到它 build 时的精确槽位（before= 其后驱共享帧）——
        #   顶层条(engbar/img_engbar) → dirbar 上
        #   参数区(f_eng/f_p 文字 · f_eng_img/f_img_top/f_img_bot 图片) → 预设行 f_ps 上
        #   自定义覆盖 f_ovr（文字专属） → 运行行 acts 上
        #   side=bottom 的 logf/img_previewf 是左列唯一底栏，默认 pack 即可
        anchor = {self.engbar: self.dirbar,
                  self.img_engbar: self.dirbar,
                  self.f_eng: self.f_ps, self.f_p: self.f_ps,
                  self.f_eng_img: self.f_ps, self.f_img_top: self.f_ps, self.f_img_bot: self.f_ps,
                  self.f_ovr: self.acts}
        for fr in active:
            cfg = dict(self._rowcfg[fr])
            if fr in anchor:
                cfg["before"] = anchor[fr]
            if fr is self.img_previewf:
                cfg["expand"] = True       # 预览区撑模型库 5 行下方剩余（可多图）
            fr.pack(**cfg)
        # 导航按钮：当前所在页 disable，另一页 enable（才能点过去切）
        self._nav_text_btn.config(state=("disabled" if page == "text" else "normal"))
        self._nav_img_btn.config(state=("normal" if page == "text" else "disabled"))
        self._refresh_preset_names()       # 预设区按当前页重列（文字/图片预设分离）

    def _switch_page(self, page):
        if page == self.page:
            return
        self._set_page_visible(page)
        self._log(f"切换到「{'图片' if page == 'image' else '文字'}」页")
        # 切页先清空模型库列表（扫描是异步、NVMe 慢）——否则图片页会残留文字页 LLM 列表
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", values=("（正在扫描模型库…）",))   # 占位提示，_fill_tree* 开头会清掉
        if page == "image":
            self.dir_var.set(self._image_models_dir)
            self._img_model = None
            self._scan_image()
            # 图片页窗体宽度 = 初始页面（文字页）最右端（_text_win_w），非屏幕最右
            self.root.update_idletasks()
            self.root.geometry(f"{getattr(self, '_text_win_w', 1560)}x{getattr(self, '_text_win_h', 954)}")
        else:
            self.dir_var.set(self._text_models_dir)   # 复位到文字页目录（勿用被 image 页污染的 self.models_dir）
            self._scan_text()
            # 切回文字页：恢复原窗体尺寸
            self.root.geometry(f"{getattr(self, '_text_win_w', 1560)}x{getattr(self, '_text_win_h', 954)}")
        self._refresh_recommend()

    def _scan_text(self):
        self.models_dir = (self.dir_var.get().strip() or MODELS_DIR)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_image(self):
        self.models_dir = (self.dir_var.get().strip() or IMAGE_MODELS_DIR_DEFAULT)
        threading.Thread(target=self._scan_image_worker, daemon=True).start()

    def _scan_image_worker(self):
        try:
            models = scan_image_models(self.models_dir)
        except Exception as e:
            self.q.put(f"[错误] 文生图模型扫描失败: {e}")
            return
        self.q.put("__IMG_SCAN_DONE__:" + json.dumps(models, ensure_ascii=False))

    def _fill_tree_image(self, models):
        if self.page != "image":
            return                       # 跨页竞态：非图片页时丢弃 image 扫描回填（避免文字页被填成 image 模型）
        self.tree.delete(*self.tree.get_children())
        self.img_cache = {}
        self._sort_vals = {}
        for e in models:
            try:
                gb = os.path.getsize(e["model"]) / 1073741824 if os.path.isfile(e["model"]) else 0
            except OSError:
                gb = 0
            iid = self.tree.insert("", "end", values=(e["name"], "-", "文生图", f"{gb:.1f}", "-", e["model"]))
            self.img_cache[e["model"]] = e
            self._sort_vals[iid] = {"name": e["name"], "b": 0.0, "kind": "文生图",
                                     "gb": float(gb), "layer": 0}
        if self._sort_col:
            self._sort_tree()
        self._calibrate_tree_font()

    def _rebuild_lora_table(self, loras_dir):
        """按 loras 目录重建 ② 区 LoRA 定制：更新下拉可用 LoRA + 已选列表（各自权重滑杆 + 移除）。
        已选里不在新目录的自动剔除（权重入 lora_w_cache 缓存，换回模型可恢复）。"""
        self.lora_dir_cur = loras_dir or ""
        self._avail_loras = sorted(list_loras(loras_dir))
        for n in [n for n in self.lora_chosen if n not in self._avail_loras]:
            self.lora_w_cache[n] = float(self.lora_chosen[n].get())
            del self.lora_chosen[n]
        self.lora_pick_dd["values"] = self._avail_loras
        self.lora_pick_var.set("")
        self._render_lora_chosen()

    def _add_lora(self):
        """下拉选中一个 LoRA → 加入已选列表（带权重滑杆）；重复选/空选忽略。"""
        name = self.lora_pick_var.get().strip()
        if not name or name not in self._avail_loras or name in self.lora_chosen:
            self.lora_pick_var.set("")
            return
        self.lora_chosen[name] = tk.DoubleVar(value=float(self.lora_w_cache.get(name, 0.8)))
        self.lora_pick_var.set("")
        self._render_lora_chosen()
        self._refresh_recommend()

    def _remove_lora(self, name):
        if name in self.lora_chosen:
            self.lora_w_cache[name] = float(self.lora_chosen[name].get())
            del self.lora_chosen[name]
            self._render_lora_chosen()
            self._refresh_recommend()

    def _render_lora_chosen(self):
        """把已选 LoRA 渲染成「名字 + 权重滑杆 + 数值 + 移除」列表（没选中的不出现）。"""
        fr = self.lora_chosen_frame
        for _c in fr.winfo_children():
            _c.destroy()
        for r, (name, wv) in enumerate(list(self.lora_chosen.items())):
            row = ttk.Frame(fr)
            row.grid(row=r, column=0, sticky="we", pady=1)
            row.columnconfigure(1, weight=1)
            ttk.Label(row, text=name, width=18, anchor="w").grid(column=0, row=0, sticky="w")
            sl = ttk.Scale(row, from_=0.0, to=2.0, variable=wv, orient="horizontal", length=90)
            sl.grid(column=1, row=0, sticky="we", padx=4)
            lbl = ttk.Label(row, text=f"{wv.get():.2f}", width=4, foreground="#c77700")
            lbl.grid(column=2, row=0, sticky="w")
            def _u(*_, _l=lbl, _w=wv):
                _l.config(text=f"{_w.get():.2f}")
                self._refresh_recommend()
            wv.trace_add("write", _u)
            ttk.Button(row, text="✕", width=2,
                       command=lambda n=name: self._remove_lora(n)).grid(column=3, row=0, sticky="w", padx=(4, 0))

    def _browse_img_param(self, key):
        """① 区浏览定制：model/encoder/vae → 选文件；lora_dir → 选目录（并刷新 LoRA 下拉）；ref → 选参考图。"""
        if key == "lora_dir":
            d = filedialog.askdirectory(parent=self.root, title="选择 LoRA 目录")
            if d:
                self.ipv["lora_dir"].set(d)
                self._rebuild_lora_table(d)
        elif key == "ref":
            p = filedialog.askopenfilename(
                parent=self.root, title="选择参考图（图改图，仅 MiniMax 生效）",
                filetypes=(("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.tiff"), ("所有文件", "*.*")))
            if p:
                self.ipv["ref"].set(p)
        else:
            p = filedialog.askopenfilename(
                parent=self.root, title="选择文件",
                filetypes=(("GGUF 模型/编码器", "*.gguf"), ("VAE", "*.safetensors"), ("所有文件", "*.*")))
            if p:
                self.ipv[key].set(p)

    def _set_img_combo_values(self, key, values):
        w = self.iwidgets.get(key)
        if isinstance(w, ttk.Combobox):
            uniq = [v for v in values if v]
            w["values"] = uniq

    def _img_prefill_from_model(self, m):
        if not m:
            return
        pf = m.get("prefill") or {}
        vals = {
            "model": m.get("model", ""), "encoder": m.get("encoder", ""),
            "vae": m.get("vae", ""), "lora_dir": m.get("loras", ""),
            "cfg": str(pf.get("cfg") or "1.1") if pf else "1.1",
            "aspect": str(pf.get("aspect") or "16:9") if pf else "16:9",
            "res": str(pf.get("res") or "768") if pf else "768",
            "sampler": "default",
            "steps": str(pf.get("steps") or "16") if pf else "16",
            "seed": str(pf.get("seed")) if (pf and pf.get("seed") is not None) else "-1",
            "format": str(pf.get("format") or "png") if pf else "png",
        }
        # ① 区下拉变体（模型量化 / 编码器 / VAE 候选）→ B 配方各元素可切换
        self._set_img_combo_values("model", m.get("model_variants") or [m.get("model", "")])
        self._set_img_combo_values("encoder", m.get("encoders") or [m.get("encoder", "")])
        self._set_img_combo_values("vae", m.get("vae_cands") or [m.get("vae", "")])
        for k, v in vals.items():
            if k in self.ipv:
                try:
                    self.ipv[k].set(v)
                except tk.TclError:
                    pass
        self._set_img_text("prompt", (pf or {}).get("prompt") or "")
        self._set_img_text("neg", (pf or {}).get("neg") or "")
        self._rebuild_lora_table(m.get("loras", ""))

    def _set_img_text(self, key, val):
        tx = self.itext.get(key)
        if not tx:
            return
        tx.config(state="normal")
        tx.delete("1.0", "end")
        tx.insert("1.0", val or "")
        tx.config(state="normal")

    def _refresh_recommend_image(self):
        m = self._img_model
        p = {k: self.ipv[k].get() for k in self.ipv if k in ("model", "encoder", "vae", "lora_dir",
                                                              "aspect", "res", "cfg", "sampler",
                                                              "steps", "seed", "format",
                                                              "ref", "strength")}
        p["edit_capable"] = bool((m or {}).get("edit_capable"))   # MiniMax 才走图改图（-r）；m 可能为 None
        user_prompt = self.itext["prompt"].get("1.0", "end").strip()
        p["neg"] = self.itext["neg"].get("1.0", "end").strip()
        # LoRA：②区下拉选择的已选 LoRA（各自权重）→ 提示词 <lora:名:权重> 标签 + --lora-model-dir
        sel = list(self.lora_chosen.keys())
        wt = {n: float(self.lora_chosen[n].get()) for n in sel}
        if sel:
            tags = " ".join(f"<lora:{n}:{wt[n]:.2f}>" for n in sel)
            p["prompt"] = (tags + " " + user_prompt).strip()
            p["lora_dir"] = (p.get("lora_dir") or getattr(self, "lora_dir_cur", "")
                             or (self._img_model or {}).get("loras", "")).strip()
        else:
            p["prompt"] = user_prompt
            p["lora_dir"] = ""          # 无 LoRA → 不传 --lora-model-dir（防空目录/静默无效）
        if not m:
            self.info.config(text="（选择左侧文生图模型以生成命令）", fg="#888")
            self._cmd_available = False
            self._refresh_btns()
            return
        if not p.get("model") or not p.get("encoder"):
            self.info.config(text=f"模型或文本编码器路径缺失（{m['name']}）", fg="#c0392b")
            self._cmd_available = False
            self._refresh_btns()
            return
        img_engine = (self.img_engine_var.get().split("（自动）")[0].split("(自动)")[0].strip()
                     or IMG_ENGINE_DEFAULT)
        outdir = (self.img_out_var.get().strip() if hasattr(self, "img_out_var") else self.img_out_dir) or self.img_out_dir
        try:
            offload = os.path.getsize(p["model"]) > 5 * 1073741824
        except OSError:
            offload = False
        cmd, out = build_image_command(m, p, outdir, img_engine, offload=offload)
        import shlex
        # shlex.join：含空格的 -p/-n 提示词自动加引号 → _run_image 的 shlex.split 才能还原为单 token
        # （旧 " ".join 不加引号，提示词被按空格拆碎 → sd-cli 只吃到第一个词 = 不理提示词/无法生成）
        cmd_str = shlex.join(cmd)
        self._last_cmd = cmd_str
        self.cmd.config(state="normal")
        self.cmd.delete("1.0", "end")
        self.cmd.insert("1.0", cmd_str)
        self.cmd.config(state="disabled")
        w, h = _aspect_wh(p.get("aspect", "16:9"), p.get("res", "768"))
        sam = p.get("sampler") or "default"
        note = (f"{m['name']} · {w}×{h} · 步数 {p.get('steps')} · cfg {p.get('cfg')} · 采样器 {sam}"
                f"\n编码器 {os.path.basename(p.get('encoder') or '')} · VAE {os.path.basename(p.get('vae') or '')}"
                f"\n输出 → {outdir}")
        if offload:
            note += "\n（权重 >5GB 已自动加 --offload-to-cpu）"
        if sel:
            note += ("\n已挂 LoRA：" + "，".join(f"{n} {wt[n]:.2f}" for n in sel)
                     + "；Turbo/低步数建议 cfg≤1.5 防烧色")
        self.info.config(text=note, fg="#c77700")
        self._cmd_available = True
        self._refresh_btns()

    def _run_image(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("生成中", "已有生成任务在运行，请先停止。")
            return
        cmd_str = self.cmd.get("1.0", "end").strip()
        if not cmd_str:
            messagebox.showwarning("无命令", "请先选择文生图模型并填写提示词。")
            return
        import shlex
        cmd_list = shlex.split(cmd_str)   # 双轨：posix 解析去外层引号（Popen 收不带引号的 token）
        outdir = (self.img_out_var.get().strip() if hasattr(self, "img_out_var") else self.img_out_dir) or self.img_out_dir
        self._out_dir = outdir
        self._running_bin = cmd_list[0]
        self._log(f"[图片] 启动生成: {cmd_str}")
        try:
            bin_dir = os.path.dirname(cmd_list[0]) or BASE   # sd-cli 同目录 DLL 靠 cwd 找到
            if IS_WIN:
                _env = dict(os.environ)
                _env["PYTHONIOENCODING"] = "utf-8"
                self.proc = subprocess.Popen(
                    cmd_list, cwd=bin_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=_env,
                    creationflags=0x08000000,
                    text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
            else:
                self.proc = subprocess.Popen(cmd_list, shell=False, cwd=BASE,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, start_new_session=True,
                                              text=True, bufsize=1)
        except Exception as e:
            self._log(f"[图片] 启动失败: {e}")
            self.status.config(text="就绪", foreground="#16202e")
            return
        self._reader_stop = False
        self._health_stop = True          # 文生图一次性生成，无 /health 轮询
        threading.Thread(target=self._img_reader, daemon=True).start()
        self._set_run_ui(True)
        self.status.config(text="生成中…", foreground="#c77700")
        self._update_img_progress(0, 100, "加载模型…")   # 统一 0-100 标尺：加载 0-50、采样 50-100

    def _img_reader(self):
        import re as _re
        p = self.proc
        # sd-cli（LocModel）实际进度行："|...条形...| CUR/TOT - X.XX(GB/s|s/it|it/s)"
        # GB/s = 张量加载相位；s/it 或 it/s = 采样步数相位（11-23 主人报"进度条不反映实际进度"根因：
        # 旧正则只匹配 it/s，采样主循环多为 s/it 形式 → 事件几乎不发，进度条卡住）
        sd_re = re.compile(r'\|\s*(\d+)\s*/\s*(\d+)\s*-\s*[\d.]+\s*(GB/s|s/it|it/s)')
        step_re2 = re.compile(r'\bstep\s*(\d+)\s*/\s*(\d+)', re.I)   # stable-diffusion.cpp "Step X/Y" 变体
        for line in iter(p.stdout.readline, ""):
            self.q.put("OUT:" + line)
            ms = sd_re.search(line)
            if ms:
                phase = "L:" if ms.group(3) == "GB/s" else ""
                self.q.put(f"__IMG_PROGRESS__:{phase}{ms.group(1)}:{ms.group(2)}")
                continue
            m2 = step_re2.search(line)
            if m2:
                self.q.put(f"__IMG_PROGRESS__:{m2.group(1)}:{m2.group(2)}")
        rc = p.wait()
        newest = self._find_newest(getattr(self, "_out_dir", self.img_out_dir))
        self.q.put(f"__IMG_DONE__:{rc}:{newest or ''}")

    def _find_newest(self, outdir):
        import glob
        best, bt = None, 0.0
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for f in glob.glob(os.path.join(outdir, ext)):
                try:
                    m = os.path.getmtime(f)
                except OSError:
                    continue
                if m > bt:
                    bt, best = m, f
        return best

    def _list_recent_imgs(self, outdir, n=None):
        import glob
        n = n or getattr(self, "_thumb_n", 8)
        files = []
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            files += glob.glob(os.path.join(outdir, ext))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files[:n]

    def _load_preview(self, path):
        # 兼容旧调用（__IMG_DONE__ 泵事件）：直接刷新整个 2 列缩略图网格
        self._refresh_img_preview()

    def _refresh_img_preview(self):
        outdir = (self.img_out_var.get().strip() if hasattr(self, "img_out_var") else self.img_out_dir)
        recent = self._list_recent_imgs(outdir)
        newest = recent[0] if recent else None
        self.img_last_lbl.config(text=("最近：" + os.path.basename(newest)) if newest else "最近：（无）")
        tw, th = self._thumb_size
        self._thumb_photos = []
        self.img_grid.delete("all")
        self._thumb_items = []
        for i, p in enumerate(recent):
            ph = self._make_thumb(p, tw, th)
            if ph is None:
                continue
            x = 6 + (i % self._thumb_cols) * (tw + 8)
            y = 6 + (i // self._thumb_cols) * (th + 8)
            self.img_grid.create_rectangle(x, y, x + tw, y + th, outline="#c3cbd8", width=1)
            self.img_grid.create_image(x + 2, y + 2, anchor="nw", image=ph)
            self._thumb_items.append((x, y, tw, th, p))
        bbox = self.img_grid.bbox("all")
        if bbox:
            self.img_grid.configure(scrollregion=(0, 0, max(bbox[2] + 20, 260), bbox[3] + 12))
        else:
            self.img_grid.create_text(10, 20, anchor="nw", text="（输出目录暂无图片）", fill="#999")
        self._redraw_thumb_highlight()   # 刷新后保留选中高亮
        self._log(f"[图片] 预览刷新: {len(recent)} 张（最近 {os.path.basename(newest) if newest else '空'}）")

    def _make_thumb(self, path, w, h):
        # PIL 优先（任意格式）；无 PIL（Windows 精简构建）→ tk.PhotoImage 原生 png/gif + subsample
        try:
            from PIL import Image, ImageTk
            im = Image.open(path).convert("RGB")
            im.thumbnail((w, h))
            ph = ImageTk.PhotoImage(im)
            self._thumb_photos.append(ph)
            return ph
        except Exception:
            pass
        if path.lower().endswith((".png", ".gif")):
            try:
                ph = tk.PhotoImage(file=path)
                pw, phh = ph.width(), ph.height()
                if pw > w or phh > h:
                    scale = max(pw / w, phh / h)
                    f = 1
                    while f * 2 < scale:
                        f *= 2
                    if f > 1:
                        ph = ph.subsample(f, f)
                self._thumb_photos.append(ph)
                return ph
            except Exception:
                pass
        return None

    def _on_thumb_click(self, ev):
        """单击缩略图 → 选中（供 删除/另存/查看原图）。"""
        for (x, y, w, h, p) in self._thumb_items:
            if x <= ev.x < x + w and y <= ev.y < y + h:
                self._select_thumb(p)
                break

    def _on_thumb_dclick(self, ev):
        """双击缩略图 → 查看原图。"""
        for (x, y, w, h, p) in self._thumb_items:
            if x <= ev.x < x + w and y <= ev.y < y + h:
                self._open_full(p)
                break

    def _select_thumb(self, path):
        self._img_selected = path
        self.img_sel_lbl.config(text="选中：" + os.path.basename(path), foreground="#1a7f37")
        self._redraw_thumb_highlight()

    def _redraw_thumb_highlight(self):
        self.img_grid.delete("sel")
        for (x, y, w, h, p) in self._thumb_items:
            if p == getattr(self, "_img_selected", None):
                self.img_grid.create_rectangle(x, y, x + w, y + h, outline="#ff5252",
                                               width=3, tags="sel")

    def _img_view_selected(self):
        if self._img_selected:
            self._open_full(self._img_selected)

    def _img_delete_selected(self):
        p = self._img_selected
        if not p or not os.path.isfile(p):
            messagebox.showinfo("删除", "未选中图片（先点一张缩略图）。")
            return
        if messagebox.askyesno("删除", f"删除 {os.path.basename(p)}？（不可恢复）"):
            try:
                os.remove(p)
                self._img_selected = None
                self.img_sel_lbl.config(text="选中：（未选）", foreground="#888")
                self._refresh_img_preview()
                self._log(f"[图片] 已删除 {os.path.basename(p)}")
            except OSError as e:
                messagebox.showerror("删除失败", str(e))

    def _img_save_selected(self):
        p = self._img_selected
        if not p or not os.path.isfile(p):
            messagebox.showinfo("另存", "未选中图片（先点一张缩略图）。")
            return
        dest = filedialog.asksaveasfilename(
            parent=self.root, title="另存为",
            initialfile=os.path.basename(p),
            filetypes=(("PNG", "*.png"), ("JPEG", "*.jpg"), ("WebP", "*.webp"), ("所有文件", "*.*")))
        if dest:
            try:
                import shutil
                shutil.copyfile(p, dest)
                self._log(f"[图片] 已另存 → {dest}")
            except OSError as e:
                messagebox.showerror("另存失败", str(e))

    def _update_img_progress(self, cur, tot, note=None):
        """出图进度：Progressbar(cur/tot) + “出图：X/N 步”标签。"""
        try:
            self.img_prog.configure(maximum=max(1, tot or 1), value=(cur or 0))
        except tk.TclError:
            pass
        if note:
            self.img_prog_lbl.config(text=f"出图：{note}")
        elif tot:
            self.img_prog_lbl.config(text=f"出图：{cur}/{tot} 步")
        else:
            self.img_prog_lbl.config(text="出图：就绪")

    def _open_full(self, path):
        win = tk.Toplevel(self.root)
        win.title(os.path.basename(path))
        try:
            from PIL import Image, ImageTk
            im = Image.open(path).convert("RGB")
            ow, oh = im.width, im.height
            # 按比例缩进 1100×760 框内（不放大、不裁切）——修 1:1 图被“独立 cap 宽/高”只显上半截
            scale = min(1100 / ow, 760 / oh, 1.0)
            cw, ch = max(1, int(ow * scale)), max(1, int(oh * scale))
            if (cw, ch) != (ow, oh):
                im = im.resize((cw, ch), Image.LANCZOS)
            ph = ImageTk.PhotoImage(im)
            self._full_photo = ph          # 防 GC
            c = tk.Canvas(win, width=cw, height=ch, highlightthickness=0, bg="#222")
            c.create_image(0, 0, anchor="nw", image=ph)
            c.pack()
            win.geometry(f"{cw}x{ch + 24}")
            self._log(f"[图片] 原图查看: {os.path.basename(path)}（显示 {cw}×{ch} / 原 {ow}×{oh}）")
        except Exception:
            # 无 PIL / 格式不支持：Windows 用系统查看器（os.startfile），macOS 给提示
            if IS_WIN:
                try:
                    os.startfile(path)
                    self._log(f"[图片] 原图查看: {os.path.basename(path)}（系统查看器）")
                    return
                except Exception as e2:
                    ttk.Label(win, text=f"打开原图失败: {e2}").pack(padx=10, pady=10)
            else:
                ttk.Label(win, text=f"打开原图失败（需 PIL）: 请装 pillow").pack(padx=10, pady=10)

    def _browse_img_engine(self):
        p = filedialog.askopenfilename(parent=self.root, title="选择文生图引擎 (sd-cli)")
        if p:
            self.img_engine_var.set(p)

    def _apply_img_engbar(self):
        cfg = _load_config()
        cfg["image_bin"] = self.img_engine_var.get().strip()
        cfg["reserve_bin"] = self.img_reserve_var.get().strip()
        _save_config(cfg)
        self._log(f"[图片] 引擎路径已应用: 图引擎={self.img_engine_var.get() or '(默认 sd-cli)'}")

    def _browse_img_out(self):
        d = filedialog.askdirectory(parent=self.root, title="选择图片输出目录",
                                    initialdir=(self.img_out_var.get() or self.img_out_dir))
        if d:
            self.img_out_var.set(d)
            self._refresh_recommend()

    def _open_img_out(self):
        d = (self.img_out_var.get().strip() if hasattr(self, "img_out_var") else self.img_out_dir)
        try:
            if IS_WIN:
                os.startfile(d) if hasattr(os, "startfile") else \
                    subprocess.Popen(["explorer", d], creationflags=0x08000000)
            else:
                subprocess.Popen(["open", d])
        except Exception as e:
            self._log(f"[图片] 打开目录失败: {e}")

    def _refresh_hw_bar(self):
        hw = self.hw
        v_ok = os.path.exists(VANILLA_ABS)
        ik_ok = os.path.exists(IK_ABS)
        t_ok = os.path.exists(TOSH_ABS)
        if IS_WIN:
            gpus = ", ".join(f"{g['name']} {g.get('vram_gb', 0):.0f}G"
                             for g in hw.gpus if not g.get("integrated")) or "无独显（纯 CPU）"
            self.hw_lbl.config(
                text=f"CPU {hw.physical_cores}C/{hw.logical_cores}T · RAM {hw.ram_gb:.0f}GiB · "
                     f"GPU {gpus}   |   "
                     f"引擎 vanilla={'✓' if v_ok else '✗缺失'}  ik={'✓' if ik_ok else '✗缺失'}")
        else:
            self.hw_lbl.config(
                text=f"{hw.cpu_brand} · {hw.ram_gb:.0f} GiB · "
                     f"{hw.physical_cores}C/{hw.logical_cores}T · "
                     f"Metal 计算={'是' if hw.metal_compute else '否（显存仅显示）'}   |   "
                     f"引擎 vanilla={'✓' if v_ok else '✗缺失'}  tosh={'✓' if t_ok else '✗缺失'}  ik={'✓' if ik_ok else '✗缺失'}")

    def _set_run_ui(self, running: bool):
        """统一重绘四个按钮：运行中→运行钮绿底“● 正在运行”、停止钮红底；
        就绪(ready)→运行钮/生成钮绿色；空闲→灰色。"""
        self._refresh_btns()

    # ---------------- Windows GPU 统计（性能计数器，0.1 验证过的采样器） ----------------
    def _win_amd_gpu_stats(self):
        """返回 (util_pct, note) 或 (None, None)。
        AMD：Get-Counter GPU Engine(*)/Utilization + GPU Process Memory(*)/Dedicated，
        选 VRAM 占用最大的 LUID 作 dGPU（排除 iGPU LUID 0x0000f59f）；NVIDIA：nvidia-smi。"""
        try:
            ps = (
                "$e = Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -SampleInterval 1 -MaxSamples 1;"
                "$m = Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage' -SampleInterval 1 -MaxSamples 1;"
                "function Luid($i){ if($i -match 'luid_0x[0-9a-fA-F]+_(0x[0-9a-fA-F]+)'){ $Matches[1] } else { '' } };"
                "$byLuid = @{};"
                "foreach($s in $m[0].CounterSamples){"
                "  $l = Luid $s.InstanceName;"
                "  if($l -and $l -ne '0x0000f59f'){ if(-not $byLuid.ContainsKey($l)){$byLuid[$l]=0}; $byLuid[$l] += [int64]$s.CookedValue }"
                "};"
                "$dLuid=''; $mx=-1;"
                "foreach($k in $byLuid.Keys){ if([int64]$byLuid[$k] -gt $mx){ $mx=[int64]$byLuid[$k]; $dLuid=$k } };"
                "$util=0.0;"
                "if($dLuid){"
                "  foreach($s in $e[0].CounterSamples){"
                "    if((Luid $s.InstanceName) -eq $dLuid){"
                "      if([double]$s.CookedValue -gt $util){ $util=[double]$s.CookedValue }"
                "    }"
                "  }"
                "};"
                "$used=0; if($dLuid -and $byLuid.ContainsKey($dLuid)){ $used=[int64]$byLuid[$dLuid] };"
                "[pscustomobject]@{ dLuid=$dLuid; util=([math]::Round($util,1)); usedBytes=$used } | ConvertTo-Json -Compress")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5,
                creationflags=0x08000000   # CREATE_NO_WINDOW：不弹黑框
            ).stdout.strip()
            import json as _json
            data = _json.loads(out)
            dluid = data.get("dLuid") or None
            util = float(data.get("util", 0) or 0)
            used_b = int(data.get("usedBytes", 0) or 0)
            if dluid:
                total_gb = self.hw.vram_gb
                return util, f"VRAM {used_b / (1024**3):.1f}/{total_gb:.0f} GiB | GPU {int(util)}% (AMD)"
        except Exception:
            pass
        # NVIDIA 兜底
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
                creationflags=0x08000000).stdout.strip()
            if out:
                u, t, v = [x.strip() for x in out.splitlines()[0].split(",")]
                return float(v), f"VRAM {int(u) / 1024:.1f}/{int(t) / 1024:.1f} GiB | GPU {int(v)}% (NVIDIA)"
        except Exception:
            pass
        return None, None

    def _gpu_sampler(self):
        """后台线程：每 2s 采样 GPU（AMD 计数器/NVIDIA），结果缓存到 self._gpu_last。
        主线程 _update_hwload 只读缓存，不再阻塞 UI（卡顿根因修复）。"""
        import time as _t
        while True:
            try:
                util, note = self._win_amd_gpu_stats()
                if util is not None:
                    self._gpu_last = (util, note)
            except Exception:
                pass
            _t.sleep(2.0)

    # ---------------- 硬件负载（实时 2s） ----------------
    def _hw_tick(self):
        if getattr(self, "_closing", False):
            return
        try:
            self._update_hwload()
        except Exception:
            pass
        self._hw_tick_id = self.root.after(2000, self._hw_tick)

    def _draw_bar(self, cv, pct, note=""):
        """Canvas 条状图：灰底 + 彩色填充（<60% 蓝 / <85% 黄 / 否则红）；pct=None 时画注。"""
        cv.delete("all")
        w = max(50, cv.winfo_width())
        h = 12
        cv.create_rectangle(0, 0, w, h, fill="#e8e9ee", outline="#c7c9d1")
        if pct is None:
            cv.create_text(w / 2, h / 2, text=note or "—", fill="#888",
                           font=("Helvetica", 9))
            return
        color = ("#4a90d9" if pct < 60 else
                 "#e6a817" if pct < 85 else "#d64545")
        cv.create_rectangle(0, 0, max(2, int(w * min(100, pct) / 100)), h,
                            fill=color, outline="")

    def _update_hwload(self):
        """2s 硬件负载：CPU/内存（psutil 非阻塞）+ GPU（Windows=后台采样线程缓存 / macOS=ioreg）。
        主线程只读缓存、不跑 1.5-3s 的 powershell —— Windows 卡顿根因修复（0.1 实测）。"""
        (cv_c, pct_c), (cv_m, pct_m), (cv_g, pct_g) = (
            self.hwload_rows["CPU"], self.hwload_rows["内存"], self.hwload_rows["GPU"])
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        gpu_pct = None
        gpu_note = ""
        if IS_WIN:
            # 读后台采样线程缓存（_gpu_sampler：AMD 性能计数器 / nvidia-smi）
            total = self.hw.vram_gb
            if total > 0.2:
                if self._gpu_last is not None:
                    gpu_pct, gpu_note = self._gpu_last
                else:
                    gpu_note = f"VRAM {total:.0f} GiB（采集中…）"
            else:
                gpu_note = "无独显（计算全在 CPU）"
        elif self.hw.metal_compute:
            gpu_note = "powermetrics 需 sudo，未采集利用率"
        else:
            gpu_note = "无 Metal 计算设备（计算全在 CPU）"
        if not IS_WIN:
            try:
                out = subprocess.run(["ioreg", "-r", "-k", "PerformanceStatistics"],
                                     capture_output=True, text=True, timeout=3).stdout
                import re
                dev_util = gpu_act = vram_used = vram_free = temp = power = None
                for ln in out.splitlines():
                    m = re.search(r'"Device Utilization %"\s*=\s*(\d+)', ln)      # AMD 真实计算利用率
                    if m:
                        dev_util = int(m.group(1))
                    m = re.search(r'"GPU Activity\(%\)"\s*=\s*(\d+)', ln)      # 旧 iGPU key（AMD 上恒 0，仅兜底）
                    if m:
                        gpu_act = int(m.group(1))
                    m = re.search(r'"inUseVidMemoryBytes"\s*=\s*(\d+)', ln)
                    if m:
                        vram_used = int(m.group(1))
                    m = re.search(r'"vramFreeBytes"\s*=\s*(\d+)', ln)
                    if m:
                        vram_free = int(m.group(1))
                    m = re.search(r'"Temperature\(C\)"\s*=\s*(\d+)', ln)
                    if m:
                        temp = int(m.group(1))
                    m = re.search(r'"Total Power\(W\)"\s*=\s*(\d+)', ln)
                    if m:
                        power = int(m.group(1))
                # GPU 计算负载条 = 真实利用率（Device Utilization % 优先，GPU Activity 兜底）；
                # 显存/温度/功率 只进注释。旧 bug：AMD 上 "GPU Activity(%)" 恒 0 → 条恒 0，或误用 VRAM 占比冒充。
                gpu_pct = dev_util if dev_util is not None else gpu_act
                parts = []
                if vram_used is not None and vram_free is not None:
                    parts.append("VRAM %.1f/%.1f GiB" % (vram_used / 1073741824,
                                                         (vram_used + vram_free) / 1073741824))
                if temp is not None:
                    parts.append("%d°C" % temp)
                if power is not None:
                    parts.append("%dW" % power)
                gpu_note = (" | ".join(parts)) if parts else "powermetrics 需 sudo（利用率未取）"
            except Exception:
                pass
            if gpu_pct is None and not self.hw.metal_compute:
                try:
                    out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                         capture_output=True, text=True, timeout=5).stdout
                    import re
                    m = re.search(r"VRAM \(.*?\):\s*(\d+)\s*(MB|GB)", out, re.IGNORECASE)
                    if m:
                        val, unit = int(m.group(1)), m.group(2).upper()
                        total_gb = val / 1024 if unit == "MB" else val
                        gpu_note = f"无 Metal 计算设备 | VRAM {total_gb:.0f} GiB（计算全在 CPU）"
                except Exception:
                    pass
        self._draw_bar(cv_c, cpu)
        self._draw_bar(cv_m, mem)
        self._draw_bar(cv_g, gpu_pct, gpu_note)
        pct_c.config(text=f"{cpu:.0f}%")
        pct_m.config(text=f"{mem:.0f}%")
        pct_g.config(text=f"{gpu_pct:.0f}%" if gpu_pct is not None else "n/a")

    # ---------------- 模型库 ----------------
    def _scan_async(self):
        self._log(f"扫描模型库 {self.models_dir} …")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _browse_models_dir(self):
        import tkinter.filedialog as fd
        d = fd.askdirectory(initialdir=self.models_dir, title="选择模型库目录")
        if d:
            self.models_dir = d
            self.dir_var.set(d)
            if self.page == "image":
                self._image_models_dir = d
                self._log(f"文生图模型库已切换 → {d}")
                self._scan_image()
                return
            self._text_models_dir = d
            _save_config(_load_config() | {"models_dir": d})
            self._log(f"模型库已切换 → {d}")
            # 刷新 MTP / mmproj 候选
            global MTP_CANDS, MTP_DISPLAY, MTP_MAP, MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP, LORA_CANDS, LORA_DISPLAY, LORA_MAP
            MTP_CANDS, MTP_DISPLAY, MTP_MAP = _build_mtp_cands(d)
            MMPROJ_CANDS, MMPROJ_DISPLAY, MMPROJ_MAP = _build_mmproj_cands(d)
            LORA_CANDS, LORA_DISPLAY, LORA_MAP = _build_lora_cands(d)
            # 更新 Combobox 候选值
            for key, widget in self.widgets.items():
                if key == "spec_draft_model":
                    widget["values"] = ["default", "off"] + MTP_DISPLAY[1:]
                elif key == "mmproj":
                    widget["values"] = ["default"] + MMPROJ_DISPLAY[1:]
                elif key == "lora":
                    widget["values"] = ["default", "off"] + LORA_DISPLAY[1:]
            self._scan_async()

    def _refresh_engbar(self):
        """把配置里的引擎路径回显到 engbar，并保证 Entry 显示「实际生效的路径」。
        规则：配置 vanilla_bin 为空 → 显示自动探测结果（which llama-server → 本地兜底），
        并加提示「(自动)」让主人一眼看出当前用的不是固定路径。"""
        import shutil
        cfg = _load_config()
        v_cfg = cfg.get("vanilla_bin", "").strip()
        t_cfg = cfg.get("tosh_bin", "").strip()
        if v_cfg:
            self.vanilla_var.set(v_cfg)
        else:
            # 空配置 = 自动探测，把探测结果填进输入框让主人可见
            found = shutil.which("llama-server") or os.path.abspath(VANILLA_BIN_FALLBACK)
            self.vanilla_var.set(found + "  (自动探测: 配置为空)")
        if t_cfg:
            self.tosh_var.set(t_cfg)
        else:
            self.tosh_var.set(TOSH_BIN_DEFAULT + "  (默认路径: 配置为空)")
        i_cfg = cfg.get("ik_bin", "").strip()
        if i_cfg:
            self.ik_var.set(i_cfg)
        else:
            self.ik_var.set(IK_BIN_DEFAULT + "  (默认路径: 配置为空)")
        global VANILLA_ABS, TOSH_ABS, IK_ABS
        VANILLA_ABS = v_cfg or os.path.abspath(shutil.which("llama-server") or VANILLA_BIN_FALLBACK)
        TOSH_ABS = t_cfg or TOSH_BIN_DEFAULT
        IK_ABS = i_cfg or IK_BIN_DEFAULT
        # 同步 recommend 模块里 import 时冻结的 VANILLA_BIN/TOSH_BIN/IK_BIN（binary_for 读模块全局）
        # → App 内升级/改引擎路径后，不必重启 app，pick_engine 即用新路径
        import recommend as _rec
        _rec.VANILLA_BIN = VANILLA_ABS
        _rec.TOSH_BIN = TOSH_ABS
        _rec.IK_BIN = IK_ABS

    def _browse_engine(self, which: str):
        import tkinter.filedialog as fd
        global VANILLA_ABS, TOSH_ABS, IK_ABS
        _v = {"vanilla": self.vanilla_var, "tosh": self.tosh_var, "ik": self.ik_var}.get(which)
        if _v is None:
            return
        p = fd.askopenfilename(initialfile=_v.get(), title=f"选择 {which} 引擎可执行文件")
        if not p:
            return
        _v.set(p)
        cfg = _load_config()
        cfg[which + "_bin"] = p
        _save_config(cfg)
        if which == "vanilla":
            VANILLA_ABS = p
        elif which == "tosh":
            TOSH_ABS = p
        else:
            IK_ABS = p
        self._refresh_hw_bar()
        self._refresh_engbar()
        self._log(f"引擎路径已更新: {which} → {p}")

    def _apply_engbar(self):
        """engbar「应用」按钮：把三个输入框（vanilla/tosh/ik）的文本写进配置并立即生效。
        留空 = 恢复自动探测/默认。输入带「(自动探测)」后缀的自动回显文本时自动剥离。"""
        global VANILLA_ABS, TOSH_ABS, IK_ABS
        import shutil
        cfg = _load_config()
        # vanilla：留空 = 自动探测（which llama-server → 本地 bin 兜底）
        v_in = re.sub(r"\s*\(自动探测: 配置为空\)$", "", self.vanilla_var.get().strip()).strip()
        if v_in:
            v_in = os.path.abspath(v_in)
            if not os.path.exists(v_in):
                self._log(f"[警告] vanilla 引擎路径不存在: {v_in}")
            cfg["vanilla_bin"] = v_in; VANILLA_ABS = v_in
        else:
            cfg.pop("vanilla_bin", None)
            VANILLA_ABS = os.path.abspath(shutil.which("llama-server") or VANILLA_BIN_FALLBACK)
        # tosh：留空 = 默认 ToshLLM 路径
        t_in = re.sub(r"\s*\(默认路径: 配置为空\)$", "", self.tosh_var.get().strip()).strip()
        if t_in:
            t_in = os.path.abspath(t_in)
            if not os.path.exists(t_in):
                self._log(f"[警告] tosh 引擎路径不存在: {t_in}")
            cfg["tosh_bin"] = t_in; TOSH_ABS = t_in
        else:
            cfg.pop("tosh_bin", None); TOSH_ABS = TOSH_BIN_DEFAULT
        # ik：留空 = 默认 ik 路径
        i_in = re.sub(r"\s*\(默认路径: 配置为空\)$", "", self.ik_var.get().strip()).strip()
        if i_in:
            i_in = os.path.abspath(i_in)
            if not os.path.exists(i_in):
                self._log(f"[警告] ik 引擎路径不存在: {i_in}")
            cfg["ik_bin"] = i_in; IK_ABS = i_in
        else:
            cfg.pop("ik_bin", None); IK_ABS = IK_BIN_DEFAULT
        _save_config(cfg)
        self._refresh_hw_bar()
        self._log("引擎路径已应用" + ("（空 = 自动探测/默认）" if not (v_in or t_in or i_in) else ""))

    # ---------------- llama.cpp 联网升级（11-19） ----------------
    def _on_upgrade_click(self):
        """「检查并升级」按钮：后台查 GitHub（不卡 UI），有新版本再弹选择框。"""
        if self._upgrading:
            self._log("[升级] 升级进行中，请稍候")
            return
        self._upgrading = True
        self.upg_btn.config(state="disabled")
        threading.Thread(target=self._upgrade_check_worker, daemon=True).start()

    def _upgrade_check_worker(self):
        """线程：查最新/稳定/本地 → 已最新则收尾，否则入队 __UPG_ASK__ 让主线程弹选择框。
        本线程不碰任何 widget，只经 self.q（pump 处理）。"""
        try:
            self.q.put("[升级] 正在查询 GitHub llama.cpp 最新版本…")
            info = upgrade_check(progress=self.q.put)
        except Exception as e:
            self.q.put("__UPG_FAIL__:" + str(e))
            return
        cur, lat, st = info["cur_num"], info["latest_num"], info["stable_num"]
        seg = [f"本地 b{cur}" if cur else "本地未知"]
        seg.append(f"稳定 b{st}") if st else None
        seg.append(f"最新 b{lat}")
        note = f"（{info['cur_src']}）" if (cur and info["cur_src"] and "bin/llama-b" not in info["cur_src"]) else ""
        self.q.put("[升级] " + " | ".join(seg) + note)
        if cur is not None and cur >= lat:
            self.q.put("__UPG_INFO__:" + json.dumps(info, ensure_ascii=False))
            self.q.put("[升级] 本地已是最新版本，无需下载")
            return
        self.q.put("__UPG_ASK__:" + json.dumps(info, ensure_ascii=False))

    def _upg_label(self, info: dict):
        """版本信息（11-23 升级行取消后）：写入日志 [升级] 行，不再写行内标签。
        11-27：稳定/最新带资产 variant（win-vulkan-x64 / win-cpu-x64，装的是 GPU 还是 CPU 引擎一目了然）。"""
        seg = []
        seg.append(f"本地 b{info['cur_num']}" if info.get("cur_num") else "本地未知")
        if info.get("stable_num"):
            seg.append(f"稳定 b{info['stable_num']}" + (f"（{info['stable_variant']}）" if info.get("stable_variant") else ""))
        seg.append(f"最新 b{info['latest_num']}" + (f"（{info['latest_variant']}）" if info.get("latest_variant") else ""))
        self._log("[升级] " + " · ".join(seg))

    def _ask_upgrade(self, info: dict):
        """主线程：选版本安装（最新；稳定高于本地时才给）。取消 = __UPG_FAIL__ 收尾。"""
        lat, st, cur = info["latest_num"], info.get("stable_num"), info.get("cur_num")
        win = tk.Toplevel(self.root)
        win.title("llama.cpp 联网升级")
        win.transient(self.root)
        try:
            win.geometry(f"+{self.root.winfo_x() + 320}+{self.root.winfo_y() + 160}")
        except Exception:
            pass
        ttk.Label(win, text=f"本地当前: b{cur}" + (f"（{info['cur_src']}）" if info.get("cur_src") else "")
                  + "\n选择要下载安装的版本:",
                  font=(MONO, 11), anchor="w", justify="left").pack(fill="x", padx=12, pady=(10, 4))
        def pick(num, url):
            win.destroy()
            self._start_download(num, url)
        if info.get("latest_url"):
            ttk.Button(win, text=f"下载并安装 最新 b{lat}（{info.get('latest_variant') or '默认'}，推荐）",
                       command=lambda: pick(lat, info["latest_url"])).pack(fill="x", padx=12, pady=2)
        if st and st != lat and st > (cur or 0) and info.get("stable_url"):
            ttk.Button(win, text=f"安装 稳定 b{st}（{info.get('stable_variant') or '默认'}）",
                       command=lambda: pick(st, info["stable_url"])).pack(fill="x", padx=12, pady=2)
        def cancel():
            win.destroy()
            self.q.put("__UPG_FAIL__:用户取消升级")
        ttk.Button(win, text="取消", command=cancel).pack(fill="x", padx=12, pady=(6, 10))
        try:
            win.grab_set()          # 窗口不可见/无头场景 grab 可能 TclError，不可连累 pump
        except Exception:
            pass
        win.focus_force()

    def _start_download(self, num: int, url: str):
        threading.Thread(target=self._upgrade_download_worker, args=(num, url), daemon=True).start()

    def _upgrade_download_worker(self, num: int, url: str):
        """线程：下载 → 解包 → 校验 --version → 轮次删旧版 → __UPG_DONE__。
        落盘路径（11-23 主人定规）：Windows = D:\\llama-vulkan-x64-bNNNN（版本化目录，
        保留最近 2 个滚动删；不再用 BASE/bin/.dl）；macOS 保持 bin/llama-bNNNN。
        全程不碰 widget；配置写回/engbar 刷新留给主线程 __UPG_DONE__ 处理。"""
        import tarfile, zipfile, shutil as _sh
        os_name, _arch = _plat_arch()
        fname = url.rsplit("/", 1)[-1]
        if os.name == "nt":
            vdir = f"{WIN_VULKAN_ROOT}-b{num}"           # D:\llama-vulkan-x64-bNNNN
            tmpdir = os.path.join(vdir, ".dl")
        else:
            vdir = None
            tmpdir = os.path.join(BASE, "bin", ".dl")
        os.makedirs(tmpdir, exist_ok=True)
        fpath = os.path.join(tmpdir, fname)
        self.q.put(f"[升级] 下载 {fname} → {vdir or os.path.join(BASE, 'bin')} …")
        try:
            def prog(pct, got, total):
                self.q.put(f"[升级] 下载 {pct}%（{got / 1048576:.1f}/{total / 1048576:.1f} MB）")
            _http_download(url, fpath, prog)
        except Exception as e:
            try:
                os.remove(fpath)
            except OSError:
                pass
            self.q.put(f"__UPG_FAIL__:下载失败: {e}")
            return
        # 解包：Windows 直落版本化目录（zip 顶层若带 llama-bNNNN/ 则拍平，保证
        # D:\llama-vulkan-x64-bNNNN\llama-server.exe 直接可用）；macOS 解到 bin/
        # （filter="data" 阻断路径穿越且保留 dylib 相对 symlink；py<3.12 无 filter → 回退裸解包）
        dest = vdir if os.name == "nt" else os.path.join(BASE, "bin")
        try:
            if fpath.endswith(".zip"):
                with zipfile.ZipFile(fpath) as z:
                    z.extractall(dest)
            else:
                with tarfile.open(fpath, "r:gz") as t:
                    try:
                        t.extractall(dest, filter="data")
                    except TypeError:
                        t.extractall(dest)
            if os.name == "nt":
                nested = os.path.join(dest, f"llama-b{num}")
                if os.path.isdir(nested):
                    for fn in os.listdir(nested):
                        os.replace(os.path.join(nested, fn), os.path.join(dest, fn))
                    os.rmdir(nested)
        except Exception as e:
            self.q.put(f"__UPG_FAIL__:解包失败: {e}")
            return
        srv = os.path.join(dest, _LLAMA_SRV) if os.name == "nt" \
            else os.path.join(dest, f"llama-b{num}", _LLAMA_SRV)
        if not os.path.isfile(srv):
            self.q.put(f"__UPG_FAIL__:解包结果缺 {srv}（旧版未动，可重试）")
            return
        for fn in os.listdir(os.path.dirname(srv)):
            p2 = os.path.join(os.path.dirname(srv), fn)
            if os.path.isfile(p2) and not os.path.islink(p2) and fn.startswith("llama-"):
                os.chmod(p2, 0o755)
        if os_name == "darwin":
            # 剥离隔离属性（urllib 下载本不带 quarantine，浏览器代下的文件才带；失败无害）
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", os.path.dirname(srv)],
                           capture_output=True)
        # 校验新版可运行
        try:
            vout = subprocess.run([srv, "--version"], capture_output=True, text=True, timeout=15)
            vline = (vout.stdout or "").strip().splitlines()
            self.q.put(f"[升级] 新版校验: {vline[0] if vline else 'ok'}")
        except Exception as e:
            self.q.put(f"__UPG_FAIL__:新版运行校验失败: {e}（旧版未动）")
            return
        # 轮次删除旧版（11-23 主人定规）：Windows = D:\llama-vulkan-x64-bNNNN 保留最近 2 个；
        # macOS = bin/llama-bNNNN 保留 新 + 最近 1 旧；正在运行的引擎所在目录不删
        running_bin = self._running_bin if (self.proc is not None and self.proc.poll() is None) else None
        if os.name == "nt":
            dropped = rotate_win_vulkan_builds(num, running_bin=running_bin)
        else:
            dropped = rotate_old_builds(num, running_bin=running_bin)
        for p in dropped:
            self.q.put(f"[升级] 轮次删除旧版: {p}")
        try:
            os.remove(fpath)
            _sh.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass
        self.q.put("__UPG_DONE__:" + json.dumps({"num": num, "path": srv, "dropped": dropped},
                                                ensure_ascii=False))

    def _scan_worker(self):
        try:
            models = scan_models(self.models_dir)
        except Exception as e:
            self.q.put(f"[错误] 扫描失败: {e}")
            return
        for st in models:
            self.st_cache[st["path"]] = st
        # 传完整 st 字典（含 layers/active_b），_fill_tree 各列取数不再缺键
        self.q.put("__SCAN_DONE__" + json.dumps(models))

    def _on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        pid = self.tree.item(sel[0], "values")[-1]  # 最后列存 path
        if self.page == "image":
            self._img_model = self.img_cache.get(pid)
            self.selected = None
            self._img_prefill_from_model(self._img_model)
            self._refresh_recommend()
            return
        st = self.st_cache.get(pid)
        if not st:
            return
        self.selected = st
        key = st.get("path")
        saved = (self._load_state().get(key) or {}).get("params") or {}
        if saved:
            # 该模型有用户上次保存的面板值 → 恢复
            self._restore_model_state()
        else:
            # 无历史 → 推荐引擎按本模型算出“尽量装显存/最大 ctx”的预填值写入面板
            self._apply_prefill()
        self._refresh_recommend()

    def _fill_tree(self, models):
        if self.page != "text":
            return                       # 跨页竞态：非文字页时丢弃文字扫描回填（避免图片页被填成文字模型）
        self.tree.delete(*self.tree.get_children())
        self._sort_vals = {}
        for st in models:
            # 类型：MoE(激活B) / dense，叠加 MTP（内嵌 MTP 头 mtp_layers>0）
            parts = [f"MoE{st.get('active_b',0):.0f}B" if st.get("is_moe") else "dense"]
            if st.get("mtp_layers"):
                parts.append("MTP")
            kind = "·".join(parts)
            b = st.get("total_b", 0)
            layers = st.get("layers", 0)
            fname = os.path.basename(st.get("path", "") or st.get("name", "?")).removesuffix(".gguf")
            iid = self.tree.insert("", "end",
                                    values=(fname,
                                            f"{b:.0f}" if b else "-",
                                            kind,
                                            f"{st.get('file_gb',0):.1f}",
                                            str(layers) if layers else "-",
                                            st.get("path")))
            self._sort_vals[iid] = {"name": fname,
                                     "b": float(b or 0),
                                     "kind": kind,
                                     "gb": float(st.get("file_gb", 0) or 0),
                                     "layer": int(layers or 0)}
        # 沿用上一次排序状态
        if self._sort_col:
            self._sort_tree()
        self._calibrate_tree_font()

    def _on_win_configure(self, _e=None):
        """窗口 <Configure>（拉伸/移屏/DPI 上下文变化）→ 防抖 300ms 后全量重绘树，
        清除 Tk8.6 PMDv2 缩放/移屏后的文字残影（"堆叠"观感的实机成因）。"""
        if self._tree_redraw_id is not None:
            try:
                self.root.after_cancel(self._tree_redraw_id)
            except Exception:
                pass
        self._tree_redraw_id = self.root.after(300, self._do_tree_repaint)

    def _do_tree_repaint(self):
        self._tree_redraw_id = None
        if getattr(self, "_closing", False) or self.page != "text":
            return
        sel = list(self.tree.selection())
        kids = [(iid, self.tree.item(iid, "values")) for iid in self.tree.get_children()]
        if not kids:
            return
        self.tree.delete(*[k[0] for k in kids])
        for iid, vals in kids:
            self.tree.insert("", "end", iid=iid, values=list(vals))
        for s_ in sel:
            if s_ in dict(kids):
                self.tree.selection_add(s_)

    def _on_heading_click(self, col):
        """点击表头：同列切换升/降；换列时数值列(B/GB)默认降序（大在前）。"""
        if col == self._sort_col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = col in ("b", "gb")
        self._sort_tree()

    def _sort_tree(self):
        children = self.tree.get_children("")
        if not children:
            return
        col, rev = self._sort_col, self._sort_reverse
        vals = self._sort_vals
        ordered = sorted(children,
                         key=lambda it: vals.get(it, {}).get(col, 0) if not isinstance(
                             vals.get(it, {}).get(col, 0), str)
                         else str(vals.get(it, {}).get(col, 0)).casefold(),
                         reverse=rev)
        for i, child in enumerate(ordered):
            cur = self.tree.index(child)
            if cur != i:
                # tkinter move(item, parent, index) —— 先 item 再 parent，旧版反了
                self.tree.move(child, "", i)
        for c, base in self._heading_base.items():
            if c == col:
                self.tree.heading(c, text=base + (" ▼" if rev else " ▲"))
            else:
                self.tree.heading(c, text=base)

    # ---------------- 推荐 / 命令 ----------------
    def _resolve_engine(self):
        """返回 (engine, binary, notes)。engine_var: auto/vanilla/tos。"""
        if not self.selected:
            return "vanilla", VANILLA_ABS, ("（未选模型）",)
        return recommend.pick_engine(self.selected, self.hw,
                                      forced=self.engine_var.get())

    def _params(self) -> dict:
        p = {k: v.get() for k, v in self.pv.items()}
        # mmproj: 将下拉显示名映射回全路径；若用户手输了全路径则直接透传
        mm = p.get("mmproj", "")
        if mm in MMPROJ_MAP:
            p["mmproj"] = MMPROJ_MAP[mm]
        # 若 mm 不在 MAP 中（用户手输），保持原值（全路径或自定义路径）
        # spec_draft_model: 同 mmproj 逻辑——显示名映射回全路径；default/off→""（FLAG_MAP 守卫省略旗标）
        sm = p.get("spec_draft_model", "")
        if sm in MTP_MAP:
            p["spec_draft_model"] = MTP_MAP[sm]
        # lora: 同 spec_draft_model——显示名映射回全路径；default/off→""（FLAG_MAP 守卫省略旗标）
        lm = p.get("lora", "")
        if lm in LORA_MAP:
            p["lora"] = LORA_MAP[lm]
        p["dynamic_moe"] = "on" if self.dm_var.get() else "off"
        return p

    def _on_ovr_edit(self, _e=None):
        self._refresh_recommend()
        self._save_state_entry()

    # ---------------- 按模型持久化（参数面板/自定义覆盖/引擎） ----------------
    def _state_key(self):
        return self.selected.get("path") if self.selected else None

    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state_entry(self):
        """把当前面板/覆盖/引擎按模型路径写回 STATE_FILE；恢复期间与未选模型时跳过。"""
        key = self._state_key()
        if not key or self._restoring:
            return
        # 保存时 mmproj / spec_draft_model 存显示名（用于 Combobox 回显），其余存实际值
        params_to_save = {k: v.get() for k, v in self.pv.items()}
        data = self._load_state()
        # 自定义覆盖（ovr）是会话级工作区，不持久化（切模型/重开自动清空，防旧参数误伤新模型）
        data[key] = {"params": params_to_save,
                     "engine": self.engine_var.get(),
                     "port": self.port_var.get(),
                     "host": self.host_var.get(),
                     "clear_mode": self.clear_mode_var.get() if hasattr(self, "clear_mode_var") else "override"}
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass   # 持久化失败不阻塞使用

    def _restore_model_state(self):
        """切到某模型时恢复它上次保存的参数；无记录则保持面板当前值。"""
        key = self._state_key()
        if not key:
            return
        entry = self._load_state().get(key) or {}
        saved = entry.get("params") or {}
        # 迁移：旧版单键 cache_type → 拆分后的 K/V 两键（缺省时各按默认）
        if "cache_type" in saved and "cache_type_k" not in saved:
            saved = dict(saved)
            saved.setdefault("cache_type_k", saved["cache_type"])
            saved.setdefault("cache_type_v", saved["cache_type"])
        self._restoring = True
        try:
            for k, v in saved.items():
                if k in self.pv:
                    try:
                        # mmproj: 若存的是全路径，转为显示名
                        if k == "mmproj" and v in MMPROJ_MAP.values():
                            for disp, full in MMPROJ_MAP.items():
                                if full == v:
                                    self.pv[k].set(disp)
                                    break
                        # spec_draft_model: 旧版手存过全路径（或预设/导入带全路径）→ 转显示名回显
                        elif k == "spec_draft_model" and v in MTP_MAP.values():
                            for disp, full in MTP_MAP.items():
                                if full == v:
                                    self.pv[k].set(disp)
                                    break
                        # lora: 旧版/预设存过全路径 → 转显示名回显
                        elif k == "lora" and v in LORA_MAP.values():
                            for disp, full in LORA_MAP.items():
                                if full == v:
                                    self.pv[k].set(disp)
                                    break
                        else:
                            self.pv[k].set(str(v))
                    except tk.TclError:
                        pass
            # 自定义覆盖不持久（Windows 规则）：恢复时清空，旧覆盖不误伤新模型
            self.ovr.config(state="normal")
            self.ovr.delete("1.0", "end")
            eng = entry.get("engine")
            if eng in ("ik", "vanilla", "tosh", "auto"):
                if IS_WIN and eng in ("tosh", "auto"):
                    eng = "vanilla"      # tosh 仅 macOS；旧 mac 状态在 Windows 上映射 vanilla
                self.engine_var.set(eng)
            if "dynamic_moe" in saved:
                self.dm_var.set(saved["dynamic_moe"] == "on")
            # 端口/Host/覆盖模式 持久化恢复
            if "port" in entry:
                self.port_var.set(entry["port"])
            if "host" in entry:
                self.host_var.set(entry["host"])
            if "clear_mode" in entry and hasattr(self, "clear_mode_var"):
                self.clear_mode_var.set(entry["clear_mode"])
        finally:
            self._restoring = False

    def _apply_prefill(self):
        """无持久记录时：面板先回到全局默认，再叠加推荐引擎按本模型算出的
        “尽量装显存/最大 ctx”预填值（ngl/ctx/cache_type/fa 等，均可编辑）。
        预填是确定性的，不写回 STATE_FILE（只有用户改动才持久化）。"""
        st = self.selected
        if not st:
            return
        try:
            pre = recommend.prefill(st, self.hw)
        except Exception:
            return
        self._restoring = True
        try:
            # 1) 全部面板项先复位到全局默认，避免残留上一个模型的旧值
            for k in self.pv:
                d = self.param_defaults.get(k)
                if d is None:
                    continue
                try:
                    self.pv[k].set(str(d))
                except tk.TclError:
                    pass
            # 2) 叠加推荐引擎算出的按硬件最优值
            for k, v in pre.items():
                if k in self.pv:
                    try:
                        self.pv[k].set(str(v))
                    except tk.TclError:
                        pass
        finally:
            self._restoring = False

    def _update_ctv_candidates(self):
        """建议#4：ToshLLM 引擎的 cache-type-v 支持 turbo2/3/4（AMD FA 量化），
        按解析后引擎动态增删候选；vanilla 下选过 turbo 值自动回退 default（防 OptionMenu 空白）。"""
        base = ["default", "f16", "q8_0", "q5_1", "q5_0", "q4_0", "iq4_nl"]
        eng = self.engine_var.get()
        if eng == "auto" and self.selected:
            eng = recommend.pick_engine(self.selected, self.hw, forced="auto")[0]
        vals = base + (["turbo2", "turbo3", "turbo4"] if eng == "tosh" else [])
        cur = self.pv["cache_type_v"].get()
        if cur not in vals:
            self.pv["cache_type_v"].set("default")
        w = self.widgets["cache_type_v"]
        if isinstance(w, ttk.Combobox):
            w["values"] = vals
        else:
            # ttk.OptionMenu 无 "values" 选项（直接赋值抛 TclError 中断整条生成链路），
            # 重建下拉菜单项；command 显式写回变量，保证选中即生效。
            menu = w["menu"]  # 下标访问返回 tkinter.Menu 对象（cget 只给路径字符串）
            menu.delete(0, "end")
            var = self.pv["cache_type_v"]
            for v in vals:
                menu.add_command(label=v, command=lambda v=v: var.set(v))

    def _refresh_recommend(self):
        if self.page == "image":
            self._refresh_recommend_image()
            return
        self._update_ctv_candidates()
        st = self.selected
        if not st:
            self.info.config(text="（选择左侧模型以查看推荐参数）", fg="#888")
            self._cmd_available = False
            self._refresh_btns()
            return
        # 自动判 MTP 头：无内嵌头(mtp_layers=0)且未配独立 MTP draft 文件时，选 mtp 会无效/崩 → 降级 off
        if not st.get("mtp_layers") and "spec_type" in self.pv:
            if str(self.pv["spec_type"].get()).strip() == "mtp":
                _dm = str(self.pv["spec_draft_model"].get()).strip() if "spec_draft_model" in self.pv else ""
                if _dm in ("", "default", "off", "auto", "不配置"):
                    self.pv["spec_type"].set("off")
                    self._log(f"⚠ {st.get('name','该模型')} 无内嵌 MTP 头且未配独立 MTP 文件（MTP draft -md）→ mtp 已自动关闭")
                else:
                    self._log(f"ℹ {st.get('name','该模型')} 无内嵌 MTP 头，改用独立 MTP 文件：{os.path.basename(_dm)}（--spec-draft-model）")
        hw = self.hw
        p = self._params()
        try:
            ctx = int(p["ctx"])
        except (ValueError, tk.TclError):
            ctx = 65536
        ncmoe = None
        # 11-5 规范：n-cpu-moe 数字参数已删除；cpu_moe=on（专家全上 CPU）时按满层数估算
        if str(p.get("cpu_moe", "off")).strip() == "on" and st.get("layers"):
            ncmoe = int(st["layers"])
        # K/V 拆分后取 K 侧为代表性量化（缺省时回退旧键 cache_type）
        kv = p.get("cache_type_k") or p.get("cache_type") or "q8_0"
        plan = recommend.recommend(st, hw, ctx=ctx,
                                   kv_type=kv, ncmoe=ncmoe)

        engine, binary, eng_notes = self._resolve_engine()
        eng_lines = list(eng_notes) if isinstance(eng_notes, (list, tuple)) else eng_notes.splitlines()
        if not binary or not os.path.exists(binary):
            self.info.config(
                text=f"推荐引擎 {engine} 的二进制缺失：{binary or '(无)'}\n" + "\n".join(eng_lines),
                fg="#c0392b")
            self._cmd_available = False
            self._refresh_btns()
            return

        extra = [x for x in (st.get("extra_flags") or "") if x]
        override = self.ovr.get("1.0", "end").strip()
        try:
            port = int(self.port_var.get() or 8080)
        except (ValueError, tk.TclError):
            port = 8080
        host = self.host_var.get().strip() or "127.0.0.1"
        clear_mode = self.clear_mode_var.get() if hasattr(self, "clear_mode_var") else "override"
        cmd = recommend.build_command(st, hw, plan, binary,
                                      port=port, host=host, extra=extra, params=p,
                                      override_args=override,
                                      clear_mode=clear_mode)
        # 计算被覆盖的冲突族用于提示
        import shlex
        user_tokens = shlex.split(override or "")
        fams = set()
        for tok in user_tokens:
            if tok.startswith("-"):
                fam = recommend._flag_family(tok)
                if fam:
                    fams.add(fam)
        hit_names = sorted("或".join(sorted(f)) for f in fams)

        cmd_str = _shell_str(cmd) if IS_WIN else " ".join(cmd)
        self._last_cmd = cmd_str
        self.cmd.config(state="normal")
        self.cmd.delete("1.0", "end")
        self.cmd.insert("1.0", cmd_str)
        self.cmd.config(state="disabled")
        # --lora 引擎门控提示：仅 vanilla 支持；非 vanilla 选了 LoRA → 已剥掉（防 ik 拒载）
        _lora_on = str(p.get("lora", "")).strip() not in ("", "off", "auto", "default", "不配置")
        if _lora_on and engine != "vanilla":
            self._log(f"ℹ --lora 仅 vanilla 引擎支持，当前 {engine} 已忽略该 LoRA（切 vanilla 才生效）")

        if hit_names:
            self.ovr_hint.config(
                text="自定义参数已覆盖面板同名参数：" + "、".join(hit_names),
                foreground="#c0392b")
        elif override:
            self.ovr_hint.config(text="自定义参数与面板无冲突，按原样追加",
                                 foreground="#666")
        else:
            self.ovr_hint.config(text=" ", foreground="#666")

        lines = [f"引擎: {engine}（binary: {os.path.basename(binary)}）"]
        lines += [f"  - {n}" for n in eng_lines if n]
        lines.append(
            f"模型 ≈ {st.get('file_gb', 0):.1f} GiB（{plan.get('kv_type','q8_0')} KV，ctx={ctx}）")
        # 硬件描述不再重复（顶部硬件栏已显示）
        if plan.get("plan_notes"):
            lines.append("计划: " + plan["plan_notes"])
        if plan.get("swa_note"):
            lines.append(plan["swa_note"])
        if plan.get("dynamic_moe_note"):
            lines.append(plan["dynamic_moe_note"])
        cpu_moe_on = str(p.get("cpu_moe", "off")).strip() == "on"
        hints = [h for h in (plan.get("hints") or []) if h]
        if cpu_moe_on:  # --cpu-moe 全专家已上 CPU，OOM 提示不再适用
            hints = [h for h in hints if not h.startswith("⚠️ OOM")]
        for h in hints:
            lines.append("提示: " + h)
        # 与提示内容重复的告警不再重打（去重）
        warns = [w for w in (plan.get("warnings") or [])
                 if not any(h and h in w for h in hints)]
        if warns:
            lines.append("⚠ " + "; ".join(warns))
        if st.get("mmap_ok") is False:
            lines.append("警告: 该模型无法 mmap（NVMe/网络卷）→ 将整读入 RAM")
        if engine == "tosh" and str(p.get("moe_cache_mib")) not in ("0", "auto", "default", "不配置", ""):
            lines.append("注: tosh 引擎无 moe-cache-mib 旗标，该项不会出现在命令里")
        # vanilla + 设了 moe-cache-mib 但无 CPU 专家（全 GPU）→ 该 flag 已被 build_command 剔除，提示如何启用
        if engine != "tosh" and str(p.get("moe_cache_mib")) not in ("0", "auto", "default", "不配置", "") \
                and not recommend._has_cpu_experts(cmd):
            lines.append("注: 已剔除 --moe-cache-mib（当前全专家在 GPU、无 -ncmoe/-cmoe，缓存无效会致加载失败）；要启用请在自定义框加 --n-cpu-moe N 或 cpu-moe=on")
        if self.dm_var.get() and st.get("is_moe"):
            n = "本机 dynamic-moe 规划器易因 VRAM/coverage 拒载，拒载后自动以 off 重试"
            if str(p.get("cpu_moe", "off")).strip() == "on":
                n += "；且 --cpu-moe（专家全上 CPU）与 --dynamic-moe 语义冲突，建议 cpu-moe 选 off"
            lines.append("注: " + n)
        # 过滤：不显示 "⚠"/"警告"/"清空模式" 开头的行（已进主日志）
        filtered = [ln for ln in lines if not ln.startswith(("⚠", "警告", "清空模式"))]
        oom_on = bool(plan.get("oom_risk")) and not cpu_moe_on
        self.info.config(text="\n".join(filtered) or "（选择左侧模型以查看推荐参数）",
                         fg="#c62828" if oom_on else "#c77700")
        # 命令已生成：点亮 ⚡生成指令 / 复制 两钮
        self._cmd_available = bool(cmd_str.strip())
        self._refresh_btns()

    def _gen_cmd(self):
        """手动触发命令生成（刷新命令框）。若未选模型自动选第一个。"""
        if self.page == "image":
            # 图片页命令由 self._img_model 驱动；不做文字页的“自动补选”（补选会触发 prefill，把提示词重置成预填→被清空）
            self._refresh_recommend()
            self._log("已刷新命令")
            return
        if not self.selected:
            # 自动补选：取 tree 第一个可见项
            children = self.tree.get_children()
            if children:
                self.tree.selection_set(children[0])
                self.tree.focus(children[0])
                self._on_select()
            if not self.selected:
                # tree 空 = 扫描尚未完成，直接等扫描线程写完 queue
                self._log("模型库尚未扫描完成，请稍候再点「⚡ 生成指令」。")
                return
        self._refresh_recommend()
        self._log("已刷新命令")

    def _on_copy_click(self):
        """复制钮入口：未生成命令时提示；已生成则复制到剪贴板。"""
        if not self._cmd_available:
            messagebox.showinfo("未生成", "请先生成命令（选择模型后点「⚡ 生成指令」）。")
            return
        self._do_copy()

    def _do_copy(self):
        txt = self.cmd.get("1.0", "end").strip()
        if not txt:
            self._log("命令框为空，未复制")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self._log("已复制命令到剪贴板")
        self._refresh_btns()

    # ---------------- 配置另存（预设） / 导出 / 导入 ----------------
    def _current_state(self) -> dict:
        """捕获当前完整配置：参数面板 + 自定义覆盖 + 引擎 + 端口 + host + dynamic-moe。"""
        if self.page == "image":
            return {
                "page": "image",
                "img_model": self._img_model.get("name") if self._img_model else "",
                "img_params": {k: self.ipv[k].get() for k in self.ipv},
                "img_prompt": self.itext["prompt"].get("1.0", "end").strip(),
                "img_neg": self.itext["neg"].get("1.0", "end").strip(),
                "img_lora": {n: float(self.lora_chosen[n].get()) for n in self.lora_chosen},
            }
        return {
            "page": "text",
            "params": {k: v.get() for k, v in self.pv.items()},
            "override": self.ovr.get("1.0", "end").strip(),
            "engine": self.engine_var.get(),
            "port": self.port_var.get(),
            "host": self.host_var.get(),
            "dynamic_moe": bool(self.dm_var.get()),
        }

    def _apply_state(self, state: dict):
        """把一份配置状态套到当前面板（预设/导入共用），再刷新命令。"""
        state = state or {}
        if state.get("page") == "image" and self.page == "image":
            self._restoring = True
            try:
                # 配方B 等：按 img_model 名从模型库找 → 预填 ① 变体 + LoRA 目录 + 默认 ②③
                imname = state.get("img_model")
                if imname:
                    m = next((c for c in self.img_cache.values() if c.get("name") == imname), None)
                    if m:
                        self._img_model = m
                        self._img_prefill_from_model(m)
                for k, v in (state.get("img_params") or {}).items():
                    if k in self.ipv:
                        try:
                            self.ipv[k].set(str(v))
                        except tk.TclError:
                            pass
                self._set_img_text("prompt", state.get("img_prompt") or "")
                self._set_img_text("neg", state.get("img_neg") or "")
                chosen = state.get("img_lora")
                if chosen is not None:
                    self.lora_chosen = {}
                    for n, v in (chosen or {}).items():
                        self.lora_chosen[n] = tk.DoubleVar(value=float(v))
                        self.lora_w_cache[n] = float(v)
                    self._render_lora_chosen()
            finally:
                self._restoring = False
            self._refresh_recommend()
            return
        self._restoring = True
        try:
            params = state.get("params") or {}
            for k, var in self.pv.items():
                if k in params:
                    try:
                        var.set(str(params[k]))
                    except tk.TclError:
                        pass
            self.ovr.config(state="normal")
            self.ovr.delete("1.0", "end")
            self.ovr.insert("1.0", state.get("override") or "")
            eng = state.get("engine")
            if eng in ("ik", "vanilla", "tosh", "auto"):
                if IS_WIN and eng in ("tosh", "auto"):
                    eng = "vanilla"
                self.engine_var.set(eng)
            if "dynamic_moe" in state:
                self.dm_var.set(bool(state.get("dynamic_moe")))
            if state.get("port"):
                self.port_var.set(str(state["port"]))
            if state.get("host"):
                self.host_var.set(str(state["host"]))
        finally:
            self._restoring = False
        self._refresh_recommend()

    def _load_presets(self) -> dict:
        try:
            with open(PRESET_FILE, encoding="utf-8") as f:
                presets = json.load(f)
        except Exception:
            presets = {}
        # 预置「配方B」（图片页）：z-image-turbo Q4_0 + 双 LoRA（pussy 0.8 / skin_texture 0.5）
        # + 提示词/负向增补 + 1:1/1024/16/seed1234/euler。按页分离：仅图片页预设区显示。
        if "配方B" not in presets:
            presets["配方B"] = {
                "page": "image",
                "img_model": "z image turbo",
                "img_recipe": "B",
                "img_params": {"aspect": "1:1", "res": "1024", "cfg": "1.1",
                               "sampler": "euler", "steps": "16", "seed": "1234", "format": "png"},
                "img_prompt": "东亚女性，真实皮肤纹理，可见毛孔，自然质感",
                "img_neg": "plastic, waxy, 塑料感、过度磨皮",
                "img_lora": {"lora-pussy": 0.8, "zimage_skin_texture_v45": 0.5},
            }
        return presets

    def _save_presets(self, presets: dict):
        tmp = PRESET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PRESET_FILE)

    def _refresh_preset_names(self):
        allp = self._load_presets()
        cur_page = self.page
        names = sorted(n for n, v in allp.items() if (v or {}).get("page", "text") == cur_page)
        curn = self.preset_var.get()
        self.preset_combo["values"] = names
        if curn in names:
            self.preset_combo.set(curn)
        elif names:
            self.preset_combo.set(names[0])
        self.preset_hint.config(text=(f"{len(names)} 个{'图片' if cur_page == 'image' else '文字'}预设")
                                 if names else "（无预设）")

    def _preset_load(self):
        name = self.preset_var.get().strip()
        entry = self._load_presets().get(name)
        if not entry:
            messagebox.showwarning("加载预设", "无此预设或未选择。")
            return
        self._apply_state(entry)
        self._log(f"已加载预设「{name}」")
        self._save_state_entry()   # 若已选模型，顺带把套上的配置按模型落盘

    def _preset_save_as(self):
        if self.page == "image":
            init = (self._img_model.get("name") if self._img_model else "图片配方")
        else:
            init = ((self.selected or {}).get("name") or "我的预设").replace(".gguf", "")
        name = simpledialog.askstring("另存预设", "预设名称（可跨模型复用）：",
                                     parent=self.root, initialvalue=init)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        presets = self._load_presets()
        presets[name] = self._current_state()
        try:
            self._save_presets(presets)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        self._refresh_preset_names()
        self.preset_combo.set(name)
        self._log(f"已另存预设「{name}」→ {os.path.basename(PRESET_FILE)}")

    def _preset_delete(self):
        name = self.preset_var.get().strip()
        presets = self._load_presets()
        if name not in presets:
            self._log(f"（无此预设，未删除）「{name}」" if name else "未选择预设，未删除")
            return
        del presets[name]
        self._save_presets(presets)
        self._refresh_preset_names()
        self._log(f"已删除预设「{name}」")

    def _export(self):
        model_name = ((self.selected or {}).get("name") or "llama-shell").replace(".gguf", "")
        default = f"{model_name}_llama-shell.json"
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".json", initialfile=default,
            filetypes=[("JSON", "*.json")], title="导出配置")
        if not path:
            return
        payload = {
            "app": "llama-shell",
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_model": (self.selected or {}).get("path", ""),
            "state": self._current_state(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return
        self._log(f"已导出配置 → {os.path.basename(path)}")

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self.root, filetypes=[("JSON", "*.json"), ("All", "*")],
            title="导入配置")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("导入失败", f"无法解析 {os.path.basename(path)}：{e}")
            return
        # 兼容：导出封装（含 state）或裸状态 dict
        state = data.get("state") if isinstance(data, dict) and "state" in data else data
        if not isinstance(state, dict) or "params" not in state:
            messagebox.showerror("导入失败", "文件格式不符：缺少 params 字段。")
            return
        self._apply_state(state)
        self._log(f"已导入配置 ← {os.path.basename(path)}")
        self._save_state_entry()

    # ---------------- 运行 / 停止 / 测速 ----------------
    def _on_run_click(self):
        """运行钮点击：无存活进程才允许启动。"""
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("运行中", "已有进程在运行，请先停止。")
            return
        self._run()

    def _on_stop_click(self):
        """停止钮点击：有存活进程才允许停止。"""
        if self.proc and self.proc.poll() is None:
            self._stop()
        else:
            self._log("当前无运行中的进程，无需停止。")

    def _on_gen_click(self):
        """⚡生成指令钮点击：刷新命令（未选模型自动补选）。"""
        self._gen_cmd()

    def _run(self):
        # 点击层已拦截：仅在无存活进程时才会调用到这里
        if self.page == "image":
            self._run_image()
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("运行中", "已有进程在运行，请先停止。")
            return
        import shlex
        cmd_str = self.cmd.get("1.0", "end").strip()
        if not cmd_str:
            messagebox.showwarning("无命令", "请先生成命令（选择模型）。")
            return
        self._auto_retried = False
        # 双轨：显示串是 _shell_str 产物（Windows 双引号）；解析回 token 用 posix shlex
        # （去外层引号、引号内反斜杠保留）——Popen 拿不带引号的原始 token
        self._spawn(shlex.split(cmd_str))

    def _spawn(self, cmd):
        # 兼容：cmd 可以是 list（推荐路径）或字符串（__RETRY__ 旧路径）
        if isinstance(cmd, str):
            import shlex
            cmd_list = shlex.split(cmd)   # 双轨：posix 解析去 _shell_str 的外层引号
        else:
            cmd_list = [str(x) for x in cmd]
        cmd_str = _shell_str(cmd_list) if IS_WIN else " ".join(cmd_list)
        self._last_cmd = cmd_str
        self._last_argv = list(cmd_list)
        self._running_bin = cmd_list[0]   # 记录在用引擎二进制（升级轮次删除时保护该版本目录）
        self._dynamic_moe_used = "--dynamic-moe on" in " ".join(cmd_list)
        self._user_stopped = False        # 新进程：非零退出先按崩溃告警，除非随后用户停止
        self._log(f"启动: {cmd_str}")
        # 11-13 自愈：bind 前清掉占用端口的残留进程（上次会话孤儿/停不掉的 35B），
        # 否则新 server 加载即失败。kill_port 只命中该端口的监听者（Windows=netstat/taskkill）。
        try:
            port = int(self.port_var.get() or 8080)
        except Exception:
            port = 8080
        host = (self.host_var.get().strip() or "127.0.0.1")
        self._last_port = port
        self._health_port, self._health_host = port, host   # 主线程缓存，供后台 _health_poll 读取
        pids = kill_port(port)
        if pids:
            self._log(f"已先清理占用端口 {port} 的残留进程 PIDs {pids}")
        try:
            # cwd 设为二进制所在目录：llama-server 须能找到同目录 DLL
            # （D:\ik-win\bin 的 llama.dll/ggml.dll、D:\llama-vulkan-x64 的 Vulkan 加载器）
            bin_dir = os.path.dirname(cmd_list[0]) or BASE
            if IS_WIN:
                # 无 shell（路径含空格/中文安全）；CREATE_NO_WINDOW 避免弹黑色控制台；
                # errors="replace"：server 控制台输出可能含非 UTF-8 字节（中文系统 GBK 控制字符），
                # 默认 UTF-8 严格解码会崩掉读线程 → 整窗消失（0.1 实测根因）
                # PYTHONIOENCODING：让子进程 stdout 恒 UTF-8（否则中文系统 GBK 编码与 utf-8 解码错位）
                _env = dict(os.environ)
                _env["PYTHONIOENCODING"] = "utf-8"
                self.proc = subprocess.Popen(
                    cmd_list, cwd=bin_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=_env,
                    creationflags=0x08000000,
                    text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
            else:
                # shell=False + start_new_session=True：self.proc 直接指向 llama-server，
                # os.killpg 能精准杀掉它（含其工作线程）。旧 shell=True 会让 self.proc
                # 指向 /bin/sh，killpg 只杀到 shell，llama-server 成孤儿继续占端口/内存。
                self.proc = subprocess.Popen(
                    cmd_list, shell=False, cwd=BASE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                    text=True, bufsize=1)
        except Exception as e:
            self._log(f"[错误] 启动失败: {e}")
            return
        # 关键：_ready 必须等 HEALTH ok 才置位（_on_health），这里只能回灰，
        # 绝不能直接置 ready——否则点击瞬间按钮就会变绿（"一按就绿"的根因）
        self._ready = False
        self._reader_stop = False
        self._health_stop = False
        self._refresh_btns()          # 画灰（● 正在加载）；绿色由 _on_health 点亮
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._health_poll, daemon=True).start()
        self._set_run_ui(True)
        self.status.config(text="运行中…", foreground="#c77700")

    def _reader(self):
        p = self.proc
        from collections import deque
        recent = deque(maxlen=4)
        for line in iter(p.stdout.readline, ""):
            if self._reader_stop:
                break
            stripped = line.rstrip()
            # 去重：与最近 4 行中任一行相同则跳过（stderr 合并/重定向偶发重复行）
            if stripped in recent:
                continue
            recent.append(stripped)
            self.q.put("OUT:" + line)
            # 自动回退：tosh 规划器拒载（VRAM/coverage）→ 用 dynamic-moe off 重跑一次
            if ("TOSH_AUTO found no configuration that fits" in line
                    or "PLAN_UNSUPPORTED" in line):
                if self._dynamic_moe_used and not self._auto_retried:
                    self._auto_retried = True
                    self.q.put("OUT:\n[GUI] Dynamic MoE 被规划器拒载（VRAM/coverage 不足）— 自动以 --dynamic-moe off 重试 …\n")
                    retry = self._last_cmd.replace("--dynamic-moe on", "--dynamic-moe off")
                    self.q.put("__RETRY__:" + retry)
        rc = p.wait()
        self.q.put(f"PROC_EXIT:{p.pid}:{rc}")   # pid 标签：_poll 据此忽略旧进程的迟到退出事件

    def _health_poll(self):
        # 后台线程不得碰 tkinter 变量（RuntimeError: main thread is not in main loop）
        # → 用 _spawn 时主线程缓存的 _health_port/_health_host
        port = getattr(self, "_health_port", 8080)
        host = getattr(self, "_health_host", "127.0.0.1")
        last_ok = None
        while not self._health_stop:
            p = self.proc      # 实时读取：dynamic-moe 自动重试换新进程后，盯住新进程继续轮询
            if p is None or p.poll() is not None:
                break          # 无进程 / 进程已退（_stop/PROC_EXIT 会把 proc 置 None）
            try:
                ok = health(port, host)
            except Exception:
                ok = False
            # 仅在结果变化时入队 → 事件不再每 1s 灌满队列；_poll 循环保持轻载
            if ok is not last_ok:
                self.q.put("HEALTH:" + ("1" if ok else "0"))
                last_ok = ok
            time.sleep(1.0)

    def _stop(self):
        self._reader_stop = True
        self._health_stop = True
        self._user_stopped = True   # 标记：随后的非零退出 = 用户主动停止，不打红字
        proc = self.proc
        if proc is not None and proc.poll() is None:
            if IS_WIN:
                # Windows 无进程组：taskkill /T 递归杀整棵进程树
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=5,
                                   creationflags=0x08000000)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._log("已停止（taskkill /T）")
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                self._log("已发送 SIGTERM（进程组），后台等待退出…")
            # 11-13：等待/SIGKILL/清端口全部移入后台 _reap 线程——
            # 35B 大模型 SIGTERM 慢时，主线程不再卡 5s，停止按钮/关窗立即可用。
            threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
        self.proc = None  # 立即清空，运行钮瞬间可用；旧进程退出后 PROC_EXIT 会再次置 None 无害
        self._ready = False  # 已停止 → 运行钮恢复灰色（须健康轮询再次判定 ready）
        self._refresh_btns()
        # 清理端口：杀残留占用进程（11-13：优先 _last_port——atexit 场景下 tk 可能已销毁）
        try:
            port = int(self.port_var.get() or 8080)
        except (ValueError, tk.TclError):
            port = getattr(self, "_last_port", 8080)
        self._last_port = port
        killed = kill_port(port)
        if killed:
            self._log(f"已清理端口 {port}（PID: {killed}）")
        self._set_run_ui(False)
        self.status.config(text="停止中…", foreground="#c77700")

    def _reap(self, proc):
        """后台回收线程（11-13）：SIGTERM 后最多等 5s → 仍存活则 SIGKILL 进程组 → 清端口。
        全程不碰任何 widget，只经 q.put 回传日志；主线程 destroy 后消息静默丢弃，atexit 兜底接力。"""
        for _ in range(25):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            if IS_WIN:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=5,
                                   creationflags=0x08000000)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self.q.put("OUT:\n[GUI] 进程未退出，已 taskkill /T 强制结束\n")
                time.sleep(0.3)
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self.q.put("OUT:\n[GUI] 进程未退出，已发送 SIGKILL（进程组）强制结束\n")
                time.sleep(0.3)
        try:
            killed = kill_port(getattr(self, "_last_port", 8080))
            if killed:
                self.q.put(f"OUT:\n[GUI] 已清理端口 {self._last_port} 残留（PID: {killed}）\n")
        except Exception:
            pass
        self.q.put("OUT:\n[GUI] 已停止\n")

    def _on_close(self):
        """窗口关闭：非阻塞地停掉 server，立即销毁窗口。

        若 server 正在运行：发 SIGTERM 到进程组，后台清理，不等它退出。
        这样 macOS 用户点关闭按钮后窗口立即消失，不会卡 5 秒。
        """
        self._closing = True
        self._reader_stop = True
        self._health_stop = True
        # 取消所有 pending after 定时器，防止 destroy 后回调再入主循环
        for attr in ("_poll_id", "_hw_tick_id"):
            try:
                tid = getattr(self, attr, None)
                if tid:
                    self.root.after_cancel(tid)
            except Exception:
                pass
        # 若有 server 在跑：发终止信号（不阻塞等待），靠后台 reader 线程收尾
        if self.proc is not None and self.proc.poll() is None:
            if IS_WIN:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True, timeout=5,
                                   creationflags=0x08000000)
                    self._log("正在停止 server…（taskkill /T，后台）")
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            else:
                try:
                    import signal, os
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                    self._log("正在停止 server…（后台）")
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None
        # 兜底清理端口：杀残留的 llama-server 孤儿（dynamic-moe 切换后 shell 死了但 server 还在）
        try:
            port = int(self.port_var.get() or 8080)
        except (ValueError, tk.TclError):
            port = getattr(self, "_last_port", 8080)
        self._last_port = port
        try:
            killed = kill_port(port)
            if killed:
                self._log(f"已清理端口 {port} 残留（PID: {killed}）")
        except Exception:
            pass
        try:
            self._save_state_entry()
        except Exception:
            pass
        self.root.destroy()

    def _bench(self):
        try:
            port = int(self.port_var.get() or 8080)
        except (ValueError, tk.TclError):
            port = 8080
        host = self.host_var.get().strip() or "127.0.0.1"
        if not health(port, host):
            self._log(f"[测速] {host}:{port} 无响应，先运行 server。")
            return
        self.status.config(text="测速中…", foreground="#c77700")
        threading.Thread(target=self._bench_worker, args=(port, host), daemon=True).start()

    def _bench_worker(self, port, host):
        try:
            tps = bench_once(port, host)
            self.q.put(f"OUT:\n[测速] 生成 48 token → {tps:.1f} t/s\n")
        except Exception as e:
            self.q.put(f"OUT:\n[测速] 失败: {e}\n")

    # ---------------- 轮询（主线程） ----------------
    def _log(self, msg, warn=False):
        # 末行去重：防 stderr 合并/重定向导致的重复行刷屏
        try:
            last = self.log.get("end-2c", "end-1c").strip()
        except Exception:
            last = ""
        if last == msg.strip():
            return
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", "warn" if warn else None)
        # 限长：超过 800 行裁掉前 400 行，避免无限增长拖慢 UI
        try:
            line_no = int(self.log.index("end-1c").split(".")[0])
            if line_no > 800:
                self.log.delete("1.0", f"{line_no - 400}.0")
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")
        # Windows（DPI 感知下 insert 不重绘/不滚动会“日志不刷新”）：
        # 强制视图滚到底 + 下一 tick 强制重绘
        if IS_WIN:
            try:
                self.log.yview_moveto(1.0)
                self.root.after(0, self.log.update_idletasks)
            except Exception:
                pass

    def _poll(self):
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                item = self.q.get_nowait()
                if item == "__NOOP__":
                    break
                if item.startswith("__SCAN_DONE__"):
                    self._fill_tree(json.loads(item[len("__SCAN_DONE__"):]))
                    self._log(f"已载入 {len(self.st_cache)} 个模型")
                elif item.startswith("OUT:"):
                    self._log(item[4:])
                    # 出图步数进度（兜底：专用 __IMG_PROGRESS__ 事件未命中时）：sd-cli 采样行
                    # "... | CUR/TOT - X.XX(s/it|it/s)" → 预览区进度条 X/N 步（GB/s=加载相位，不驱动步数）
                    import re as _re
                    ms = _re.search(r'(\d+)\s*/\s*(\d+)\s*-\s*[\d.]+\s*(?:s/it|it/s)', item)
                    if ms and hasattr(self, "img_prog") and self.page == "image":
                        _c, _t = int(ms.group(1)), int(ms.group(2))
                        self._update_img_progress(50 + min(50, int(_c / max(1, _t) * 50)), 100,
                                                  note=f"采样 {_c}/{_t} 步")
                elif item.startswith("__IMG_PROGRESS__:"):
                    _pr = item[len("__IMG_PROGRESS__:"):].split(":")
                    if hasattr(self, "img_prog"):
                        # 统一 0-100 标尺：加载相位占 0-50、采样相位占 50-100
                        # （11-23 主人报"一开始就刷全绿然后才从头开始"= 旧版加载走满 453/453 后采样从 1/16 归零跳变）
                        if len(_pr) == 3 and _pr[0] == "L":
                            _c, _t = int(_pr[1]), int(_pr[2])
                            self._update_img_progress(min(50, int(_c / max(1, _t) * 50)), 100,
                                                      note=f"加载模型 {_c}/{_t}")
                        elif len(_pr) == 2:
                            _c, _t = int(_pr[0]), int(_pr[1])
                            self._update_img_progress(50 + min(50, int(_c / max(1, _t) * 50)), 100,
                                                      note=f"采样 {_c}/{_t} 步")
                elif item.startswith("HEALTH:"):
                    ok = item[7:] == "1"
                    # 状态机：HEALTH 健康 = 系统 ready（运行钮可点亮的门）
                    self._ready = ok
                    if hasattr(self, "status"):
                        self.status.config(
                            text="READY" if ok else "运行中…",
                            foreground="#1a7f37" if ok else "#c77700")
                    self._refresh_btns()
                elif item.startswith("PROC_EXIT:"):
                    # pid 标签退出事件（_reader 发 PROC_EXIT:{pid}:{rc}）：
                    # 旧进程（已被 _stop 置 None 的）的迟到事件按 pid 识别丢弃，不误刷状态
                    pidd, _, rc = item[len("PROC_EXIT:"):].partition(":")
                    rc = rc.strip() or "?"
                    cur = str(self.proc.pid) if (self.proc is not None and getattr(self.proc, "pid", None)) else ""
                    if pidd and cur and pidd != cur:
                        self._log(f"[内部] 旧进程退出事件已忽略 rc={rc}")
                    else:
                        self.proc = None
                        self._ready = False        # 进程退出 → 不再 ready，运行钮恢复灰色
                        self._set_run_ui(False)
                        self._refresh_btns()
                        if rc in ("0", ""):
                            self.status.config(text="已退出", foreground="#666")
                            self._log("进程已退出 rc=0（正常）")
                        elif getattr(self, "_user_stopped", False):
                            self.status.config(text="已停止", foreground="#666")
                            self._log(f"已停止（用户停止，rc={rc}，正常）")
                        else:
                            try:
                                port = (self.port_var.get() or "").strip() or "8080"
                            except tk.TclError:
                                port = "8080"
                            self.status.config(text="已退出", foreground="#c0392b")
                            self._log(f"进程已退出 rc={rc}（非零）。常见原因：端口 {port} 被其他实例占用 / "
                                      f"模型路径错误 / OOM / Vulkan 驱动异常")
                elif item.startswith("__UPG_INFO__:"):
                    # 升级检查完且本地已最新 → 刷新版本标签、恢复按钮（不下载）
                    info = json.loads(item[len("__UPG_INFO__:"):])
                    self._upg_label(info)
                    self._upgrading = False
                    self.upg_btn.config(state="normal")
                elif item.startswith("__UPG_ASK__:"):
                    # 有比本地新的版本 → 刷新标签 + 主线程弹版本选择框
                    info = json.loads(item[len("__UPG_ASK__:"):])
                    self._upg_label(info)
                    self._ask_upgrade(info)
                elif item.startswith("__UPG_FAIL__:"):
                    msg = item[len("__UPG_FAIL__:")]
                    self._log(f"[升级] 已中断: {msg}")
                    self._upgrading = False
                    self.upg_btn.config(state="normal")
                elif item.startswith("__UPG_DONE__:"):
                    # 安装成功 → 配置 vanilla_bin 指向新版（服务即指向新版本）+ engbar 回显
                    d = json.loads(item[len("__UPG_DONE__:"):])
                    cfg = _load_config()
                    cfg["vanilla_bin"] = d["path"]
                    _save_config(cfg)
                    self._refresh_engbar()   # 内部同步模块全局 VANILLA_ABS + recommend.VANILLA_BIN（免重启）
                    self._refresh_recommend()   # 立即重生成命令 → cmd 框即刻指向新引擎（不必重启 app）
                    self._log(f"[升级] ✅ 已安装 b{d['num']}: {d['path']}（vanilla_bin 已更新，运行即新版）")
                    self._upgrading = False
                    self.upg_btn.config(state="normal")
                elif item.startswith("__IMG_SCAN_DONE__:"):
                    self._fill_tree_image(json.loads(item[len("__IMG_SCAN_DONE__:"):]))
                    self._log(f"[图片] 已载入 {len(self.img_cache)} 个文生图模型")
                elif item.startswith("__IMG_DONE__:"):
                    _parts = item.split(":", 2)
                    rc, path = _parts[1], _parts[2] if len(_parts) > 2 else ""
                    self.proc = None
                    self._ready = False
                    self._set_run_ui(False)
                    self._refresh_btns()
                    if hasattr(self, "img_prog"):
                        self._update_img_progress(100 if path else 0, 100,
                                                 "完成 ✓" if path else f"失败 rc={rc}")
                    if path:
                        self._load_preview(path)
                        self.status.config(text="已生成", foreground="#1a7f37")
                        self._log(f"[图片] 完成(rc={rc}): {path}")
                    else:
                        self.status.config(text="已退出", foreground="#666")
                        self._log(f"[图片] 生成结束 rc={rc}（未找到输出文件）")
                elif item.startswith("__RETRY__:"):
                    retry_cmd = item[len("__RETRY__:"):].strip()
                    def do_retry():
                        if self._closing:
                            return
                        if self.proc is None:
                            return          # 用户已停止/关闭，放弃重试
                        if self.proc.poll() is None:
                            self.root.after(300, do_retry)   # 等旧进程退出再换
                            return
                        self.cmd.config(state="normal")
                        self.cmd.delete("1.0", "end")
                        self.cmd.insert("1.0", retry_cmd)
                        self.cmd.config(state="disabled")
                        self._spawn(retry_cmd)
                    do_retry()
                else:
                    self._log(item)
        except queue.Empty:
            pass
        except Exception as e:
            # 任一事件处理异常只记日志丢弃，不断 _poll 链（断链=日志/UI 停更、整窗假死）
            self._log(f"[内部] 事件处理异常已丢弃: {e}")
        self._poll_id = self.root.after(80, self._poll)


def main():
    if IS_WIN:
        # Per-Monitor DPI 感知：不开则 Windows 对高分屏做 100% 虚化（字糊）；
        # shcore SetProcessDpiAwareness 失败（旧系统）退回 user32 SetProcessDPIAware
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    root = tk.Tk()
    if sys.platform == "darwin":
        try:
            root.tk.call("tk", "scaling", 2.0)  # macOS 高分屏放大
        except Exception:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()