#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recommend.py — 推理参数推荐引擎（llama-shell）

移植自 toshllm 0.87.11 的规划启发式（Engelbert Delgado），针对
本地硬件（i5-12400F / 32GB / macOS x64 + llama.cpp b12212）裁剪。

来源（/tmp/toshllm_ref/）：
  - Sources_Hardware_Hardware.swift   → Estimator.estimate / kvBytesPerToken / kvTypeScale
  - Handoff_DynamicMoE.md §5-§6        → 主机内存 cap / coverage 判定
  - Docs_DynamicMoE.md                 → ncmoe>30 层警告、ubatch、MTP head 收益

公式（与 toshllm 一致，保留原常数）：
  computeGB = 0.9 + paramsB * 0.012
  dense 全 GPU：need = weights*1.03 + kv + compute
  MoE：denseGB(attention+shared) 常驻 GPU；experts 按 VRAM 余量切分；
       ncmoe = ceil(layers * cpu_experts/experts)
  KV 精确值 = attn_layers * heads_kv * (klen+vlen) * 2 字节/token（f16）
  kvTypeScale: f16=1.0 q8_0=0.53 q5_1=0.38 q5_0=0.36 q4_1=0.32 其它≈0.30
  RAM 带宽按 48 GB/s；MoE 效率 0.48
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

GiB = 1024 ** 3


# ---------------------------------------------------------------- 硬件探测

@dataclass
class Hardware:
    ram_gb: float
    physical_cores: int
    logical_cores: int
    cpu_brand: str
    gpus: list = field(default_factory=list)   # [{name, vram_gb, integrated}]
    ram_bw_gbps: float = 48.0                  # toshllm 常数
    metal_compute: bool = True                 # 是否有 Metal 可算 GPU
    vulkan_compute: bool = False               # 若走 Vulkan 后端，独显即可用作计算

    @property
    def best_gpu(self):
        dis = [g for g in self.gpus if not g.get("integrated")]
        return (dis or self.gpus or [None])[0]

    @property
    def vram_gb(self) -> float:
        return (self.best_gpu or {}).get("vram_gb", 0.0) or 0.0

    @property
    def has_gpu(self) -> bool:
        """可 offload 的 GPU：须真有 VRAM 且 Metal 可算（显示-only 不算）。"""
        g = self.best_gpu
        if not g or g.get("vram_gb", 0) <= 0.2:
            return False
        if not g.get("usable_compute", self.metal_compute):
            return False
        return True


def detect_hardware(ram_bw_override: float | None = None,
                    vram_override_gb: float | None = None,
                    gpu_cache: list | None = None) -> Hardware:
    """跨平台硬件探测。
    - vram_override_gb: 用户显式声明的独显显存（GiB），覆盖探测值（最高优先级）。
    - gpu_cache: 上次探得的 GPU 列表（gui 从 llama-shell-config.json 读入，30 天内免 dxdiag 慢探测）。
    """
    if sys.platform == "win32":
        # 先检查 GPU 缓存，命中则跳过 10s 的 WMI/dxdiag 慢探测
        cache_hit = False
        if gpu_cache:
            try:
                import json, time
                cached = json.loads(gpu_cache) if isinstance(gpu_cache, str) else gpu_cache
                if (time.time() - cached.get("ts", 0)) < 30 * 86400 and cached.get("gpus"):
                    cache_hit = True
            except Exception:
                pass
        if cache_hit:
            # 快速路径：只探测 CPU/RAM（ctypes，<0.1s），GPU 用缓存
            hw = _detect_hw_fast()
            hw.gpus = cached["gpus"]
        else:
            hw = _detect_hardware_windows(ram_bw_override)
            # 保存探测结果供下次缓存
            global _last_gpu_probe
            _last_gpu_probe = {"ts": time.time(), "src": "wmi", "gpus": hw.gpus}
    elif sys.platform == "darwin":
        hw = _detect_hardware_macos(ram_bw_override)
    else:
        hw = _detect_hardware_generic(ram_bw_override)
    _apply_gpu_policy(hw)
    if vram_override_gb:
        for g in hw.gpus:
            if not g.get("integrated"):
                g["vram_gb"] = float(vram_override_gb)
                break
        _apply_gpu_policy(hw)
    return hw


def _apply_gpu_policy(hw: "Hardware"):
    """独显（dGPU）走 Vulkan/CUDA 可算；集显（iGPU）视为低预算、不可 offload。"""
    for g in hw.gpus:
        if g.get("integrated"):
            g["vram_gb"] = min(g["vram_gb"], 0.5)   # iGPU 视为不可用预算
            g["usable_compute"] = False
        else:
            g["usable_compute"] = True
    hw.vulkan_compute = any((not g.get("integrated")) and g.get("vram_gb", 0) > 0.2
                            for g in hw.gpus)


def _detect_hw_fast() -> "Hardware":
    """快速硬件探测（<0.1s）：CPU/RAM 用 ctypes+psutil，GPU 留空由调用方填缓存。
    跳过 PowerShell WMI 查询和 dxdiag（10-20s），用于 30 天内缓存命中场景。"""
    import ctypes
    ram = 0.0
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        ram = m.ullTotalPhys / GiB
    except Exception:
        try:
            import psutil
            ram = psutil.virtual_memory().total / GiB
        except Exception:
            ram = 0.0
    lc = os.cpu_count() or 0
    pc = lc
    try:
        import psutil
        p = psutil.cpu_count(logical=False)
        if p:
            pc = p
    except Exception:
        pass
    brand = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            brand = winreg.QueryValueEx(k, "ProcessorNameString")[0]
    except Exception:
        pass
    return Hardware(ram_gb=ram, physical_cores=pc, logical_cores=lc, cpu_brand=brand,
                    gpus=[], ram_bw_gbps=48.0,
                    metal_compute=False, vulkan_compute=False)


