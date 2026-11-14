#!/usr/bin/env python3
"""gguf_fast.py — 快速 GGUF 解析（新/旧规格自动探测，纯 open().read()，无 mmap）

新规格（2025+ 量化模型，gguf 0.19 支持）：
  header: magic u32, version u32, tensor_count u64, kv_count u64
  KV: key(string: u64 len + bytes), value_type u32, value
  类型码: 0=u8 1=i8 2=u16 3=i16 4=u32 5=i32 6=f32 7=bool
          8=string(u64 len + bytes) 9=array(u32 elem_type + u64 cnt + elems)
          10=u64 11=i64 12=f64
  tensor: name(u64 len + bytes), n_dims u32, dims u64[n], dtype u32, offset u64

旧规格（老模型）：value_type u8，类型码 0..9（5=str 6/7/8=array 9=str_array），
string 长度同样是 u64。

NVMe 外接盘在 kernel sandbox 中 mmap 报 EINVAL → 一律 open().read()。
前缀读 64MB；tensor 表若跨出前缀，按偏移从盘上续读。
"""
import os, re, struct

# ggml 类型 → (块元素数, 块字节)；主源=官方 gguf 库，兜底=同一表的硬编码快照
_KBPP = {0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22),
         7: (32, 24), 8: (32, 34), 9: (32, 40), 10: (256, 84), 11: (256, 110),
         12: (256, 144), 13: (256, 176), 14: (256, 210), 15: (256, 292),
         16: (256, 66), 17: (256, 74), 18: (256, 98), 19: (256, 50),
         20: (32, 18), 21: (256, 110), 22: (256, 82), 23: (256, 136),
         24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8),
         29: (256, 56), 30: (1, 2), 34: (256, 54), 35: (256, 66),
         39: (32, 17), 40: (64, 36), 41: (128, 18)}
try:
    from gguf.constants import GGML_QUANT_SIZES as _OQS
    _KBPP = {int(t): (v, b) for t, (v, b) in _OQS.items()}
except Exception:
    pass


