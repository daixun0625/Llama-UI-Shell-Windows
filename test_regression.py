"""llama-shell 0.2 回归测试（Windows 双引擎基座 + 文生图页；每次打包前跑，全 PASS 才出货）。

覆盖 0.1 历史坑（1-10）+ 0.2 新功能门（11-16）：
  1. 按钮绑定共存（add="+" 不顶掉点击）
  2. 启动→日志滚→停止→再启动→日志继续滚
  3. 非零退出有诊断且 _poll 不断链
  4. PROC_EXIT pid 标签（旧进程迟到事件不误判）
  5. 主线程 _update_hwload 不阻塞（GPU 采样后台线程化）
  6. 引号规则（.gguf 引、二进制不引；反斜杠路径安全）
  7. Windows 双引擎基座：默认 vanilla、tosh 行隐藏、引擎单选恰为 {ik, vanilla}
  8. 清空模式（0.2 StringVar override/clear）仅框架+输入
  9. ovr 旧覆盖切模型不再静默注入（override 键不持久化）
 10. 用户停止非零退出码不打红字
 11. 0.2 参数面板 36 项齐 + FREE_EDIT_KEYS + SKIP 含 mtp/lora + MTP/LoRA 候选
 12. 引擎旗标语义：ik 剥 -ngl/--agent/--cache-reuse；MTP 引擎感知；lora 仅 vanilla；cache_ram
 13. build_image_command（-r/--strength 仅 edit_capable；lora 标签；offload >5GB）
 14. sd-cli 默认探测（Windows LocModel 引擎目录）
 15. 图片页控件齐 + 升级按钮在 engbar（无独立升级行）
"""
import sys, os, re, time, ctypes, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
if sys.platform == "win32":
    ctypes.windll.user32.SetProcessDPIAware()

import tkinter as tk
import tkinter.messagebox as mbox
import gui

FAILS = []

def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)

def fake_argv(exit_code=0):
    code = ("import sys, time\n"
            "for i in range(3):\n    print(f'FAKE_LINE{i}', flush=True)\n    time.sleep(0.4)\n"
            f"sys.exit({exit_code})")
    return [sys.executable, "-c", code]

ROOT = tk.Tk()
ROOT.withdraw()
gui.messagebox = mbox
for k in ("showinfo", "showwarning", "showerror"):
    setattr(mbox, k, lambda *a, **kw: None)