def _detect_hardware_windows(ram_bw_override: float | None = None) -> Hardware:
    import ctypes
    ram = 0.0
    # --- RAM: GlobalMemoryStatusEx（纯 ctypes，无第三方依赖）
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        ram = m.ullTotalPhys / GiB
    except Exception:
        try:
            import psutil
            ram = psutil.virtual_memory().total / GiB
        except Exception:
            ram = 0.0
    # --- CPU：逻辑核 = os.cpu_count；物理核 = WMI NumberOfCores（兜底 psutil/逻辑核）
    lc = os.cpu_count() or 0
    pc = lc
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum "
              "| Select-Object -ExpandProperty Sum"],
             capture_output=True, text=True, encoding="utf-8", errors="replace",
             timeout=20, creationflags=0x08000000).stdout.strip()  # CREATE_NO_WINDOW：不闪黑框
        if out.isdigit() and int(out) > 0:
            pc = int(out)
    except Exception:
        try:
            import psutil
            p = psutil.cpu_count(logical=False)
            if p:
                pc = p
        except Exception:
            pass
    # --- CPU 品牌：注册表
    brand = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            brand = winreg.QueryValueEx(k, "ProcessorNameString")[0]
    except Exception:
        pass
    # --- GPU：优先 dxdiag（跨厂商拿真实独显显存，含 8G 以上大显存卡），失败退 WMI
    gpus = _win_dxdiag_gpus()
    src = "dxdiag"
    if not gpus:
        gpus = _win_wmi_gpus()
        src = "wmi"
    hw = Hardware(ram_gb=ram, physical_cores=pc, logical_cores=lc, cpu_brand=brand,
                  gpus=gpus, ram_bw_gbps=ram_bw_override or 48.0,
                  metal_compute=False, vulkan_compute=False)
    _apply_gpu_policy(hw)
    if gpus:
        hw.gpu_probe_src = src   # 供 gui 缓存
    return hw


def _win_dxdiag_gpus() -> list:
    """dxdiag /t 文本 → GPU 列表（Dedicated Memory 为独显真实显存；WMI 4GB 上限在此可绕过）。
    虚拟显示适配器（无 Dedicated 行）自动跳过。约 10-20s，gui 层有 30 天缓存。"""
    import tempfile
    out = os.path.join(tempfile.gettempdir(), "llama-shell-dxdiag.txt")
    try:
        subprocess.run(["dxdiag", "/t", out], capture_output=True, timeout=45)
        with open(out, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return []
    finally:
        try:
            os.remove(out)
        except Exception:
            pass
    gpus: list[dict] = []
    # 只解析 "Display Adapters:" 段（到下一个顶级段 "Sound" 止）
    seg = text.split("Sound Devices:", 1)[0]
    start = seg.find("Display Adapters:")
    if start < 0:
        return []
    seg = seg[start:]
    cur_name, cur_ded, cur_shared = None, None, None
    for line in seg.splitlines():
        s = line.strip()
        if s.startswith("Card name:"):
            if cur_name is not None:
                _flush_gpu(gpus, cur_name, cur_ded, cur_shared)
            cur_name = s.split(":", 1)[1].strip()
            cur_ded = cur_shared = None
        elif s.startswith("Dedicated Memory:") and cur_name:
            cur_ded = _parse_mb(s)
        elif s.startswith("Shared Memory:") and cur_name:
            cur_shared = _parse_mb(s)
    if cur_name is not None:
        _flush_gpu(gpus, cur_name, cur_ded, cur_shared)
    # 去重（同卡名保留第一个）
    seen, out2 = set(), []
    for g in gpus:
        if g["name"] not in seen:
            seen.add(g["name"])
            out2.append(g)
    return out2


def _flush_gpu(gpus, name, dedicated_mb, shared_mb):
    if not name or "virtual" in name.lower() or "basic render" in name.lower():
        return
    # 有独显显存才是显卡（虚拟显示器/声卡等无 Dedicated 行，跳过）
    if dedicated_mb is None:
        return
    low = name.lower()
    integrated = any(k in low for k in ("integrated", "uhd", "iris", "intel", "vga"))
    gpus.append({"name": name, "vram_gb": round(0.001 * dedicated_mb, 1),
                 "integrated": integrated})


def _parse_mb(s: str) -> float | None:
    m = re.search(r"([\d.]+)\s*MB", s)
    return float(m.group(1)) if m else None


def _win_wmi_gpus() -> list:
    """兜底：Win32_VideoController。注意 AdapterRAM 在部分驱动下有 4GB 上限（如 8G 卡报 4G）。"""
    gpus: list[dict] = []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_VideoController "
                              "| Select-Object Name,AdapterRAM | ConvertTo-Json"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=20, creationflags=0x08000000).stdout
        data = json.loads(out or "[]")
        items = data if isinstance(data, list) else [data]
        for it in items:
            name = it.get("Name") or ""
            ram = it.get("AdapterRAM")
            if not name or not ram:
                continue
            vram = float(ram) / GiB
            low = name.lower()
            integrated = any(k in low for k in ("integrated", "uhd", "iris", "intel", "vga"))
            gpus.append({"name": name, "vram_gb": round(min(vram, 4.0), 1) if integrated else round(vram, 1),
                         "integrated": integrated,
                         "vram_capped": (not integrated and float(ram) <= 4294000000 and float(ram) >= 4280000000)})
    except Exception:
        pass
    return gpus


def _detect_hardware_macos(ram_bw_override: float | None = None) -> Hardware:
    def sysctl(name: str):
        try:
            out = subprocess.run(["sysctl", "-n", name],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip()
        except Exception:
            return ""
    ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True).stdout or "0") / GiB
    pc = int(sysctl("hw.physicalcpu") or "0")
    lc = int(sysctl("hw.ncpu") or "0")
    brand = sysctl("machdep.cpu.brand_string")
    gpus: list[dict] = []
    try:
        prof = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"],
                              capture_output=True, text=True, timeout=20).stdout
        data = json.loads(prof)
        for item in data.get("SPDisplaysDataType", []):
            # 顶层就是 GPU 卡：_name + spdisplays_vram（子列表 ndrvs 是内存/显示器，勿取）
            name = item.get("_name") or item.get("sppci_model") or "?"
            mem = item.get("spdisplays_vram") or item.get("spdisplays_wired")
            vram = 0.0
            m = re.search(r"([\d.]+)\s*([GM])B", str(mem)) if mem else None
            if m:
                # "8 GB" -> 8.0（单位本就是 GiB，别误除 1024）；"8192 MB" -> 8.0
                vram = float(m.group(1)) if m.group(2) == "G" else float(m.group(1)) / 1024.0
            low = name.lower()
            integrated = any(k in low for k in ("integrated", "uhd", "iris", "vga"))
            gpus.append({"name": name, "vram_gb": round(vram, 1),
                         "integrated": integrated})
    except Exception:
        pass
    for g in gpus:
        if g["integrated"]:
            g["vram_gb"] = min(g["vram_gb"], 0.5)   # iGPU 视为不可用预算
    hw = Hardware(ram_gb=ram, physical_cores=pc, logical_cores=lc,
                  cpu_brand=brand, gpus=gpus,
                  ram_bw_gbps=ram_bw_override or 48.0)
    # 显示卡有 VRAM ≠ 能算：Metal 只认可算设备，须看 SPMetalDataType。
    hw.metal_compute = _metal_compute_available()
    for g in gpus:
        if g.get("integrated"):
            # 集显 / iGPU：只有 Metal 计算可用才算能 offload
            g["usable_compute"] = hw.metal_compute
        else:
            # 独显（eGPU/dGPU）：Metal（Apple dGPU）或 Vulkan（任意独显）都能算。
            # Intel Mac 上 AMD eGPU 走 Vulkan，标 usable，不再被 Metal 一票否决。
            g["usable_compute"] = True
    hw.vulkan_compute = any((not g.get("integrated")) and g.get("vram_gb", 0) > 0.2
                            for g in gpus)
    return hw