def _tensor_bytes(dt, elems):
    """元素数 → 字节（块量化按块向上取整）；未知 dt 返回 0"""
    e = _KBPP.get(dt)
    if not e:
        return 0
    v, blk = e
    return (elems // v) * blk + (1 if elems % v else 0) * blk

# 新规格：值推进。voff=值起点（type 字段之后）
def _adv_new(buf, voff, t):
    n = len(buf)
    if t in (0, 1, 7): return voff + 1 if voff + 1 <= n else -1
    if t in (2, 3): return voff + 2 if voff + 2 <= n else -1
    if t in (4, 5, 6): return voff + 4 if voff + 4 <= n else -1
    if t in (10, 11, 12): return voff + 8 if voff + 8 <= n else -1
    if t == 8:
        if voff + 8 > n: return -1
        ln = struct.unpack_from('<Q', buf, voff)[0]
        if ln > 100_000_000: return -1
        return voff + 8 + ln if voff + 8 + ln <= n else -1
    if t == 9:
        if voff + 12 > n: return -1
        itype = struct.unpack_from('<I', buf, voff)[0]
        cnt = struct.unpack_from('<Q', buf, voff + 4)[0]
        off = voff + 12
        for _ in range(min(cnt, 5_000_000)):
            off = _adv_new_elem(buf, off, itype)
            if off == -1: return -1
        return off
    return -1

def _adv_new_elem(buf, off, itype):
    n = len(buf)
    if itype in (0, 1, 7): return off + 1 if off + 1 <= n else -1
    if itype in (2, 3): return off + 2 if off + 2 <= n else -1
    if itype in (4, 5, 6): return off + 4 if off + 4 <= n else -1
    if itype in (10, 11, 12): return off + 8 if off + 8 <= n else -1
    if itype == 8:
        if off + 8 > n: return -1
        ln = struct.unpack_from('<Q', buf, off)[0]
        if ln > 1_000_000: return -1
        return off + 8 + ln if off + 8 + ln <= n else -1
    return -1

# 旧规格：值推进。voff=值起点
def _adv_old(buf, voff, t):
    n = len(buf)
    if t in (0, 1): return voff + 1 if voff + 1 <= n else -1
    if t in (2, 3, 4): return voff + 4 if voff + 4 <= n else -1
    if t == 5:
        if voff + 8 > n: return -1
        ln = struct.unpack_from('<Q', buf, voff)[0]
        if ln > 1_000_000: return -1
        return voff + 8 + ln if voff + 8 + ln <= n else -1
    if t in (6, 7, 8):
        if voff + 8 > n: return -1
        cnt = struct.unpack_from('<Q', buf, voff)[0]
        return voff + 8 + 4 * min(cnt, 50_000_000) if voff + 8 + 4 * min(cnt, 50_000_000) <= n else -1
    if t == 9:
        if voff + 8 > n: return -1
        cnt = struct.unpack_from('<Q', buf, voff)[0]
        off = voff + 8
        for _ in range(min(cnt, 500_000)):
            if off + 8 > n: return -1
            ln = struct.unpack_from('<Q', buf, off)[0]
            if ln > 1_000_000 or off + 8 + ln > n: return -1
            off += 8 + ln
        return off
    return -1

def _str(buf, off):
    """string 字段: u64 len + bytes → (text, new_off, ok)"""
    n = len(buf)
    if off + 8 > n: return "", off + 8, False
    ln = struct.unpack_from('<Q', buf, off)[0]
    if ln > 100_000_000: return "", off + 8, False
    if off + 8 + ln > n: return "", off + 8 + ln, False
    return bytes(buf[off + 8:off + 8 + ln]).decode('utf-8', 'replace'), off + 8 + ln, True

def _walk(buf, kv_count, spec, probe=False):
    """走查 KV 段（probe=True 只走前 3 条用于规格探测）。返回 (end_off, broken)"""
    n = len(buf)
    limit = 3 if probe else kv_count
    off = 24
    for _ in range(min(limit, kv_count)):
        key, off, ok = _str(buf, off)
        if not ok or off >= n:
            return off, True
        if spec == 'new':
            if off + 4 > n: return off, True
            t = struct.unpack_from('<I', buf, off)[0]
            off = _adv_new(buf, off + 4, t)
        else:
            off = _adv_old(buf, off + 1, buf[off])
        if off == -1:
            return off, True
    return off, False

def _detect_spec(buf, kv_count):
    for spec in ('new', 'old'):
        off, broken = _walk(buf, kv_count, spec, probe=True)
        if not broken:
            return spec
    return 'new'

def _scalar(buf, t, voff, spec):
    """按 spec 读标量/字符串值"""
    n = len(buf)
    if spec == 'new':
        if t == 8:
            if voff + 8 > n: return None
            ln = struct.unpack_from('<Q', buf, voff)[0]
            if ln > 1_000_000: return None
            return bytes(buf[voff + 8:voff + 8 + ln]).decode('utf-8', 'replace')
        if t in (4, 5, 10, 11):
            v = struct.unpack_from('<i', buf, voff)[0]
            return v
        if t in (6, 12):
            return struct.unpack_from('<f', buf, voff)[0] if t == 6 else struct.unpack_from('<d', buf, voff)[0]
        if t == 7: return bool(buf[voff])
        if t in (0, 1): return buf[voff]
        if t in (2, 3): return struct.unpack_from('<i', buf, voff)[0] if t == 3 else struct.unpack_from('<I', buf, voff)[0]
        return None
    else:
        if t == 5:
            if voff + 8 > n: return None
            ln = struct.unpack_from('<Q', buf, voff)[0]
            if ln > 1_000_000: return None
            return bytes(buf[voff + 8:voff + 8 + ln]).decode('utf-8', 'replace')
        if t in (2, 3, 4):
            return struct.unpack_from('<i', buf, voff)[0]
        if t == 1: return bool(buf[voff])
        if t == 0: return buf[voff]
        return None

from dataclasses import dataclass, field

@dataclass
class ModelHeader:
    path: str = ""
    arch: str = ""
    model_name: str = ""
    layers: int = 0
    embed: int = 0
    n_head: int = 0
    head_kv: int = 0
    key_len: int = 0
    value_len: int = 0
    expert_count: int = 0
    expert_shared: int = 0
    active_experts: int = 0
    full_attn_interval: int = 1
    ctx: int = 0
    ctx_train: int = 0
    file_type: int = -1
    size_gb: float = 0.0
    tensor_count: int = 0
    tensor_bytes: int = 0
    expert_tensor_count: int = 0
    expert_bytes: int = 0
    other_bytes: int = 0
    kv_bytes_per_token_f16: int = 0
    kv_layers: int = 0
    ssm_layers: int = 0
    ssm_state_bytes: int = 0
    mtp_layers: int = 0
    spec: str = "new"
    kv_walk_ok: bool = True
    first_tensors: list = field(default_factory=list)
    unknown_dts: set = field(default_factory=set)
    kv_keys: list = field(default_factory=list)
    kv_vals: dict = field(default_factory=dict)
    # b12212 tensor entry 尾部 u64 data_offset（张量数据在文件内的绝对偏移）
    first_offsets: list = field(default_factory=list)   # [(tensor_name, data_off)] 前 4 个
    last_data_off: int = None                            # 上一张量 data_off（连续性检查）
    last_tensor_bytes: int = 0                            # 上一张量字节数
    contig_violations: int = 0                            # 相邻 data_off 不连续的次数（应为 0）
    span_end: int = 0                                     # max(data_off + bytes)，≈ 文件大小

QUANT_NAMES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0",
               8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M",
               13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S",
               17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS",
               21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S",
               25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
               29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
               36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4_MOE", 39: "NVFP4",
               40: "Q1_0", 41: "Q2_0", 42: "F8_E4M3"}

