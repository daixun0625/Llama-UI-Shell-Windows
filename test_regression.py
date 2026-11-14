"""llama-shell 回归测试（每次打包前跑：python test_regression.py，全 PASS 才出货）。

覆盖历次用户反馈的坑：
  1. 按钮绑定共存（视觉反馈 add="+" 不顶掉点击逻辑）
  2. 启动→日志滚→停止→再启动→日志继续滚（停再开不刷日志 bug）
  3. 非零退出走 warn 分支不杀 _poll 队列泵（_log(warn) TypeError 曾致日志永不刷新）
  4. PROC_EXIT 解析正确（切片/索引 typo 曾导致状态永久卡"运行中"）
  5. 主线程 _update_hwload 不阻塞（GPU 采样后台化，卡顿根因）
  6. 引号规则（.gguf 引、二进制不引）
  7. Windows 多引擎已删（固定 vanilla）
  8. ovr 旧覆盖切模型不再静默注入
  9. 用户停止非零退出码不打红字
"""
import sys, os, re, time, ctypes, json
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
app = gui.App(ROOT)
ROOT.update(); time.sleep(0.3)

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        ROOT.update(); time.sleep(0.05)

def out_lines(t):
    return len(re.findall(r"FAKE_LINE\d", t))

# 1) 按钮绑定共存
n_run = len(re.findall(r"\[\S+<lambda>", app.run_cv.bind("<Button-1>")))
n_stop = len(re.findall(r"\[\S+<lambda>", app.stop_cv.bind("<Button-1>")))
check("按钮绑定共存(点击+手感)", n_run >= 2 and n_stop >= 2, f"run={n_run} stop={n_stop}")

# 2) 完整旅程：run→log→stop→run→log
app._spawn(fake_argv()); pump(2.6)
t1 = app.log.get("1.0", "end")
check("第1次运行日志滚动", out_lines(t1) == 3, f"rows={out_lines(t1)}")
app._stop(); pump(0.5)
check("停止后 proc 清空", app.proc is None)
app._spawn(fake_argv()); pump(2.6)
t2 = app.log.get("1.0", "end")
check("停后再开日志继续刷", out_lines(t2) - out_lines(t1) == 3, f"delta={out_lines(t2)-out_lines(t1)}")

# 3) 非零退出：warn 分支必须正常打日志且 _poll 存活
app._spawn(fake_argv(3)); pump(2.6)
t3 = app.log.get("1.0", "end")
check("非零退出有诊断日志", "进程已退出 rc=3" in t3)
app._spawn(fake_argv()); pump(2.6)
t4 = app.log.get("1.0", "end")
check("非零退出后 _poll 未断链(日志继续刷)", out_lines(t4) - out_lines(t3) == 3,
      f"delta={out_lines(t4)-out_lines(t3)}")

# 4) PROC_EXIT 解析（旧进程事件不误杀新进程）
app.proc = None
app.q.put("PROC_EXIT:99999:1")
pump(0.6)
txt = app.log.get("1.0", "end")
check("旧进程退出事件不误判当前状态", "旧进程事件已忽略" not in txt.split("启动")[-1] or "进程已退出 rc=1" in txt)

# 5) 主线程不阻塞
t0 = time.time()
for _ in range(3):
    app._update_hwload()
dt = (time.time() - t0) / 3 * 1000
check("主线程 hwload 不卡顿(<100ms)", dt < 100, f"{dt:.0f} ms")

# 6) 引号规则
s = gui._shell_str([r"D:\llama-bin-win-vulkan-x64\llama-server.exe", "-m", r"D:\models\A-B\gguf模型.gguf"])
check("引号规则(二进制不引,gguf引)",
      r"D:\llama-bin-win-vulkan-x64\llama-server.exe" in s and '"D:\\models\\A-B\\gguf模型.gguf"' in s,
      s[:90])

# 6b) moe-cache-mib 语义：0 = 不配置此参数（旗标不下发），非零 = 下发
import recommend as _rec
class _H:  # 最小硬件桩
    pass
_hw = _rec.Hardware(ram_gb=32, physical_cores=8, logical_cores=16, cpu_brand="x",
                    gpus=[{"name": "g", "vram_gb": 8.0, "integrated": False, "usable_compute": True}],
                    ram_bw_gbps=50, metal_compute=False, vulkan_compute=True)
_st = {"name": "m", "path": r"D:\models\m.gguf", "is_moe": True, "total_b": 35, "active_b": 3.3,
      "layers": 40, "file_gb": 21.0, "expert_gb": 14.0, "kv_bpt": 1.0, "mmap_ok": True, "hidden": 4096}
_pl = _rec.recommend(_st, _hw, ctx=32768, kv_type="q8_0")
c0 = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                 params={"moe_cache_mib": "0"}))
c1 = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                 params={"moe_cache_mib": "1536"}))
check("moe-cache-mib=0 → 不下发旗标", "--moe-cache-mib" not in c0)
check("moe-cache-mib=1536 → 下发", "--moe-cache-mib 1536" in c1)

