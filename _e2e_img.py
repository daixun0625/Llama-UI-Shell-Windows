# -*- coding: utf-8 -*-
"""E2E-B：GUI 驱动真实文生图（LocModel sd-cli + z-image-turbo）。
headless App → 选 z-image-turbo → steps=8/768/seed=1234 → _run_image()（真 Popen sd-cli.exe）
→ 等进程退出/日志 Saved → 验证 PNG 落盘。
"""
import sys, os, time, glob, ctypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
if sys.platform == "win32":
    ctypes.windll.user32.SetProcessDPIAware()
import tkinter as tk
import tkinter.messagebox as mbox
import gui

OUT = os.path.join(os.environ.get("TMP", "."), "_e2e_img_out")
os.makedirs(OUT, exist_ok=True)

ROOT = tk.Tk(); ROOT.withdraw()
gui.messagebox = mbox
for k in ("showinfo", "showwarning", "showerror"):
    setattr(mbox, k, lambda *a, **kw: None)
app = gui.App(ROOT)

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        ROOT.update(); time.sleep(0.05)

# 1) 图片页 + 等 image 扫描完成（__IMG_SCAN_DONE__ → _poll → _fill_tree_image）
app._switch_page("image")
t0 = time.time()
while time.time() - t0 < 120:
    pump(0.5)
    kids = app.tree.get_children()
    # image 节点特征：第 3 列 = 文生图（文字模型是别的 tag）
    if kids and str(app.tree.item(kids[0], "values")[2]) == "文生图":
        break
kids = app.tree.get_children()
print(f"img tree children={len(kids)}")
assert kids, "image 模型库为空"
def _is_z(k):
    v = str(app.tree.item(k, "values")).lower()
    return ("z-image" in v) or ("z image" in v)

# 选 z-image-turbo（iid 可能随重填失效 → 选中后校验）
kids = app.tree.get_children()
zid = next((k for k in kids if _is_z(k)), None) or (kids[0] if kids else None)
assert zid, "image 模型库为空"
app.tree.selection_set(zid); app.tree.focus(zid); app._on_select(); pump(1.5)
kids = app.tree.get_children()
zid = next((k for k in kids if _is_z(k)), kids[0])
app.tree.selection_set(zid); app.tree.focus(zid); app._on_select()
print("picked:", app.tree.item(zid, "values")[0])

# 2) 参数：LocModel 引擎 + 快速档
app.img_engine_var.set(r"C:\Users\dx\AppData\Local\LocModel\engines\sd\master-1106-532d2c1-vulkan\sd-cli.exe")
app.ipv["steps"].set("8")
app.ipv["res"].set("768")
app.ipv["seed"].set("1234")
app.img_out_var.set(OUT)
app._refresh_recommend_image(); ROOT.update()
print("IMG CMD:", app.cmd.get("1.0", "end").strip()[:160])

# 3) 真启动
before = set(glob.glob(os.path.join(OUT, "*.png")))
app._run_image()
pump(1.0)
assert app.proc is not None, "sd-cli 未启动"
print("spawned pid", app.proc.pid)

# 4) 等完成
t1 = time.time()
done = False
last = ""
while time.time() - t1 < 420:
    pump(0.5)
    txt = app.log.get("1.0", "end")
    if txt != last:
        last = txt
        tail = txt.strip().splitlines()
        if tail:
            print("LOG:", tail[-1][:120])
    if app.proc is None:
        logtxt = app.log.get("1.0", "end")
        if "[图片] 完成(rc=0)" in logtxt:
            done = True
            break
print(f"done={done} after {time.time()-t1:.0f}s")

after = set(glob.glob(os.path.join(OUT, "*.png")))
new = sorted(after - before)
print("new pngs:", new)
assert done and new, "未产出新图"
sz = os.path.getsize(new[0])
print(f"png size={sz} bytes")
assert sz > 10000, "图太小（疑似失败占位）"
ROOT.destroy()
print("E2E-B PASS（GUI 驱动 LocModel 真实出图）:", new[0])