def _metal_compute_available() -> bool:
    """本机是否存在 llama.cpp Metal 可识别的计算 GPU（非仅显示）。"""
    try:
        out = subprocess.run(
            ["system_profiler", "SPMetalDataType"],
            capture_output=True, text=True, timeout=20).stdout
        # SPMetalDataType 会列出 "Metal Devices"；空输出 = 无可算设备
        return "Metal Devices" in out or ("GPU" in out and "VRAM" in out)
    except Exception:
        return False


def _detect_hardware_generic(ram_bw_override: float | None = None) -> Hardware:
    """Linux/其它：/proc + os.cpu_count + nvidia-smi（若有）。GPU 探测尽力而为。"""
    ram, brand, pc = 0.0, "", 0
    lc = os.cpu_count() or 0
    pc = lc
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    ram = int(ln.split()[1]) / 1024 / 1024
                    break
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for ln in f:
                if ln.startswith("model name"):
                    brand = ln.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    gpus = _nvidia_smi_gpus()
    hw = Hardware(ram_gb=ram, physical_cores=pc, logical_cores=lc, cpu_brand=brand,
                  gpus=gpus, ram_bw_gbps=ram_bw_override or 48.0,
                  metal_compute=False, vulkan_compute=False)
    _apply_gpu_policy(hw)
    return hw


