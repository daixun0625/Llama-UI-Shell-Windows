#!/usr/bin/env python3
"""纯 Python GGUF 头部解析器（零依赖）。

读取 GGUF 文件的 magic/version/kv 段（不解析 tensor info 段，
只按 kv_count 读完 key-value 对），提取模型架构与主要参数。

兼容两种布局：
- v2 布局：key/字符串无填充
- v3 布局：key/字符串填充到 8 字节边界
部分转换工具写出 version=3 但无填充的文件，因此对首 key 做双模式
嗅探自动判定。
"""
import re
import struct
import sys
import os

# "GGUF" 字节按小端 u32
GGUF_MAGIC = 0x46554747

_T_NAMES = {
    0: 'u8', 1: 'i8', 2: 'u16', 3: 'i16', 4: 'u32', 5: 'i32',
    6: 'f32', 7: 'bool', 8: 'str', 9: 'arr', 10: 'u64', 11: 'i64', 12: 'f64',
}
_T_FMT = {
    'u8': ('<B', 1), 'i8': ('<b', 1), 'u16': ('<H', 2), 'i16': ('<h', 2),
    'u32': ('<I', 4), 'i32': ('<i', 4), 'f32': ('<f', 4),
    'bool': ('<B', 1), 'u64': ('<Q', 8), 'i64': ('<q', 8), 'f64': ('<d', 8),
}


class _Cursor:
    """支持对齐模式的文件游标。"""

    def __init__(self, f, aligned):
        self.f = f
        self.aligned = aligned

    def _pad(self):
        if self.aligned:
            pad = (8 - self.f.tell() % 8) % 8
            self.f.read(pad)

    def read_u64(self):
        return struct.unpack('<Q', self.f.read(8))[0]

    def read_i32(self):
        return struct.unpack('<i', self.f.read(4))[0]

    def read_str(self):
        n = self.read_u64()
        if n > 1_000_000:
            # len 异常（布局错位或超大字符串）：跳偏移防卡死
            self.f.seek(n, 1)
            return f'<str {n}B (skipped)>'
        s = self.f.read(n).decode('utf-8', errors='replace')
        self._pad()
        return s

    def read_value(self, t):
        tname = _T_NAMES.get(t, 'i32')
        if t == 8:
            return self.read_str()
        if t == 9:
            et = self.read_i32()
            count = self.read_u64()
            et_name = _T_NAMES.get(et, 'i32')
            fmt, esize = _T_FMT.get(et_name, ('<i', 4))
            if count > 100000:
                # 值域异常（布局错位或超大词表类数组）：只跳偏移，不展开读取
                self.f.seek(count * esize, 1)
                return f'<arr {et_name} x{count} (skipped)>'
            return [struct.unpack(fmt, self.f.read(esize))[0] for _ in range(count)]
        fmt, size = _T_FMT.get(tname, ('<i', 4))
        return struct.unpack(fmt, self.f.read(size))[0]


def _sniff_first_key(f):
    """嗅探对齐模式：把第一个 kv 对按两种布局各读一遍，选解释合理者。

    合理的标准：type 在 0..12，且若为字符串则长度合理且 ASCII 可打印，
    并进一步要求紧随的第二个 key 长度也合理（0..512）。
    返回 'aligned' 或 'unaligned'。
    """
    kv_start = f.tell()
    best, best_score = 'unaligned', -1
    for mode in (False, True):
        try:
            f.seek(kv_start)
            cur = _Cursor(f, aligned=mode)
            key1 = cur.read_str()
            t1 = cur.read_i32()
            v1 = cur.read_value(t1)
            # 继续读第二个 key 做二次验证
            key2 = cur.read_str()
            t2 = cur.read_i32()
            score = 0
            score += 1 if 1 < len(key1) < 128 and all(c.isprintable() for c in key1) else 0
            score += 1 if 1 < len(key2) < 128 and all(c.isprintable() for c in key2) else 0
            score += 1 if t1 in _T_NAMES and 0 <= t1 <= 12 else 0
            score += 1 if t2 in _T_NAMES and 0 <= t2 <= 12 else 0
            if t1 == 8 and isinstance(v1, str):
                score += 2 if 0 < len(v1) < 128 and all(c.isprintable() for c in v1) else -3
            if score > best_score:
                best, best_score = ('aligned' if mode else 'unaligned'), score
        except Exception:  # noqa: BLE001
            continue
    f.seek(kv_start)
    return best