# 11-27 hermetic：回归门不得依赖 D 盘 b 版本目录是否还在（b12651 被删后
# 基线配置指向不存在的二进制 → cmd 框为空 → #8 IndexError 整门崩掉）。
# 基线配置的引擎二进制有缺失时，注入临时配置：vanilla 指现存最高 b 目录（无则
# 兜底 ik 二进制），ik 指 D:\ik-win\bin。全齐则不注入（保持原行为）。
_base_cfg = gui._load_config()
_v, _ik = _base_cfg.get("vanilla_bin") or "", _base_cfg.get("ik_bin") or ""
_vok, _ikok = bool(_v) and os.path.isfile(_v), bool(_ik) and os.path.isfile(_ik)
if not (_vok and _ikok):
    repl_v, repl_ik = _v, _ik
    if not _vok:
        for _n, _d in gui.win_vulkan_builds():
            _c = os.path.join(_d, gui._LLAMA_SRV)
            if os.path.isfile(_c):
                repl_v = _c
                break
        if not (repl_v and os.path.isfile(repl_v)):
            repl_v = r"D:\ik-win\bin\llama-server.exe"   # 兜底：门只要求可解析二进制
    if not _ikok:
        repl_ik = r"D:\ik-win\bin\llama-server.exe"
    if not (os.path.isfile(repl_v) and os.path.isfile(repl_ik)):
        raise SystemExit(f"regression: 找不到可用引擎二进制 (vanilla={repl_v} ik={repl_ik})")
    _cfg_tmp = os.path.join(tempfile.gettempdir(), "llama-shell-regtest-config.json")
    _cfg_data = dict(_base_cfg)
    _cfg_data["vanilla_bin"] = repl_v
    _cfg_data["ik_bin"] = repl_ik
    json.dump(_cfg_data, open(_cfg_tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    gui.CONFIG_FILE = _cfg_tmp
    print(f"[hermetic] 注入临时配置: vanilla={repl_v} | ik={repl_ik}")

app = gui.App(ROOT)
ROOT.update(); time.sleep(0.3)

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        ROOT.update(); time.sleep(0.05)

def out_lines(t):
    return len(re.findall(r"FAKE_LINE\d", t))

# 1) 按钮绑定共存（0.2：Button-1=点击逻辑、ButtonPress-1=手感反馈，两者并存）
b1 = lambda cv: len(re.findall(r"\[\S+<lambda>", cv.bind("<Button-1>")))
pr = lambda cv: "<ButtonPress-1>" in cv.bindtags() or cv.bind("<ButtonPress-1>") != "[]"
check("按钮绑定共存(点击+手感)", b1(app.run_cv) >= 1 and b1(app.stop_cv) >= 1
      and pr(app.run_cv) and pr(app.stop_cv),
      f"run={b1(app.run_cv)} stop={b1(app.stop_cv)}")

# 2) 完整旅程：run→log→stop→run→log
app._spawn(fake_argv()); pump(2.6)
t1 = app.log.get("1.0", "end")
check("第1次运行日志滚动", out_lines(t1) == 3, f"rows={out_lines(t1)}")
app._stop(); pump(0.5)
check("停止后 proc 清空", app.proc is None)
app._spawn(fake_argv()); pump(2.6)
t2 = app.log.get("1.0", "end")
check("停后再开日志继续刷", out_lines(t2) - out_lines(t1) == 3, f"delta={out_lines(t2)-out_lines(t1)}")

# 3) 非零退出：诊断日志且 _poll 存活
app._spawn(fake_argv(3)); pump(2.6)
t3 = app.log.get("1.0", "end")
check("非零退出有诊断日志", "进程已退出 rc=3" in t3)
app._spawn(fake_argv()); pump(2.6)
t4 = app.log.get("1.0", "end")
check("非零退出后 _poll 未断链(日志继续刷)", out_lines(t4) - out_lines(t3) == 3,
      f"delta={out_lines(t4)-out_lines(t3)}")

# 4) PROC_EXIT pid 标签（旧进程事件不误杀新进程状态）
app.proc = None
app.q.put("PROC_EXIT:99999:1")
pump(0.6)
txt = app.log.get("1.0", "end")
check("旧进程退出事件不误判当前状态",
      "旧进程事件已忽略" not in txt.split("启动")[-1] or "进程已退出 rc=1" in txt)

# 5) 主线程不阻塞（Windows GPU 采样已后台化）
t0 = time.time()
for _ in range(3):
    app._update_hwload()
dt = (time.time() - t0) / 3 * 1000
check("主线程 hwload 不卡顿(<100ms)", dt < 100, f"{dt:.0f} ms")

# 6) 引号规则（双轨：_shell_str 显示串套引号；posix shlex 解析回不带引号的原始 token）
s = gui._shell_str([r"D:\llama-vulkan-x64\llama-server.exe", "-m", r"D:\models\A-B\gguf模型.gguf",
                    r"D:/ik-win/bin/llama-server.exe"])
check("引号规则(反斜杠/中文必引,正斜杠裸路径不引)",
      '"D:\\llama-vulkan-x64\\llama-server.exe"' in s
      and '"D:\\models\\A-B\\gguf模型.gguf"' in s
      and "D:/ik-win/bin/llama-server.exe" in s and '"D:/ik-win/bin/llama-server.exe"' not in s,
      s[:120])
# 双轨不变量：显示串经 posix shlex 解析 == 原始 token 列表（Popen 实参）
import shlex as _shlex
_toks = [r"D:\ik-win\bin\llama-server.exe", "-m", r"D:\models\A-B\m.gguf", "--host", "127.0.0.1"]
check("双轨不变量(_shell_str→posix shlex 还原原始token)",
      _shlex.split(gui._shell_str(_toks)) == _toks)

# 6b) moe-cache-mib 语义 + 6c) 自由输入键
import recommend as _rec
_hw = _rec.Hardware(ram_gb=32, physical_cores=8, logical_cores=16, cpu_brand="x",
                    gpus=[{"name": "g", "vram_gb": 8.0, "integrated": False, "usable_compute": True}],
                    ram_bw_gbps=50, metal_compute=False, vulkan_compute=True)
_st = {"name": "m", "path": r"D:\models\m.gguf", "is_moe": True, "total_b": 35, "active_b": 3.3,
      "layers": 40, "file_gb": 21.0, "expert_gb": 14.0, "kv_bpt": 1.0, "mmap_ok": True, "hidden": 4096}
_pl = _rec.recommend(_st, _hw, ctx=32768, kv_type="q8_0")
c0 = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                 params={"moe_cache_mib": "0"}))
c1 = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                 params={"moe_cache_mib": "1536", "cpu_moe": "on"}))
check("moe-cache-mib=0 → 不下发旗标", "--moe-cache-mib" not in c0)
check("moe-cache-mib=1536(CPU专家在场) → 下发", "--moe-cache-mib 1536" in c1, c1[:120])
check("moe_cache_mib/cache_ram 可手输(在 FREE_EDIT_KEYS)",
      "moe_cache_mib" in gui.FREE_EDIT_KEYS and "cache_ram" in gui.FREE_EDIT_KEYS)

