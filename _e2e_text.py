# -*- coding: utf-8 -*-
"""E2E-A：GUI 驱动真实 ik 引擎（D:\\ik-win）端到端。
headless Tk 建 App → 选 E4B 模型 → 引擎=ik/端口 18088/ctx 8192 → _run()（真 Popen 路径）
→ 等 READY → /completion 16 token → _stop()（taskkill）→ 验证进程/端口已清。
"""
import sys, os, re, time, json, urllib.request, ctypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
if sys.platform == "win32":
    ctypes.windll.user32.SetProcessDPIAware()
import tkinter as tk
import tkinter.messagebox as mbox
import gui

PORT = 18088
ROOT = tk.Tk()
ROOT.withdraw()
gui.messagebox = mbox
for k in ("showinfo", "showwarning", "showerror"):
    setattr(mbox, k, lambda *a, **kw: None)
app = gui.App(ROOT)

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        ROOT.update(); time.sleep(0.05)

# 1) 等模型库扫完，选 E4B
target = None
for _ in range(60):
    kids = app.tree.get_children()
    if kids:
        break
    pump(0.5)
for k in app.tree.get_children():
    name = app.tree.item(k, "values")[0]
    if "Gemma-4-E4B" in name:      # 11-27: 不钉量化档（11-21 起库里是 Q6_K_P；换模型文件不该弄死门禁）
        target = k
        break
assert target, f"Gemma-4-E4B 不在模型库: {[app.tree.item(k, 'values')[0] for k in app.tree.get_children()]}"
app.tree.selection_set(target); app.tree.focus(target); app._on_select(); pump(1.0)

# 2) 引擎 ik + 端口 + ctx 8192（CPU 快路径）
app.engine_var.set("ik")
app.port_var.set(str(PORT))
app.pv["ctx"].set("8192")
app._refresh_recommend(); ROOT.update()
cmd_line = app.cmd.get("1.0", "end").strip()
print("CMD:", cmd_line[:150], "...")
assert "ik" in (app._last_cmd or ""), "命令应走 ik 引擎"
assert "-ngl" not in cmd_line, "ik 引擎不应有 -ngl"

# 3) _run() → 真 Popen（CREATE_NO_WINDOW + cwd=D:\ik-win\bin + utf-8 读线程）
app._run(); pump(0.5)
assert app.proc is not None, "进程未启动"
pid = app.proc.pid
print(f"spawned pid={pid} bin={app._running_bin}")

# 4) 等 READY（_health_poll → q → _poll → status）
t0 = time.time()
ready = False
while time.time() - t0 < 300:
    pump(0.5)
    st = app.status.cget("text")
    if st == "READY":
        ready = True
        break
    if app.proc is None:
        break
print(f"READY={ready} after {time.time()-t0:.0f}s, status={st}")
assert ready, "server 未在 300s 内就绪"

# 5) 真实对话
body = json.dumps({"prompt": "用一句简短的话介绍 llama.cpp 是什么。",
                   "max_tokens": 16, "stream": False, "temperature": 0}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion", data=body,
                             headers={"Content-Type": "application/json"})
t1 = time.time()
d = json.loads(urllib.request.urlopen(req, timeout=60).read())
out = d.get("content", "").strip()
tps = d.get("tokens_predicted", 0) / max(0.1, time.time() - t1)
print(f"REPLY({len(out)}字, {tps:.1f} t/s): {out[:80]}")
assert out, "无回复"

# 6) 停止（taskkill /F /T）+ 验证
app._stop(); pump(2.0)
time.sleep(1.5)   # 等 _reap 线程收尾
import subprocess as _sp
outp = _sp.check_output(["netstat", "-ano"], text=True, encoding="utf-8", errors="replace")
port_busy = any(f":{PORT} " in (l.split()[1] + " ") and l.split()[3] == "LISTENING"
                for l in outp.splitlines() if len(l.split()) >= 5)
alive = False
try:
    import psutil
    alive = psutil.pid_exists(pid) and psutil.Process(pid).is_running()
except Exception:
    pass
print(f"port_busy={port_busy} proc_alive={alive} status={app.status.cget('text')}")
assert not port_busy and not alive, "停止后端口/进程仍残留"

ROOT.destroy()
print("E2E-A PASS（GUI 驱动 ik 真实对话 + 干净停止）")