def parse_gguf(path, max_kv=1024):
    """解析 GGUF 文件头部，返回 {'meta','kv','version','kv_count','aligned','errors'}。"""
    out = {'meta': {}, 'kv': {}, 'version': None, 'aligned': None, 'errors': []}
    try:
        with open(path, 'rb') as f:
            magic = struct.unpack('<I', f.read(4))[0]
            if magic != GGUF_MAGIC:
                out['errors'].append(f'magic 不匹配: 0x{magic:08x}')
                return out
            version = struct.unpack('<I', f.read(4))[0]
            tensor_count = struct.unpack('<Q', f.read(8))[0]
            kv_count = struct.unpack('<Q', f.read(8))[0]
            out['version'] = version
            out['tensor_count'] = tensor_count
            out['kv_count'] = kv_count

            aligned = _sniff_first_key(f)
            out['aligned'] = aligned
            f.seek(0x18)  # kv 段起点
            cur = _Cursor(f, aligned=(aligned == 'aligned'))
            for i in range(min(kv_count, max_kv)):
                key = cur.read_str()
                t = cur.read_i32()
                if t not in _T_NAMES:
                    out['errors'].append(f'kv#{i} 类型 {t} 未知 (key={key!r})')
                    break
                out['kv'][key] = cur.read_value(t)
    except Exception as e:  # noqa: BLE001
        out['errors'].append(f'{type(e).__name__}: {e}')
    return out