# 7) Windows 双引擎基座：默认 vanilla；tosh 行隐藏；引擎单选恰为 {ik, vanilla}
if sys.platform == "win32":
    check("Windows 默认引擎 = vanilla", app.engine_var.get() == "vanilla", app.engine_var.get())
    tosh_labels = [w for w in app.engbar.winfo_children()
                   if w.winfo_class() == "TLabel" and str(w.cget("text")) == "tosh:"]
    check("Windows engbar 隐藏 tosh 行", not tosh_labels)
    rb_txts = [str(w.cget("text")) for w in app.f_eng.winfo_children()
               if w.winfo_class() == "TRadiobutton"]
    eng_rbs = [t for t in rb_txts if t in ("ik", "vanilla", "tosh", "auto")]
    check("Windows 引擎单选恰为 ik/vanilla", sorted(eng_rbs) == ["ik", "vanilla"], str(rb_txts))

# 8) 清空模式（0.2 StringVar：override/clear）
kids = app.tree.get_children()
while not kids:   # 等扫描完成
    ROOT.update(); time.sleep(0.1)
    kids = app.tree.get_children()
app.tree.selection_clear(); app.tree.selection_set(kids[0]); app.tree.focus(kids[0])
app._on_select(); pump(1.0)
app.clear_mode_var.set("clear")
app.ovr.config(state="normal"); app.ovr.delete("1.0", "end")
app.ovr.insert("1.0", "-c 8192 -fa on")
app._refresh_recommend(); ROOT.update()
cmd_line = (app.cmd.get("1.0", "end").strip().splitlines() or [""])[0]
ok_clear = bool(cmd_line) and ("--host" in cmd_line and "-c 8192" in cmd_line and "-fa on" in cmd_line
            and "-ngl" not in cmd_line and "--ctx-size" not in cmd_line)
check("清空模式(仅 框架+输入，无自动参数)", ok_clear,
      "cmd 框为空（引擎二进制缺失?）" if not cmd_line else cmd_line[:120])