# 6c) moe_cache_mib 可手输（Combobox，5K 高分辨率下 OptionMenu 下拉不可用时能直接敲 0）
check("moe_cache_mib 可手输(在 FREE_EDIT_KEYS)", "moe_cache_mib" in gui.FREE_EDIT_KEYS)

# 7) 清空模式：命令 = 框架 + 用户输入，无 -ngl 等自动参数
_kids = ROOT.update() or app.tree.get_children()
while not _kids:   # 等扫描完成
    ROOT.update(); time.sleep(0.1)
    _kids = app.tree.get_children()
app.tree.selection_clear(); app.tree.selection_set(_kids[0]); app.tree.focus(_kids[0])
app._on_select()
pump(1.0)
app.clear_mode_var.set(True)
app.ovr.config(state="normal")
app.ovr.delete("1.0", "end")
app.ovr.insert("1.0", "-c 8192 -fa on")
app._refresh_recommend()
ROOT.update()
cmd_line = app.cmd.get("1.0", "end").strip().splitlines()[0]
ok_clear = ("--host" in cmd_line and "-c 8192" in cmd_line and "-fa on" in cmd_line
           and "-ngl" not in cmd_line and "--ctx-size" not in cmd_line)
check("清空模式(仅 框架+输入，无自动参数)", ok_clear, cmd_line[:120])
app.clear_mode_var.set(False)
app.ovr.delete("1.0", "end")
app._refresh_recommend()
ROOT.update()
cmd_line2 = app.cmd.get("1.0", "end").strip().splitlines()[0]
check("切回覆盖模式(自动参数恢复)", ("-ngl" in cmd_line2 or "-m " in cmd_line2), cmd_line2[:80])

# 8) 不配置（空项）选项：选中即从最终命令删除该参数（旧值"不配置"兼容）
cmd_nc = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                     params={"batch": "", "ctx": "", "ngl": "",
                                            "moe_cache_mib": "不配置", "load_mode": ""}))
ok_nc = ("--batch-size" not in cmd_nc and "--ctx-size" not in cmd_nc
         and "-ngl" not in cmd_nc and "--moe-cache-mib" not in cmd_nc and "--load-mode" not in cmd_nc)
check("空项(不配置) → 参数整体不出现在命令", ok_nc, cmd_nc[:100])
cmd_ja = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                     params={"jinja": "on", "agent": "on", "batch": "不配置"}))
check("jinja/agent 开关 → --jinja/--agent 下发", "--jinja" in cmd_ja and "--agent" in cmd_ja, cmd_ja[-80:])
cmd_jo = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                     params={"jinja": "off", "agent": ""}))
check("jinja off/agent 空 → 旗标不下发", "--jinja" not in cmd_jo and "--agent" not in cmd_jo)
cmd_df = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                     params={"batch": "default", "ctx": "default", "ngl": "default",
                                            "jinja": "default", "agent": "default", "load_mode": "default"}))
check("default(不配置) → 参数整体不出现在命令",
      all(x not in cmd_df for x in ("--batch-size", "--ctx-size", "-ngl", "--jinja", "--agent", "--load-mode")),
      cmd_df[:100])
# 对照：正常值仍在
cmd_v = " ".join(_rec.build_command(_st, _hw, _pl, "llama-server.exe", 8080, "127.0.0.1",
                                    params={"batch": "2048", "ctx": "32768", "ngl": "999",
                                            "moe_cache_mib": "1024", "load_mode": "mmap"}))
ok_v = ("--batch-size 2048" in cmd_v and "--ctx-size 32768" in cmd_v
        and "-ngl 999" in cmd_v and "--moe-cache-mib 1024" in cmd_v and "--load-mode mmap" in cmd_v)
check("具体值 → 参数正常下发", ok_v, cmd_v[:100])

# 7) Windows 多引擎删除
check("Windows 固定 vanilla", sys.platform != "win32" or app.engine_var.get() == "vanilla",
      app.engine_var.get())


# 8) ovr 框：保存过的旧 override 不再在切模型时静默混入命令
kids = app.tree.get_children()
if kids:
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
    finally:
        d = json.load(open(gui.STATE_FILE, encoding="utf-8"))
        if old_entry:
            d[key] = old_entry
        else:
            d.pop(key, None)
        json.dump(d, open(gui.STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)

# 9) 用户点停止 → 非零退出码是常态，不打红字诊断（崩溃才打）
before_log = app.log.get("1.0", "end")
app._spawn(fake_argv()); pump(1.0)
app._stop(); pump(2.5)
tail_log = app.log.get("1.0", "end")[len(before_log):]
check("用户停止不打红字诊断",
      "用户停止" in tail_log and "常见原因" not in tail_log,
      tail_log.splitlines()[-3:])

ROOT.destroy()
print()
if FAILS:
    print(f"结果: {len(FAILS)} 项 FAIL → {FAILS}（不许出货）")
    sys.exit(1)
print("结果: 全 PASS ✅ 可以打包")