def _nvidia_smi_gpus() -> list:
    """nvidia-smi → GPU 列表（NVIDIA 卡；非 NVIDIA 平台返回空）。"""
    gpus: list[dict] = []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
        for ln in out.strip().splitlines():
            parts = [x.strip() for x in ln.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                gpus.append({"name": parts[0], "vram_gb": round(int(parts[1]) / 1024.0, 1),
                             "integrated": False})
    except Exception:
        pass
    return gpus


# ---------------------------------------------------------------- 模型参数解析

def _parse_size(name: str) -> tuple[float, float]:
    """从模型名解析 (总参数B, 激活参数B)。'A<N>B' 为 MoE 激活参数量。"""
    name = name.upper()
    # 激活量：A 前缀的 "<N>B"（MoE）
    active = 0.0
    am = re.search(r"A(\d+(?:\.\d+)?)\s?B", name)
    if am:
        active = float(am.group(1))
    # 总量：所有 "数字B"（含 E 前缀如 E4B）里最大的那个
    tot = 0.0
    for mm in re.finditer(r"(\d+(?:\.\d+)?)\s?B", name):
        tot = max(tot, float(mm.group(1)))
    if active and not tot:
        tot = active
    return tot, active


# 量化 bytes/param（近似，用于由文件大小反推参数量）
QUANT_BYTES = {
    1: 2.0, 2: 0.56, 3: 0.56, 7: 1.06, 13: 0.44, 14: 0.55, 15: 0.57,
    16: 0.67, 17: 0.69, 18: 0.81, 31: 0.53, 32: 2.0, 38: 0.50, 39: 0.56,
    42: 1.0,
}


def model_stats(h) -> dict:
    """把 gguf_fast.ModelHeader 归纳成推荐引擎需要的量。"""
    file_gb = h.tensor_bytes / GiB
    expert_gb = h.expert_bytes / GiB
    layers = h.layers or 0
    is_moe = (h.expert_tensor_count > 0) or (h.expert_count > 0)
    # 显示名始终用文件名（去 .gguf），避免内部名 (Staged_Tmpl 等) 污染 UI
    path = getattr(h, "path", "")
    name = os.path.basename(path)
    if name.lower().endswith(".gguf"):
        name = name[:-5]
    # 参数量推算：优先解析文件名，其次字节/量化反推
    tot, act = _parse_size(name)
    if not tot and file_gb > 0:
        bpp = QUANT_BYTES.get(h.file_type, 0.57)
        tot = file_gb * GiB / bpp / 1e9
    if is_moe and not act:
        act = tot * 0.11
    kv_bpt = getattr(h, "kv_bytes_per_token_f16", 0)
    ssm_state_gb = getattr(h, "ssm_state_bytes", 0) / GiB
    other_gb = h.other_bytes / GiB
    return {"name": name, "path": getattr(h, "path", ""), "file_gb": file_gb,
            "expert_gb": expert_gb, "other_gb": other_gb,
            "layers": layers, "is_moe": is_moe,
            "total_b": tot, "active_b": act, "kv_bpt": kv_bpt,
            "ssm_state_gb": ssm_state_gb,
            "kv_layers": getattr(h, "kv_layers", 0),
            "ssm_layers": getattr(h, "ssm_layers", 0),
            "expert_count": getattr(h, "expert_count", 0),
            "active_experts": getattr(h, "active_experts", 0),
            "mtp_layers": getattr(h, "mtp_layers", 0)}


# ---------------------------------------------------------------- 参数估算

KV_TYPE_SCALE = {"f16": 1.0, "q8_0": 0.53, "q5_1": 0.38, "q5_0": 0.36,
                 "q4_1": 0.32, "q4_0": 0.30, "iq4_nl": 0.30, "turbo4": 0.25}


def _kv_gb(st: dict, ctx: int, kv_type: str) -> float:
    """KV 缓存估算（f16 几何 × 量化折算）——estimate() 与 prefill() 共用。"""
    kv_bpt = st.get("kv_bpt")
    total_b = st.get("total_b", 0)
    if kv_bpt:
        kv_gb = kv_bpt * ctx / GiB
    else:
        per_k = 0.10 if total_b >= 25 else 0.08 if total_b >= 12 else 0.05
        kv_gb = per_k * ctx / 1024
    return kv_gb * KV_TYPE_SCALE.get(kv_type, 0.30)


def estimate(st: dict, ctx: int, kv_type: str, hw: Hardware,
             ncmoe_override: int | None = None,
             vram_reserve_gb: float = 1.0) -> dict:
    file_gb, expert_gb = st["file_gb"], st["expert_gb"]
    layers, is_moe, total_b, active_b = st["layers"], st["is_moe"], st["total_b"], st["active_b"]
    kv_bpt = st["kv_bpt"]

    vram_budget = max(0.0, hw.vram_gb - vram_reserve_gb)
    bw_vram = 384.0 if hw.has_gpu else hw.ram_bw_gbps   # 有显存按 HBM/独显带宽

    # KV（f16 几何）→ 按所选类型折算
    kv_gb = _kv_gb(st, ctx, kv_type)
    compute_gb = 0.9 + total_b * 0.012

    out: dict = {"ctx": ctx, "kv_type": kv_type, "kv_gb": round(kv_gb, 2),
                 "vram_budget_gb": round(vram_budget, 2), "warnings": []}

    if not is_moe:
        need = file_gb * 1.03 + kv_gb + compute_gb
        if hw.has_gpu and need <= vram_budget:
            out.update(mode="full_gpu", n_gpu_layers=layers, n_cpu_moe=0,
                       vram_need_gb=round(need, 2), ram_need_gb=0.7, level="ideal",
                       speed=f"~{max(4, int(bw_vram / max(0.5, file_gb)))} t/s")
            return out
        if hw.has_gpu and file_gb * 0.5 <= vram_budget and file_gb < hw.ram_gb * 0.6:
            on_gpu = max(0.0, vram_budget - kv_gb - compute_gb)
            frac_ram = min(1.0, max(0.0, (file_gb - on_gpu) / max(0.5, file_gb)))
            per_tok = file_gb * ((1 - frac_ram) / bw_vram + frac_ram / hw.ram_bw_gbps) / GiB
            out.update(mode="cpu_partial", n_gpu_layers=-1, n_cpu_moe=0,
                       vram_need_gb=round(vram_budget, 2),
                       ram_need_gb=round(file_gb - on_gpu + 2, 1),
                       level="slow",
                       speed=f"~{max(4, int(1 / max(1e-4, per_tok)))} t/s (CPU 主导)")
            return out
        if hw.has_gpu:
            out.update(mode="reject_gpu", n_gpu_layers=0, n_cpu_moe=0,
                       vram_need_gb=round(need, 2), level="no",
                       speed="纯 CPU 运行")
            out["hints"] = [
                "dense 模型超显存：试降量化档位或减 ctx；也可在自定义参数字段手写部分卸载旗标"]
            out["warnings"].append(f"GPU 余量 {vram_budget:.1f} GiB 不够，转纯 CPU")
            return out
        ram_need = file_gb + 1.5
        if ram_need > hw.ram_gb * 0.72:
            out.update(mode="reject", level="no",
                       warnings=out["warnings"] + [f"需 ~{ram_need:.1f} GiB RAM（>72% 的 {hw.ram_gb:.0f} GiB）"])
            return out
        out.update(mode="cpu_only", n_gpu_layers=0, n_cpu_moe=0,
                   vram_need_gb=0, ram_need_gb=round(ram_need, 1), level="slow",
                   speed=f"~{max(4, int(hw.ram_bw_gbps / max(0.5, file_gb)))} t/s (CPU 主导)")
        return out

    # ---- MoE：attention/shared 常驻，experts 按余量切分
    dense_gb = max(0.3, file_gb - expert_gb) if expert_gb > 0 else 1.3
    ssm_state_gb = st.get("ssm_state_gb", 0.0)
    overhead_gb = dense_gb + 0.1 + kv_gb + compute_gb + ssm_state_gb
    experts = expert_gb if expert_gb > 0 else max(0.0, file_gb - 1.3)
    gpu_exp = min(experts, max(0.0, vram_budget - overhead_gb)) if hw.has_gpu else 0.0
    cpu_exp = experts - gpu_exp
    ram_need = cpu_exp + 1.0

    if hw.has_gpu:
        if ram_need > hw.ram_gb * 0.72:
            out.update(mode="reject", level="no",
                       warnings=out["warnings"] + [f"需 ~{ram_need:.1f} GiB 主机内存（>72%）"])
            return out
        auto_ncmoe = min(layers, int(layers * (cpu_exp / experts) + 0.5)) if experts else 0
        warns = list(out["warnings"])
        if ncmoe_override is not None:
            ncmoe = max(0, min(layers, ncmoe_override))
        else:
            ncmoe = auto_ncmoe
            # auto 必须压进预算：ncmoe 偏小（VRAM 放太多）时逐层挪回 CPU 直到 vram_need≤预算
            while ncmoe < layers and (overhead_gb + experts * (1 - ncmoe / max(1, layers))) > vram_budget + 1e-6:
                ncmoe += 1
        frac = min(1.0, ncmoe / max(1, layers))
        c_exp = experts * frac
        g_exp = experts - c_exp
        vram_need_now = overhead_gb + g_exp
        if ncmoe_override is not None and vram_need_now > vram_budget + 1e-6:
            warns.append(f"强制 {ncmoe} 层进 CPU：vram_need {vram_need_now:.2f}g 超预算 {vram_budget:.1f}g，可能 OOM/换页")
        bps = file_gb / max(1.0, total_b)
        act_gb = active_b * bps
        per_tok = act_gb * ((1 - frac) / bw_vram + frac / hw.ram_bw_gbps) / GiB
        tg = 0.48 / max(1e-4, per_tok)
        tg = min(max(60.0, 384.0 / 4), max(4.0, tg))
        lvl = "slow" if frac > 0.85 else "good"
        if vram_need_now > vram_budget + 1e-6:
            lvl = "warn"  # 超显存预算：优先于 slow/good
        if ncmoe > 30:
            warns.append(f"{ncmoe} 层专家在 CPU：prefill 收益低于成本，pp 会明显变慢")
        hints = []
        free_gb = vram_budget - vram_need_now
        if ncmoe > 0 and free_gb >= 0.5:
            hints.append(
                f"experts 在 CPU 且 VRAM 余 ~{free_gb:.1f}g：vanilla 可加 "
                f"--moe-cache-mib ≤{max(256, int(free_gb * 1024))}（热专家上 GPU，提速但吃显存）")
        elif ncmoe > 0:
            hints.append("VRAM 已满，勿再加 --moe-cache-mib（会挤爆 8G）")
        out.update(mode="moe_split", n_gpu_layers=-1, n_cpu_moe=ncmoe,
                   vram_need_gb=round(overhead_gb + g_exp, 2),
                   ram_need_gb=round(c_exp + 1.0, 1), level=lvl,
                   speed=f"~{max(4, int(tg * 0.8))}-{int(tg)} t/s",
                   gpu_experts_gb=round(g_exp, 1), cpu_experts_gb=round(c_exp, 1),
                   hints=hints,
                   warnings=warns)
        return out

    # 无 GPU 的 MoE：experts 全走 CPU
    if ram_need > hw.ram_gb * 0.72:
        out.update(mode="reject", level="no",
                   warnings=out["warnings"] + [f"需 ~{ram_need:.1f} GiB 主机内存（>72% 的 {hw.ram_gb:.0f}）"])
        return out
    act_gb = active_b * (file_gb / max(1.0, total_b))
    out.update(mode="cpu_only", n_gpu_layers=0, n_cpu_moe=0,
               vram_need_gb=0, ram_need_gb=round(ram_need, 1), level="slow",
               speed=f"~{max(4, int(hw.ram_bw_gbps / max(0.5, act_gb)))} t/s (CPU，active 参数)")
    return out


# ---------------------------------------------------------------- 上下文推荐

def recommend(st: dict, hw: Hardware, ctx: int | None = None,
              kv_type: str | None = None, ncmoe: int | None = None) -> dict:
    """主入口：自动选 ctx/KV，输出完整推荐。st = model_stats(h)。"""
    ctx = ctx or 16384
    kv = kv_type or "q8_0"  # 默认档位 q8_0（显存友好、精度损失极小）
    trial = estimate(st, ctx, kv, hw, ncmoe_override=ncmoe)
    if trial.get("vram_need_gb", 0) > trial.get("vram_budget_gb", 1e9) * 0.98 and kv == "f16":
        trial2 = estimate(st, ctx, "q8_0", hw, ncmoe_override=ncmoe)
        if trial2.get("ram_need_gb", 0) <= trial.get("ram_need_gb", 1e9) + 2:
            trial = trial2
            # 用降级后 trial2 自己的警告（数字与最终 vram_need 一致），丢旧 f16 的失配 OOM 警告
            trial["warnings"] = ["KV 降为 q8_0 以保证内存余量（精度损失很小）"] + list(trial2.get("warnings", []))
    trial["model"] = st["name"]
    return trial


# 各参数 key -> flag 映射（flag 名与取值均已从 b12212 两个二进制的 --help 实测确认）
# 值为 None = 省略该 flag（保持二进制默认）
FLAG_MAP = {
    # CPU / MoE
    "threads": lambda v: ["--threads", str(v)] if v not in ("auto", "") else None,
    "cpu_moe": lambda v: ["--cpu-moe"] if v == "on" else None,
    "moe_cache_mib": lambda v: ["--moe-cache-mib", str(v)] if v not in ("0", "", "auto") else None,
    "moe_cache_cpu_share": lambda v: ["--moe-cache-cpu-share", str(v)] if v not in ("0", "0.0", "", "auto") else None,
    "batch": lambda v: ["--batch-size", str(v)],
    "ubatch": lambda v: ["--ubatch-size", str(v)],
    "cont_batching": lambda v: ["--no-cont-batching"] if v == "off" else None,
    "prio": lambda v: ["--prio", str(v)] if v not in ("normal", "") else None,
    "poll": lambda v: ["--poll", str(v)] if v not in ("50", "") else None,
    "no_warmup": lambda v: ["--no-warmup"] if v == "on" else None,
    "dynamic_moe": lambda v: ["--dynamic-moe", "on"] if v == "on" else None,
    "jinja": lambda v: ["--jinja"] if v == "on" else None,
    "agent": lambda v: ["--agent"] if v == "on" else None,
    # 上下文 / KV / FA / load
    "ctx": lambda v: ["--ctx-size", str(v)],
    "flash_attn": lambda v: ["--flash-attn", v],
    # K/V 分开设（b12212 实测 -ctk/-ctv）；GUI 旧键 cache_type 由 GUI 层迁移到 k/v
    "cache_type_k": lambda v: ["--cache-type-k", v],
    "cache_type_v": lambda v: ["--cache-type-v", v],
    # MTP（Multi-Token Prediction）投机解码，b12212 vanilla 实测支持
    "spec_type": lambda v: ["--spec-type", str(v)] if str(v) not in ("", "off", "none") else None,
    "spec_draft_model": lambda v: ["--spec-draft-model", str(v)] if str(v).strip() else None,
    "spec_draft_n_max": lambda v: ["--spec-draft-n-max", str(v)] if str(v).strip() else None,
    "spec_draft_cache_type_k": lambda v: ["--cache-type-k-draft", v] if v not in ("", "auto") else None,
    "spec_draft_cache_type_v": lambda v: ["--cache-type-v-draft", v] if v not in ("", "auto") else None,
    "cache_reuse": lambda v: ["--cache-reuse", str(v)] if v not in ("0", "") else None,
    "swa_full": lambda v: ["--swa-full"] if v == "on" else None,
    "load_mode": lambda v: ["--load-mode", v] if v != "auto" else None,
    # 多模态 / 视觉塔（mmproj）
    "mmproj": lambda v: ["--mmproj", str(v)] if str(v).strip() and str(v) not in ("auto", "off", "") else None,
    "mmproj_offload": lambda v: ["--mmproj-offload"] if v == "on" else (["--no-mmproj-offload"] if v == "off" else None),
    "mmproj_device": lambda v: ["--mmproj-device", v] if v not in ("none", "auto", "") else None,
    "image_min_tokens": lambda v: ["--image-min-tokens", str(v)] if str(v).strip() else None,
    "image_max_tokens": lambda v: ["--image-max-tokens", str(v)] if str(v).strip() else None,
    # 采样 / 惩罚 / 思考
    "temp": lambda v: ["--temp", str(v)],
    "top_k": lambda v: ["--top-k", str(v)],
    "top_p": lambda v: ["--top-p", str(v)],
    "min_p": lambda v: ["--min-p", str(v)],
    "repeat_penalty": lambda v: ["--repeat-penalty", str(v)] if v not in ("1.0", "1.00", "") else None,
    "presence_penalty": lambda v: ["--presence-penalty", str(v)] if v not in ("0", "0.0", "") else None,
    "frequency_penalty": lambda v: ["--frequency-penalty", str(v)] if v not in ("0", "0.0", "") else None,
    "seed": lambda v: ["--seed", str(v)],
    # 思考模式（Qwen3 系 thinking）
    "reasoning": lambda v: ["--reasoning", v] if v != "auto" else None,
    "reasoning_effort": lambda v: ["--reasoning-effort", v] if v != "default" else None,
    "reasoning_format": lambda v: ["--reasoning-format", v] if v != "auto" else None,
}

# 不带取值的长 flag（消解冲突时不吞下一个 token）
_BOOL_LONG_FLAGS = {"warmup", "no-warmup", "swa-full", "no-cont-batching",
                    "cont-batching", "no-kv-offload", "kv-offload",
                    "mmproj-offload", "no-mmproj-offload"}

# flag 别名族（冲突消解：用户自定义覆盖时按族去重；-c 与 --ctx-size 同族）
FLAG_ALIASES = [
    {"ctx-size", "c"}, {"threads", "t"}, {"batch-size", "b"}, {"ubatch-size", "ub"},
    {"ngl", "n-gpu-layers"},
    {"jinja"}, {"agent"},
    {"cont-batching", "cb", "no-cont-batching", "nocb"}, {"prio"}, {"poll"},
    {"warmup", "no-warmup"}, {"dynamic-moe"}, {"flash-attn", "fa"},
    {"n-cpu-moe", "ncmoe", "cpu-moe", "cmoe"}, {"n-cpu-ffn", "ncffn"},
    {"moe-cache-mib"}, {"moe-cache-cpu-share"},
    {"cache-type-k", "ctk"}, {"cache-type-v", "ctv"}, {"cache-reuse"}, {"swa-full"},
    {"load-mode", "lm"}, {"numa"}, {"kv-offload", "kvo", "no-kv-offload", "nkvo"},
    {"temp", "temperature"}, {"top-k"}, {"top-p"}, {"min-p"},
    {"repeat-penalty", "rep-penalty"}, {"presence-penalty", "pre-penalty"},
    {"frequency-penalty", "freq-penalty"}, {"seed", "s"},
    {"reasoning", "rea"}, {"reasoning-effort"}, {"reasoning-format"},
    # MTP 投机解码族（b12212 vanilla）
    {"spec-type"}, {"spec-draft-model", "model-draft"},
    {"spec-draft-n-max", "spec-ngram-mod-n-max"},
    {"cache-type-k-draft", "ctkd"}, {"cache-type-v-draft", "ctvd"},
    # 多模态 / 视觉塔族
    {"mmproj"}, {"mmproj-offload", "mmproj-no-offload", "no-mmproj-offload"},
    {"mmproj-device"}, {"image-min-tokens"}, {"image-max-tokens"},
]


def _flag_family(tok: str):
    """一个 flag token（'-c'/'--ctx-size'）所属别名族。"""
    bare = tok.lstrip("-")
    for fam in FLAG_ALIASES:
        if bare in fam:
            return frozenset(fam)
    return None


def _strip_flag_pairs(tokens: list, families: set) -> list:
    """删掉 tokens 中属于冲突族的 flag 及其取值（--flag value / -f value 形式）。"""
    out, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        fam = _flag_family(tok) if tok.startswith("-") else None
        if fam and fam in families:
            take_value = (tok.startswith("--")
                          and tok.lstrip("-") not in _BOOL_LONG_FLAGS
                          and "=" not in tok)
            i += 2 if take_value else 1
            continue
        out.append(tok)
        i += 1
    return out


def merge_custom_overrides(cmd_tokens: list, override_args: str) -> tuple[list, list]:
    """把用户自定义参数字符串并入命令。同名 flag（含别名）以用户为准：
    先删掉生成侧冲突族的 flag，再把用户参数整体追加到末尾。
    返回 (新 token 列表, 被覆盖的冲突族可读列表)。"""
    import shlex
    user = shlex.split(override_args.strip() or "")
    if not user:
        return cmd_tokens, []
    families = set()
    for tok in user:
        if tok.startswith("-"):
            fam = _flag_family(tok)
            if fam:
                families.add(fam)
    if not families:
        return cmd_tokens + user, []
    base = _strip_flag_pairs(cmd_tokens, families)
    hit = sorted("或".join(sorted(f)) for f in families)
    return base + user, hit


def build_command_clear(st: dict, binary: str,
                        port: int = 8080, host: str = "127.0.0.1",
                        override_args: str = "") -> list:
    """清空模式：命令 = 主模型加载框架（二进制 -m 模型 --host --port）+ 用户输入，
    不生成任何自动参数（无 -ngl/ctx/量化等）。"""
    import shlex
    cmd = [binary, "-m", st["path"], "--host", host, "--port", str(port)]
    user = shlex.split((override_args or "").strip() or "")
    return cmd + user


def build_command(st: dict, hw: Hardware, plan: dict, binary: str,
                  port: int = 8080, host: str = "127.0.0.1", extra: list | None = None,
                  dynamic_moe: bool = False,
                  params: dict | None = None,
                  override_args: str = "") -> list:
    """把 plan 翻译成 llama-b12212 参数（llama-server / llama-cli 通用）；
    tosh 二进制缺 moe-cache-* 旗标时自动省略。"""
    cmd = [binary, "-m", st["path"], "--host", host, "--port", str(port)]
    p = dict(params or {})
    cmoe_on = p.get("cpu_moe") == "on"
    # ngl：面板可显式指定（含预填的具体数字）；auto 时按 plan 走
    # （--n-cpu-moe 已被面板 --cpu-moe on/off 取代，不再由 plan 注入）
    ngl_param = str(p.get("ngl", "auto")).strip()
    ngl_val = plan.get("n_gpu_layers", 0)
    if ngl_param in ("不配置", "", "default"):
        # default（不配置）：完全不下发 -ngl，二进制按自身默认（-1=尽量卸载）执行
        pass
    elif ngl_param not in ("", "auto", "None"):
        # 用户/预填显式指定 ngl → 直接采用（优先于 plan）
        cmd += ["-ngl", ngl_param]
    elif ngl_val > 0:
        cmd += ["-ngl", str(ngl_val)]
    elif ngl_val == -1:
        if st.get("is_moe"):
            # MoE 拆分：-ngl 999 全层；专家是否上 CPU 由面板 --cpu-moe on/off 控制
            #（不再由 plan 注入 --n-cpu-moe；没开的话 GUI 会打 OOM 警告提示）
            cmd += ["-ngl", "999"]
        else:
            # dense cpu_partial：部分卸载（ngl=一半层），用户可再调
            half = int(st.get("layers", 0) // 2)
            if half > 0:
                cmd += ["-ngl", str(half)]
    else:
        # ngl_val == 0 → 全 CPU
        cmd += ["-ngl", "0"]
    # 面板参数（FLAG_MAP）：默认值省略；冲突族由 merge_custom_overrides 消解
    # 旧版面板键 cache_type 兼容：未拆 k/v 时映射到两个
    if "cache_type" in p and "cache_type_k" not in p and "cache_type_v" not in p:
        p = dict(p)
        p["cache_type_k"] = p.pop("cache_type")
        p["cache_type_v"] = p["cache_type_k"]
    spec_on = str(p.get("spec_type", "off")).strip() not in ("", "off", "none")
    is_tosh = "ToshLLM" in binary
    for key, val in p.items():
        # default（不配置；旧值"不配置"/空串 "" 兼容）：该参数从最终命令里整体删除，
        # 二进制按自身默认执行
        if str(val).strip() in ("default", "", "不配置"):
            continue
        if key == "dynamic_moe" and not is_tosh:
            continue
        if key in ("moe_cache_mib", "moe_cache_cpu_share") and is_tosh:
            continue  # ToshLLM 二进制无 moe-cache-* 旗标（help 实测）
        if key in ("spec_draft_model", "spec_draft_n_max") and not spec_on:
            continue  # MTP 关闭时不单独下发 draft 参数
        fn = FLAG_MAP.get(key)
        if fn is None:
            continue
        out = fn(str(val))
        if out:
            cmd += out
    if extra:
        cmd += extra
    cmd = merge_custom_overrides(cmd, override_args)[0]
    return cmd


VANILLA_BIN = "bin/llama-b12212/llama-server"
TOSH_BIN = "/Applications/ToshLLM.app/Contents/Resources/bin/llama-server"


def pick_engine(st: dict, hw: Hardware, forced: str = "auto") -> tuple:
    """返回 (engine, binary, notes)。forced: auto/vanilla/tosh（GUI 引擎单选）。
    实测（Gemma4-26B-A4B 纯 CPU）：ToshLLM 13.4 t/s > vanilla 9.3 t/s。
    - 有 Metal 计算 GPU → tosh（AutoMemoryPlan 最优切分）
    - MoE（纯 CPU 也快）→ tosh（其 CPU MoE 内核更快，dynamic-moe 默认 off）
    - 其余 → vanilla
    """
    tosh_ok = os.path.exists(TOSH_BIN)
    notes = []
    if st.get("is_moe"):
        engine = "tosh"
        reason = "MoE 模型 → ToshLLM（CPU MoE 内核实测更快）"
    elif hw.metal_compute:
        engine = "tosh"
        reason = "有 Metal 计算 GPU → ToshLLM（VRAM/CPU 最优切分）"
    else:
        engine = "vanilla"
        reason = "无 Metal 计算 GPU（显存仅显示）→ vanilla CPU 后端"
    notes.append(reason)
    if not tosh_ok:
        if sys.platform == "win32":
            notes.append("ToshLLM 仅支持 macOS；Windows 请用 win-vulkan 版 vanilla 二进制（其 MoE CPU 内核亦较快）")
            engine = "vanilla"
        else:
            notes.append("但 ToshLLM 二进制缺失 → 回退 vanilla")
            engine = "vanilla"
    if forced in ("vanilla", "tos", "tosh"):
        want = "vanilla" if forced == "vanilla" else "tosh"
        if want != engine:
            notes.append(f"用户指定 → {want}")
            engine = want
            if want == "tosh" and not tosh_ok:
                notes.append("⚠ ToshLLM 二进制缺失")
    binary = binary_for(engine)
    if st.get("mmap_ok") is False:
        notes.append("模型在 NVMe/网络卷：mmap 会失败 → 整读 RAM（需 32GiB）")
    return engine, binary, notes


def binary_for(engine: str) -> str:
    """tosh/auto(且 ToshLLM 存在) → ToshLLM 二进制；否则本地 llama-b12212。"""
    if engine in ("tosh", "auto") and os.path.exists(TOSH_BIN):
        return TOSH_BIN
    return VANILLA_BIN


def engine_binary(engine: str) -> str:
    return binary_for(engine)


# ---------------------------------------------------------------- GUI 预填（每模型自动优化默认值）

# ctx 上限：首选 65536（与用户默认一致），装不下就逐级降
CTX_PREFILL_CANDS = [65536, 32768, 16384, 8192, 4096]


def _moe_cache_mib_from_hints(hints) -> str:
    """从 hint 文本提取 --moe-cache-mib 上限（MiB）。无缓存余量则返回 ''。"""
    import re
    for h in hints or []:
        m = re.search(r"--moe-cache-mib\s*≤\s*(\d+)", h)
        if m:
            return m.group(1)
    return ""


def prefill(st: dict, hw: Hardware, kv_type: str = "q8_0") -> dict:
    """每模型自动优化预填值：尽量把模型/专家装进显存（提速），并取装得下的最大 ctx。
    返回面板 key 的 dict，GUI 在模型无持久记录时以它作 *可编辑* 默认值。

    社区经验来源（2026-11，RX 6600 8GB VRAM / macOS Metal）：
    - MoE (Qwen3-35B-A3B)：-ngl 999 + --cpu-moe on + q8_0 KV + fa=on
      → ToshLLM dynamic-moe ~24 T/s；vanilla + -b 4096 -ub 1024 → 8K prefill 222 tok/s
    - dense (Qwen2.5-27B Q4_K_M ~15.7 GiB)：8GB VRAM 装不下 → 纯 CPU，
      32GB RAM budget (72%−2G=21 GiB) 够 ctx=65536 q8_0
    - threads = 物理核数（i5-12400F → 6）比逻辑核更稳
    - MoE 大 ctx 开 flash-attn（省 KV 显存）；dense 小 ctx 交给 auto
    - MoE：-ngl 999 + --cpu-moe on；moe_cache_mib 有值时配 moe_cache_cpu_share=0.5
    - dense 全显存存得下 → -ngl=全部层数；存不下(cpu_partial) → -ngl=一半
    """
    chosen = None
    for c in CTX_PREFILL_CANDS:
        p = recommend(st, hw, ctx=c, kv_type=kv_type)
        if p.get("level") not in ("warn", "no"):
            chosen = (c, p)
            break
        chosen = (c, p)  # 回退：最小 ctx
    if chosen is None:
        c = CTX_PREFILL_CANDS[-1]
        chosen = (c, recommend(st, hw, ctx=c, kv_type=kv_type))
    c, p = chosen

    # dense 超显存（reject_gpu → 纯 CPU）：显存装不下，但主机内存往往还有余量
    # → 按 RAM 预算（72% − 2g 留白）重挑 ctx：取装得下的最大档，不白丢上下文
    if not st.get("is_moe") and p.get("mode") == "reject_gpu" and hw.has_gpu:
        ram_budget = hw.ram_gb * 0.72 - 2.0
        compute_gb = 0.9 + st.get("total_b", 0) * 0.012
        best = None
        for cand in CTX_PREFILL_CANDS:  # 已降序
            need = st["file_gb"] * 1.03 + _kv_gb(st, cand, kv_type) + compute_gb + 1.0
            if need <= ram_budget:
                best = cand
                break
        if best and best != c:
            c, p = best, recommend(st, hw, ctx=best, kv_type=kv_type)

    # 社区最佳（RX 6600 8GB）：MoE prefill-optimized -b 4096 -ub 1024；dense -b 2048 -ub 512
    _batch, _ubatch = ("4096", "1024") if st.get("is_moe") else ("2048", "512")
    pre = {
        "ctx": str(p.get("ctx", c)),
        "cache_type_k": p.get("kv_type", "q8_0"),
        "cache_type_v": p.get("kv_type", "q8_0"),
        "flash_attn": "on" if st.get("is_moe") else "auto",
        "threads": str(hw.physical_cores) if getattr(hw, "physical_cores", 0) else "auto",
        "batch": _batch,
        "ubatch": _ubatch,
        "swa_full": "off",
        "cpu_moe": "off",
    }
    if st.get("is_moe"):
        pre["ngl"] = "999"
        ncm = p.get("n_cpu_moe")
        pre["cpu_moe"] = "on" if ncm else "off"   # plan 让专家上 CPU → 开 --cpu-moe
        mib = _moe_cache_mib_from_hints(p.get("hints"))
        if mib:
            pre["moe_cache_mib"] = mib
            # vanilla：配 moe-cache-cpu-share 0.5（一半专家走 CPU share）
            # ToshLLM 无此旗标，build_command 会自动省略
            pre["moe_cache_cpu_share"] = "0.5"
    else:
        ngl = p.get("n_gpu_layers", 0)
        if ngl > 0:
            pre["ngl"] = str(ngl)          # 全层进显存
        elif ngl == -1:
            pre["ngl"] = str(int(st.get("layers", 0) // 2))  # cpu_partial：一半进显存
        else:
            pre["ngl"] = "0"              # reject / cpu_only：全 CPU
    return pre


if __name__ == "__main__":
    import gguf_fast, os, glob
    M = os.environ.get("LLAMA_SHELL_MODELS", "models")
    hw = detect_hardware()
    tosh_ok = os.path.exists(TOSH_BIN)
    print(f"HW: {hw.cpu_brand} | {hw.ram_gb:.0f}GiB | "
          f"{hw.physical_cores}C/{hw.logical_cores}T | Metal计算={hw.metal_compute} | GPUs: "
          + (", ".join(f"{g['name']} {g['vram_gb']}GiB{'(iGPU)' if g['integrated'] else ''}"
                       for g in hw.gpus) or "none"))
    print(f"Engines: vanilla={'OK' if os.path.exists('bin/llama-b12212/llama-server') else '缺失'} "
          f"tosh={'OK' if tosh_ok else '缺失'}")
    for p in sorted(glob.glob(M + "/*.gguf")):
        if "mmproj" in p or "clip" in p or "whisper" in p.lower():
            continue
        h = gguf_fast.parse_gguf_header(p)
        st = model_stats(h)
        plan = recommend(st, hw)
        engine, bin_path, eng_notes = pick_engine(st, hw)
        p = {"threads": "auto", "temp": "0.8", "top_p": "0.95",
             "top_k": "40", "flash_attn": "on", "dynamic_moe": "off"}
        cmd = build_command(st, hw, plan, bin_path, params=p)
        print(f"\n== {st['name']}  ({st['total_b']:.0f}B"
              + (f" MoE act={st['active_b']:.0f} exp={st['expert_count']}" if st["is_moe"] else " dense")
              + f")  engine={engine}")
        print(f"   {'; '.join(eng_notes)}")
        print(f"   mode={plan['mode']} level={plan['level']} ctx={plan['ctx']} kv={plan['kv_type']}")
        print(f"   ngl={plan.get('n_gpu_layers')} ncmoe={plan.get('n_cpu_moe')} "
              f"vram_need={plan.get('vram_need_gb')} ram_need={plan.get('ram_need_gb')}")
        print(f"   {plan.get('speed', '')}")
        if plan["warnings"]:
            print("   ! " + "; ".join(plan["warnings"]))
        print("   CMD: " + " ".join(cmd))