app.clear_mode_var.set("override")
app.ovr.delete("1.0", "end")
app._refresh_recommend(); ROOT.update()
cmd_line2 = (app.cmd.get("1.0", "end").strip().splitlines() or [""])[0]
check("切回覆盖模式(自动参数恢复)", bool(cmd_line2) and ("-ngl" in cmd_line2 or "-m " in cmd_line2),
      "cmd 框为空" if not cmd_line2 else cmd_line2[:80])

# 9) ovr 旧覆盖不再静默注入 + 不持久化 override 键
key = app.tree.item(kids[0], "values")[-1]
d = json.load(open(gui.STATE_FILE, encoding="utf-8"))
old_entry = d.get(key)
d[key] = {"params": {"ctx": "32768"}, "override": "-c 8192 -fa on",
          "engine": "vanilla", "port": "8080", "host": "127.0.0.1"}
json.dump(d, open(gui.STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
try:
    app.tree.selection_set(kids[0]); app.tree.focus(kids[0]); app._on_select()
    pump(1.2)
    ovr_txt = app.ovr.get("1.0", "end").strip()
    app._refresh_recommend(); pump(0.8)
    cmd_line = app.cmd.get("1.0", "end").strip().splitlines()[0]
    check("ovr 旧覆盖不再静默注入(切模型清空)",
          ovr_txt == "" and "-fa" not in cmd_line and "-c 8192" not in cmd_line,
          f"ovr={ovr_txt!r} cmd尾部={cmd_line[-60:]}")
    app._save_state_entry()
    d2 = json.load(open(gui.STATE_FILE, encoding="utf-8"))
    check("state 不持久化 override 键", "override" not in (d2.get(key) or {}))
finally:
    d = json.load(open(gui.STATE_FILE, encoding="utf-8"))
    if old_entry:
        d[key] = old_entry
    else:
        d.pop(key, None)
    json.dump(d, open(gui.STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)

# 10) 用户点停止 → 非零退出码是常态，不打红字（崩溃才打）
before_log = app.log.get("1.0", "end")
app._spawn(fake_argv()); pump(1.0)
app._stop(); pump(2.5)
tail_log = app.log.get("1.0", "end")[len(before_log):]
check("用户停止不打红字诊断",
      "用户停止" in tail_log and "常见原因" not in tail_log,
      tail_log.splitlines()[-3:])

# 11) 0.2 参数面板 36 项齐
expect_36 = {"ngl", "threads", "cpu_moe", "moe_cache_mib", "batch", "ubatch", "no_warmup",
             "mmproj", "mmproj_device", "image_min_tokens", "image_max_tokens", "agent",
             "ctx", "flash_attn", "cache_type_k", "cache_type_v", "spec_type",
             "spec_draft_model", "spec_draft_n_max", "spec_draft_cache_type_k",
             "spec_draft_cache_type_v", "lora", "cache_ram", "load_mode",
             "temp", "top_k", "top_p", "min_p", "repeat_penalty", "presence_penalty",
             "frequency_penalty", "seed", "repeat_last_n", "reasoning", "reasoning_effort", "jinja"}
missing = expect_36 - set(app.pv)
check("0.2 参数面板 36 项齐", not missing, f"missing={missing}")

# 12) SKIP 含 mtp/lora（附属文件不进主模型库）+ MTP/LoRA 候选构建
tmpd = tempfile.mkdtemp(prefix="lshell_")
for fn in ("real-model.gguf", "real-model-MTP.gguf", "adapter-lora.gguf", "mmproj-vision.gguf"):
    open(os.path.join(tmpd, fn), "w").close()
found = [os.path.basename(st.get("path", "")) for st in gui.scan_models(tmpd)]
check("SKIP 过滤 mtp/lora/mmproj", found == ["real-model.gguf"], str(found))
mc, md, mm = gui._build_mtp_cands(tmpd)
check("MTP 候选(off+文件)", md[0] == "off" and any("MTP" in x for x in md), str(md))
lc, ld, lm = gui._build_lora_cands(tmpd)
check("LoRA 候选(off+文件)", ld[0] == "off" and any("lora" in x for x in ld), str(ld))

# 13) 引擎旗标语义（ik 剥除 / MTP 引擎感知 / lora 仅 vanilla / cache_ram）
ik_bin = gui.IK_ABS if os.path.exists(gui.IK_ABS) else r"D:\ik-win\bin\llama-server.exe"
cmd_ik = _rec.build_command(_st, _hw, _pl, ik_bin, 8080, "127.0.0.1",
                            params={"ngl": "999", "agent": "on", "cache_reuse": "512",
                                    "spec_type": "mtp", "lora": r"D:\models\x-lora.gguf",
                                    "cache_ram": "8192"})
s_ik = " ".join(cmd_ik)
check("ik 剥 -ngl/--agent/--cache-reuse",
      "-ngl" not in s_ik and "--agent" not in s_ik and "--cache-reuse" not in s_ik, s_ik[:120])
check("MTP 引擎感知: ik → 专有语法", "--spec-type mtp:n_max=1,p_min=0.0" in s_ik, s_ik)
check("lora 门控: ik 不下发", "--lora" not in s_ik)
cmd_v = _rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                           params={"ngl": "999", "spec_type": "mtp",
                                   "lora": r"D:\models\x-lora.gguf", "cache_ram": "default"})
s_v = " ".join(cmd_v)
check("MTP 引擎感知: vanilla → draft-mtp", "--spec-type draft-mtp" in s_v, s_v)
check("lora: vanilla 下发", "--lora" in s_v)
check("cache_ram=default 不下发", "--cache-ram" not in s_v)

# 14) build_image_command（文生图/改图）
img_model = {"model": r"D:\models\image\z-image-turbo\z.gguf",
             "encoder": r"D:\models\image\common\Qwen3-4B.gguf",
             "vae": r"D:\models\image\common\ae.safetensors",
             "edit_capable": False, "loras": ""}
p_t2i = {"aspect": "1:1", "res": "1024", "cfg": "1.1", "sampler": "euler",
         "steps": "16", "seed": "1234", "format": "png",
         "prompt": "东亚女性，真实皮肤纹理", "neg": "plastic"}
ci, oi = gui.build_image_command(img_model, p_t2i, gui.IMAGE_OUT_DIR, "sd-cli.exe", offload=False)
si = " ".join(ci)
check("T2I 基础旗标齐", all(x in si for x in ("--diffusion-model", "--llm", "--vae",
                                              "--steps 16", "--cfg-scale 1.1", "-p")), si[:120])
check("非 edit 模型不传 -r/--strength", "-r " not in si and "--strength" not in si)
img_edit = dict(img_model); img_edit["edit_capable"] = True
p_i2i = dict(p_t2i); p_i2i["ref"] = r"D:\a.png"; p_i2i["strength"] = "0.5"
p_i2i["edit_capable"] = True   # 实际流程：_refresh_recommend_image 从模型条目注入到 params
ce, oe = gui.build_image_command(img_edit, p_i2i, gui.IMAGE_OUT_DIR, "sd-cli.exe", offload=True)
se = " ".join(ce)
check("I2I: edit 模型传 -r + --strength",
      re.search(r"-r D:\\a\.png", se) is not None and "--strength 0.5" in se, se)
check("offload: >5GB 自动 --offload-to-cpu", "--offload-to-cpu" in se)
p_lora = dict(p_t2i); p_lora["lora_dir"] = r"D:\models\image\loras"
p_lora["prompt"] = "<lora:x:0.8> 东亚女性"
cl, ol = gui.build_image_command(img_model, p_lora, gui.IMAGE_OUT_DIR, "sd-cli.exe", offload=False)
check("LoRA: 提示词标签 + --lora-model-dir",
      "<lora:x:0.8>" in " ".join(cl) and "--lora-model-dir" in " ".join(cl))

# 15) sd-cli 默认探测（Windows=LocModel 引擎目录；macOS=ToshLLM 自带）
check("SDCLI_DEFAULT 非空", bool(gui.SDCLI_DEFAULT), gui.SDCLI_DEFAULT)
if sys.platform == "win32":
    check("Windows sd-cli 默认路径存在", os.path.exists(gui.SDCLI_DEFAULT), gui.SDCLI_DEFAULT)

# 16) 图片页控件齐 + 升级按钮（11-23：升级行取消，「检查并升级」移入 engbar vanilla ⋯ 右边）
check("图片页控件齐", all(hasattr(app, a) for a in
      ("img_prog", "img_prog_lbl", "img_engbar", "lora_pick_dd", "img_last_lbl"))
      and {"prompt", "neg"} <= set(app.itext) and {"aspect", "res", "cfg", "steps",
       "seed", "format", "strength", "ref"} <= set(app.ipv))
check("升级按钮在engbar且无独立升级行", hasattr(app, "upg_btn")
      and app.upg_btn.master is app.engbar and not hasattr(app, "upgbar")
      and not hasattr(app, "up_ver_lbl"))
if sys.platform == "win32":
    # 11-23 主人定规：Windows 升级落盘 = D:\llama-vulkan-x64-bNNNN 版本化目录，保留最近 2 个滚动删
    vb = gui.win_vulkan_builds()
    check("Windows 版本化目录枚举", callable(getattr(gui, "win_vulkan_builds", None))
          and isinstance(vb, list) and all(isinstance(t, tuple) and len(t) == 2 for t in vb),
          [t[0] for t in vb][:3])
    check("Windows 轮次删除函数在位", callable(getattr(gui, "rotate_win_vulkan_builds", None)))

    # 17) 11-27 升级流：platform_asset win32 优先 win-vulkan（修 CPU 静默降级）
    #     + upgrade_check 进度行 / 最新资产走 vulkan（hermetic：monkeypatch _gh_get，不碰真网络）
    _rel_both = {"tag_name": "b12809", "assets": [
        {"name": "llama-b12809-bin-win-cpu-x64.zip", "browser_download_url": "https://x/cpu.zip"},
        {"name": "llama-b12809-bin-win-vulkan-x64.zip", "browser_download_url": "https://x/vulkan.zip"}]}
    _pa = gui.platform_asset(_rel_both, "win32", "x64")
    _pc = gui.platform_asset({"tag_name": "b12000", "assets": [
        {"name": "llama-b12000-bin-win-cpu-x64.zip", "browser_download_url": "https://x/cpu.zip"}]}, "win32", "x64")
    check("platform_asset win32 优先 win-vulkan（缺 vulkan 才回落 cpu）",
          bool(_pa) and _pa["asset"].endswith("win-vulkan-x64.zip") and _pa["url"] == "https://x/vulkan.zip"
          and bool(_pc) and _pc["asset"].endswith("win-cpu-x64.zip") and _pc["variant"] == "win-cpu-x64",
          f"both={_pa} | cpu-only={_pc}")
    _prog = []
    _real_gh = gui._gh_get
    gui._gh_get = lambda url, timeout=60, raw=False: (
        [_rel_both] if "/releases?per_page" in url else {"tag_name": "b12809", "assets": []})
    try:
        _info = gui.upgrade_check(progress=_prog.append)
    finally:
        gui._gh_get = _real_gh
    check("upgrade_check 进度行 + 最新资产走 win-vulkan（hermetic）",
          any("release 列表" in m for m in _prog) and _info["latest_num"] == 12809
          and _info.get("latest_url") == "https://x/vulkan.zip"
          and _info.get("latest_variant") == "win-vulkan-x64",
          f"progress={_prog} latest={_info['latest_num']} url={_info.get('latest_url')}")

ROOT.destroy()
print()
if FAILS:
    print(f"结果: {len(FAILS)} 项 FAIL → {FAILS}（不许出货）")
    sys.exit(1)
print("结果: 全 PASS ✅ 可以打包")