def _derive_experts(h):
    """从 kv_vals 里启发式找专家数（兼容 qwen3_moe 的 n_expert*、新式 .ffn_expert_*、gemma4 等）"""
    for k, v in h.kv_vals.items():
        if not isinstance(v, int):
            continue
        lk = k.lower()
        if 'expert' not in lk and 'experts' not in lk and 'moe' not in lk:
            continue
        if 'shared' in lk:
            h.expert_shared = v
        elif 'active' in lk or 'used' in lk:
            h.active_experts = v
        elif lk.split('.')[-1].rstrip('s') in ('expert', 'n_expert', 'num_expert', 'expert_count',
                                               'ffn_expert_count', 'experts'):
            h.expert_count = max(h.expert_count, v)
        elif 'expert_count' in lk or 'n_expert' == lk.split('.')[-1]:
            h.expert_count = max(h.expert_count, v)


def quant_label(ft):
    if ft in QUANT_NAMES:
        return QUANT_NAMES[ft]
    return f"ft{ft}"

_FIELD_MAP = {
    ".block_count": "layers",
    ".embedding_length": "embed",
    ".attention.head_count": "n_head",
    ".attention.head_count_kv": "head_kv",
    ".attention.key_length": "key_len",
    ".attention.value_length": "value_len",
    ".full_attention_interval": "full_attn_interval",
    ".ffn_expert_count": "expert_count",
    ".ffn_expert_shared_count": "expert_shared",
    ".ffn_expert_used_count": "active_experts",
    ".context_length": "ctx",
    ".context_length_training": "ctx_train",
}