def _to_int(x):
    """防御性 int：list 取首元素，非数值返回 None。"""
    if isinstance(x, (list, tuple)):
        x = x[0] if x else None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def summarize(info, size_bytes=None):
    """从 parse_gguf 结果提取人类可读摘要。"""
    kv = info['kv']
    arch = kv.get('general.architecture', '未知')
    name = kv.get('general.name', '')
    ftype = _to_int(kv.get('general.file_type'))

    ftype_map = {
        0: 'F32', 1: 'F16', 2: 'Q4_0', 3: 'Q4_1', 6: 'Q5_0', 7: 'Q5_1', 8: 'Q8_0',
        218: 'Q4_K', 219: 'Q5_K', 220: 'Q6_K', 221: 'Q8_K',
        1024: 'Q4_K_S', 1025: 'Q4_K_M', 1026: 'Q4_K_L',
        1536: 'Q5_K_S', 1537: 'Q5_K_M', 1538: 'Q5_K_L',
        2048: 'Q6_K', 2559: 'Q8_K',
    }
    quant = ftype_map.get(ftype, f'ftype={ftype}') if ftype is not None else '?'

    def kvget(suffixes):
        for sfx in suffixes:
            for key in (f'{arch}.{sfx}', f'general.{sfx}'):
                if key in kv:
                    return kv[key]
        return None

    layers = _to_int(kvget(['block_count']))
    embd = _to_int(kvget(['embedding_length']))
    n_head = _to_int(kvget(['attention.head_count']))
    n_kv = _to_int(kvget(['attention.head_count_kv', 'attention.kv_head_count']))
    ctx_len = _to_int(kvget(['context_length', 'context_len']))
    experts = _to_int(kvget(['ffn_expert_count', 'n_experts']))
    n_active = _to_int(kvget(['ffn_expert_count_active', 'n_active_experts']))
    head_dim = (embd // n_head) if (embd and n_head) else None

    out_kv = None
    if layers and n_kv and head_dim:
        # K+V 两张量 * layers * kv_heads * head_dim * bytes/elem
        kv_bytes_per_tok = 2 * layers * n_kv * head_dim
        out_kv = {
            'layers': layers, 'embd': embd,
            'n_head': n_head, 'n_kv': n_kv, 'head_dim': head_dim,
            'ctx_len': ctx_len, 'experts': experts, 'n_active': n_active,
            'kv_bytes_per_tok': kv_bytes_per_tok,
            'kv_gb_8k_f16': round(kv_bytes_per_tok * 8192 * 4 / 1e9, 2),
            'kv_gb_8k_q8': round(kv_bytes_per_tok * 8192 * 1 / 1e9, 2),
        }
        bpp = {
            'F16': 2.0, 'Q4_0': 0.5, 'Q4_1': 0.5625, 'Q5_0': 0.6875,
            'Q5_1': 0.75, 'Q8_0': 1.0625,
            'Q4_K_S': 0.546875, 'Q4_K_M': 0.578125, 'Q4_K_L': 0.609375,
            'Q5_K_S': 0.71875, 'Q5_K_M': 0.78125, 'Q5_K_L': 0.84375,
            'Q6_K': 0.9375, 'Q8_K': 1.0625,
        }
        if size_bytes:
            if quant in bpp:
                out_kv['est_params_b'] = round(size_bytes / bpp[quant] / 1e9, 1)
            else:
                # 量化未知时按 Q4_K_M 近似（本库主流量化），并标注
                out_kv['est_params_b'] = round(size_bytes / 0.578125 / 1e9, 1)
                out_kv['est_params_b_note'] = 'approx (quant unknown, assume Q4_K_M)'

    # MoE 检测
    is_moe = bool(experts) or bool(n_active)
    active_hint = None
    import re
    m = re.search(r'-(\d+(?:\.\d+)?)B-A(\d+(?:\.\d+)?)B', name or arch)
    if m:
        active_hint = f'{m.group(2)}B active'
    elif n_active:
        hidden_per_expert = None
        active_hint = f'{n_active} 个专家激活'

    return {
        'arch': arch, 'name': name, 'quant': quant,
        'size_mb': round(size_bytes / 1e6, 1) if size_bytes else None,
        'is_moe': is_moe, 'active_hint': active_hint,
        'details': out_kv, 'kv': kv,
    }


if __name__ == '__main__':
    for p in sys.argv[1:]:
        sz = os.path.getsize(p) if os.path.exists(p) else None
        info = parse_gguf(p)
        s = summarize(info, sz)
        print(f'\n=== {os.path.basename(p)} ({(sz or 0)/1e9:.2f} GB) ===')
        print(f"  arch={s['arch']}  quant={s['quant']}  aligned={info['aligned']}  "
              f"kv={len(info['kv'])}/{info.get('kv_count')}")
        print(f"  name={s['name']!r}  moe={s['is_moe']}  active={s['active_hint']}")
        d = s['details'] or {}
        if d:
            print(f"  layers={d.get('layers')} embd={d.get('embd')} "
                  f"heads={d.get('n_head')}/kv={d.get('n_kv')} head_dim={d.get('head_dim')}")
            print(f"  ctx_len={d.get('ctx_len')} experts={d.get('experts')}")
            print(f"  KV@8k: f16≈{d.get('kv_gb_8k_f16')}GB  q8≈{d.get('kv_gb_8k_q8')}GB")
        if sz:
            bpp = {'Q4_K_S': 0.546875, 'Q4_K_M': 0.578125, 'Q4_K_L': 0.609375,
                   'Q5_K_M': 0.78125, 'Q6_K': 0.9375, 'Q8_K': 1.0625}
            bpp_used = bpp.get(s['quant'], 0.578125)
            approx = s['quant'] not in bpp or d.get('est_params_b_note')
            print(f"  参数≈{round(sz / bpp_used / 1e9, 1)}B{'(approx)' if approx else ''}"
                  + (f"  [{d['est_params_b_note']}]" if d.get('est_params_b_note') else ''))
        if info['errors']:
            print(f"  errors: {info['errors'][:3]}")