def parse_gguf_header(path, max_read_mb=64):
    fs = os.path.getsize(path)
    buf = open(path, 'rb').read(max_read_mb * 1048576)
    n = len(buf)
    h = ModelHeader(path=path, size_gb=fs / 1073741824)
    tensor_count, kv_count = struct.unpack_from('<QQ', buf, 8)
    h.tensor_count = tensor_count

    spec = _detect_spec(buf, kv_count)
    h.spec = spec

    # ---------- KV 段 ----------
    off = 24
    broken = False
    for i in range(kv_count):
        key, off, ok = _str(buf, off)
        if not ok or off >= n:
            broken = True
            break
        if spec == 'new':
            if off + 4 > n:
                broken = True
                break
            t = struct.unpack_from('<I', buf, off)[0]
            voff = off + 4
            off2 = _adv_new(buf, voff, t)
        else:
            t = buf[off]
            voff = off + 1
            off2 = _adv_old(buf, voff, t)
        if off2 == -1:
            broken = True
            break
        h.kv_keys.append(key)
        if not key:
            off = off2
            continue
        # 提取值（仅小值；大数组跳过但保持推进）
        if spec == 'new' and t == 9:
            pass
        elif spec == 'old' and t in (6, 7, 8, 9):
            pass
        else:
            v = _scalar(buf, t, voff, spec)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                h.kv_vals[key] = v
            if key == "general.architecture" and isinstance(v, str):
                h.arch = v
            elif key == "general.name" and isinstance(v, str):
                h.model_name = v
            elif key == "general.file_type" and isinstance(v, int):
                h.file_type = v
            else:
                for suf, attr in _FIELD_MAP.items():
                    if key.endswith(suf):
                        if isinstance(v, int):
                            setattr(h, attr, v)
                        break
        off = off2
    h.kv_walk_ok = not broken
    _derive_experts(h)

    # ---------- 缺失字段兜底：顺序扫描 300MB 前缀 ----------
    arch = h.arch or _guess_arch(os.path.basename(path))
    h.kv_vals['_arch_guess'] = arch
    want = []
    if not h.arch: want.append("general.architecture")
    if h.file_type < 0: want.append("general.file_type")
    for suf, attr in _FIELD_MAP.items():
        full = f"{arch}{suf}"
        if not getattr(h, attr, 0) and not any(k == full for k in h.kv_vals):
            want.append(full)
    if want:
        _recover_fields(h, path, want, spec)
        _derive_experts(h)

    if not h.full_attn_interval:
        h.full_attn_interval = 1
    if not h.head_kv:
        h.head_kv = h.n_head
    if not h.key_len:
        h.key_len = h.embed // max(1, h.n_head)
    if not h.value_len:
        h.value_len = h.embed // max(1, h.head_kv)

    # ---------- tensor 表（紧跟 KV 段之后，无重复计数） ----------
    toff, broken2 = _walk(buf, kv_count, spec)
    off = toff
    tcnt = tensor_count
    if off + 8 > n:
        _scan_tensors_from_disk(h, path, off, remaining=tcnt)
    else:
        _scan_tensor_table(h, buf, off, tcnt)

    # KV cache 字节/token（f16）
    # 混合注意力模型（full_attention_interval>1，如 Qwen3-Next：每 N 层 1 层 full-attn，
    # 其余走 SSM 线性注意力）只有部分层持有 KV，其余层的 SSM 状态是固定小缓冲、不随 ctx 增长。
    # 若按全部层算 KV 会把缓存高估 N 倍（本例 N=4 → 高估 4 倍），导致切分预测偏保守。
    h.full_attn_interval = h.full_attn_interval or 1
    kv_layers = -(-h.layers // h.full_attn_interval) if (h.layers and h.full_attn_interval > 1) else h.layers
    ssm_layers = (h.layers - kv_layers) if (h.layers and h.full_attn_interval > 1) else 0
    h.kv_layers = kv_layers
    h.ssm_layers = ssm_layers
    if h.layers:
        h.kv_bytes_per_token_f16 = 2 * kv_layers * h.head_kv * h.value_len * 2
        # SSM/mamba2 固定状态：conv 缓冲 + 递归状态（不随 ctx 变）。粗估每层约 1MiB 起。
        def _ssm(suffix, default):
            for k, v in h.kv_vals.items():
                if k.lower().endswith(".ssm." + suffix) and isinstance(v, (int, float)):
                    return int(v)
            return default
        ssm_inner = _ssm("inner_size", h.embed)
        ssm_state = _ssm("state_size", 128)
        ssm_conv = _ssm("conv_kernel", 4)
        h.ssm_state_bytes = ssm_layers * (ssm_state + ssm_conv) * ssm_inner * 2
    return h

def _recover_fields(h, path, want, spec):
    """在 300MB 前缀内顺序 find() 缺失 key，解析紧随其后的值"""
    data = open(path, 'rb').read(300 * 1048576)
    for key in want:
        idx = data.find(key.encode())
        if idx == -1:
            continue
        after = idx + len(key.encode())
        if spec == 'new':
            t = struct.unpack_from('<I', data, after)[0]
            voff = after + 4
        else:
            t = data[after]
            voff = after + 1
        v = _scalar(data, t, voff, spec)
        if key == "general.architecture":
            h.arch = v or h.arch
        elif key == "general.file_type":
            if isinstance(v, int) and 0 <= v < 128:
                h.file_type = v
        else:
            for suf, attr in _FIELD_MAP.items():
                if key.endswith(suf) and isinstance(v, int):
                    setattr(h, attr, v)
                    h.kv_vals[key] = v

def _scan_tensors_from_disk(h, path, off, remaining=None):
    """tensor 表续读（内存前缀读完还没扫完时）：从 off 起整表读盘一次解析
    （表本身很小：866 entry ≈ 80KB；上限保护 512MB）"""
    if remaining is None:
        remaining = h.tensor_count
    cap = min(remaining * 512, 512 * 1048576)
    f = open(path, 'rb')
    f.seek(off)
    tbl = f.read(cap)
    f.close()
    base, i = 0, 0
    n = len(tbl)
    while i < remaining:
        key, off2, ok = _str(tbl, base)
        if not ok or not key or off2 >= n:
            break
        nxt = _consume_tensor_entry(h, tbl, off2, key)
        if nxt == -1 or nxt >= n:
            break
        base = nxt
        i += 1

def _consume_tensor_entry(h, buf, off, key):
    """在 buf 的 off（相对）处解析一个 tensor entry（key 已读），累加到 h，返回下一个 entry 的 off。

    b12212 规范：nd(u32) dims(8nd) dt(u32) data_off(u64)。
    data_off = 该张量数据在文件内的绝对偏移（首张量=0）。
    用相邻 entry 的 data_off 做连续性验证（违例数入 h.contig_violations），
    并记录数据段理论终点 h.span_end（≈文件大小，验证 total_bytes 正确性）。
    """
    n = len(buf)
    if off + 4 > n:
        return -1
    nd = struct.unpack_from('<I', buf, off)[0]
    off += 4
    if nd > 8:
        return -1
    if off + 8 * nd > n:
        return -1
    dims = list(struct.unpack_from(f'<{nd}Q', buf, off)) if nd else [0]
    off += 8 * nd
    if off + 12 > n:
        return -1
    dt = struct.unpack_from('<I', buf, off)[0]
    data_off = struct.unpack_from('<Q', buf, off + 4)[0]
    off += 12
    elems = 1
    for d in dims:
        elems *= d
    b = _tensor_bytes(dt, elems)
    h.unknown_dts.add(dt) if not _KBPP.get(dt) else None
    if ('_exps' in key or '.exps.' in key):
        h.expert_bytes += b
        h.expert_tensor_count += 1
    else:
        h.other_bytes += b
    h.tensor_bytes += b
    if len(h.first_tensors) < 4:
        h.first_offsets.append((key, data_off))
    if h.last_data_off is not None and data_off != h.last_data_off + h.last_tensor_bytes:
        h.contig_violations += 1
    h.last_data_off = data_off
    h.last_tensor_bytes = b
    h.span_end = max(h.span_end, data_off + b)
    return off

def _scan_tensor_table(h, buf, off, tcnt):
    limit = min(tcnt, 500_000)
    i = 0
    while i < limit:
        key, off2, ok = _str(buf, off)
        if not ok or not key or off2 >= len(buf):
            break
        if i < 4:
            h.first_tensors.append(key)
        nxt = _consume_tensor_entry(h, buf, off2, key)
        if nxt == -1:
            break
        off = nxt
        i += 1
        if re.search(r'\b(nextn|mtp)\.', key):
            h.mtp_layers = 1
        if nxt >= len(buf):
            break
    if i < tcnt:
        _scan_tensors_from_disk(h, h.path, off, remaining=tcnt - i)

def _guess_arch(name):
    nl = name.lower()
    if 'arete' in nl or 'qwen3.8' in nl or 'qwen3.6' in nl or 'qwen3.5' in nl:
        return "qwen35"
    if 'gemma4' in nl or 'gemma-4' in nl:
        return "gemma3n"
    if 'gemma3' in nl:
        return "gemma3"
    if 'llama' in nl:
        return "llama"
    return ""

if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        h = parse_gguf_header(p)
        print(f"{os.path.basename(p)}: arch={h.arch} L={h.layers} ft={h.file_type}({quant_label(h.file_type)}) "
              f"tensor={h.tensor_bytes/2**30:.2f}GB exp={h.expert_bytes/2**30:.2f}GB "
              f"kv/Ktok={h.kv_bytes_per_token_f16/1048576:.5f}GB spec={h.spec} walk_ok={h.kv_walk_ok}")
