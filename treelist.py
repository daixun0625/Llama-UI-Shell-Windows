# -*- coding: utf-8 -*-
"""TreeList —— Windows 专用：Treeview API 兼容的 Canvas 列表控件。

背景（11-23 主人 4 次反馈"列表文字堆叠/拥挤"）：
  本构建（vista 主题 + 裁剪 Tk）里 ttk.Treeview 的行距被硬编码 ~20px（font/padding/
  style 配置全部实测无效），9pt 中字行距 28px → 行距 < 字行距，相邻行文字几乎粘连，
  观感"堆叠无法辨认"。TreeList 用 Canvas 自绘：行高 = 字行距 × 1.35（可见留白），
  完全绕开 Treeview 行距 bug；API 与 ttk.Treeview 对齐（gui.py 只换构造行，其余不动）。
  macOS 不用本类（Mac 构建 Treeview 行距正常），保持真 Treeview。
"""
import tkinter as tk
import tkinter.font as tkfont

# 选区/表头配色（对齐 Windows 观感）
_SEL_BG = "#0078d7"
_SEL_FG = "white"
_HDR_BG = "#f4f4f4"
_HDR_FG = "#333333"
_ROW_OUT = "#d9d9d9"


class TreeList:
    """ttk.Treeview 的 Canvas 兼容替身（仅 Windows 高缩放屏使用）。"""

    def __init__(self, master, columns=None, height=10, show="headings"):
        self.master_root = master if isinstance(master, tk.Tk) else master.winfo_toplevel()
        self.cols = tuple(columns or ())
        # 列宽（column() 设置；stretch 列在绘制时吃满剩余宽度）
        self._colw = {c: 100 for c in self.cols}
        self._stretch = {c: False for c in self.cols}
        self._anchor = {c: "w" for c in self.cols}
        self._hdtxt = {}                 # 表头文字（heading() 可带排序箭头）
        self._hdcmd = {}                 # 表头点击回调（heading(command=)）
        self._kids = []                 # 行 iid 顺序
        self._vals = {}                 # iid -> values tuple
        self._sel = set()
        self._focus_iid = None
        self._visible_rows = int(height or 10)

        # 字体：默认 YaHei UI 9pt（与 ttk 默认一致）
        self._fam = "Microsoft YaHei UI"
        self._fsz = 9
        self._font = tkfont.Font(self.master_root, (self._fam, self._fsz))

        self._cv = tk.Canvas(master, bg="white", highlightthickness=0, bd=0,
                             height=max(int(height or 10) * 22, 40))
        self._cv.bind("<Button-1>", self._on_click)
        self._cv.bind("<Configure>", lambda _e: self._redraw())

    # ---------- 内部 ----------
    def _row_h(self):
        """行高 = 字行距 × 1.35（留白 35%，高缩放屏下明显不挤）"""
        return max(int(self._font.metrics("linespace") * 1.35), 16)

    def _col_widths(self, canvas_w):
        """stretch 列吃满剩余宽度（对齐 Treeview 语义）"""
        widths = dict(self._colw)
        total = sum(widths.values())
        if total < canvas_w and any(self._stretch.values()):
            deficit = canvas_w - total
            stretch_cols = [c for c in self.cols if self._stretch[c]]
            if stretch_cols:
                each = deficit // len(stretch_cols)
                for i, c in enumerate(stretch_cols):
                    widths[c] += each + (deficit % len(stretch_cols) == i)
        return widths

    def _x_offsets(self, canvas_w):
        widths = self._col_widths(canvas_w)
        offs, x = {}, 2
        for c in self.cols:
            offs[c] = x
            x += widths[c]
        return offs, widths

    def _redraw(self):
        cv = self._cv
        cv.delete("all")
        canvas_w = max(cv.winfo_width(), 200)
        RH = self._row_h()
        hdr_h = RH - 4
        offs, widths = self._x_offsets(canvas_w)
        x_end = 2 + sum(widths.values())
        # 表头
        cv.create_rectangle(2, 0, x_end, hdr_h, fill=_HDR_BG, outline=_ROW_OUT)
        for c in self.cols:
            cv.create_text(offs[c] + 6, hdr_h / 2, text=self._hdtxt.get(c, c),
                           anchor="w", font=self._font, fill=_HDR_FG)
        # 行
        fnt_sel = tkfont.Font(self.master_root, (self._fam, self._fsz, "bold"))
        y = hdr_h
        zebra = 0
        for iid in self._kids:
            vals = self._vals.get(iid, ())
            sel = iid in self._sel
            bg = _SEL_BG if sel else ("white" if zebra % 2 == 0 else "#fafafa")
            cv.create_rectangle(2, y, x_end, y + RH, fill=bg, outline=_ROW_OUT)
            fg = _SEL_FG if sel else "#1a1a1a"
            for j, c in enumerate(self.cols):
                txt = str(vals[j] if j < len(vals) else "")
                # 列内截断（Canvas 文字无裁剪）
                if len(txt) > 1 and self._font.measure(txt) > widths[c] - 10:
                    while txt and self._font.measure(txt + "…") > widths[c] - 10:
                        txt = txt[:-1]
                    txt += "…"
                cv.create_text(offs[c] + 6, y + RH / 2, text=txt, anchor="w",
                               font=(fnt_sel if sel else self._font), fill=fg)
            y += RH
            zebra += 1
        cv.configure(scrollregion=(0, 0, x_end, y))

    def _on_click(self, ev):
        cv = self._cv
        RH = self._row_h()
        hdr_h = RH - 4
        canvas_w = max(cv.winfo_width(), 200)
        if ev.y < hdr_h:
            offs, _ = self._x_offsets(canvas_w)
            for c in self.cols:
                if offs[c] <= ev.x < offs[c] + self._col_widths(canvas_w)[c] and c in self._hdcmd:
                    self._hdcmd[c]()
                    return
        idx = int((ev.y - hdr_h) / RH)
        if 0 <= idx < len(self._kids):
            iid = self._kids[idx]
            self._sel = {iid}
            self._focus_iid = iid
            self._redraw()
            self._cv.event_generate("<<TreeviewSelect>>")

    # ---------- Treeview 兼容 API ----------
    def insert(self, parent, index, iid=None, values=(), **kw):
        if iid is None:
            n = max([int(i[1:]) for i in self._kids if i[1:].isdigit()] or [0]) + 1
            iid = "I%03d" % n
        self._kids.append(iid)
        self._vals[iid] = tuple(values)
        self._redraw()
        return iid

    def delete(self, *iids):
        for iid in iids:
            if iid in self._kids:
                self._kids.remove(iid)
            self._vals.pop(iid, None)
            self._sel.discard(iid)
        self._redraw()

    def get_children(self, parent=""):
        return list(self._kids)

    def item(self, iid, option=None, **kw):
        if option == "values":
            return self._vals.get(iid, ())
        if "values" in kw:
            self._vals[iid] = tuple(kw["values"])
            self._redraw()
        return ""

    def index(self, iid):
        return self._kids.index(iid) if iid in self._kids else -1

    def move(self, iid, parent, index):
        if iid not in self._kids:
            return
        self._kids.remove(iid)
        self._kids.insert(int(index), iid)
        self._redraw()

    def selection(self):
        return tuple(self._sel)

    def selection_set(self, iid):
        self._sel = {iid}
        self._focus_iid = iid
        self._redraw()

    def selection_add(self, iid):
        self._sel.add(iid)
        self._redraw()

    def selection_clear(self, *iids):
        if iids:
            for i in iids:
                self._sel.discard(i)
        else:
            self._sel = set()
        self._redraw()

    def focus(self, iid=None):
        if iid is None:
            return self._focus_iid or ""
        self._focus_iid = iid
        return iid

    def heading(self, col, text=None, command=None, **kw):
        if text is not None:
            self._hdtxt[col] = str(text)
        if command is not None:
            self._hdcmd[col] = command
        self._redraw()

    def column(self, col, width=None, anchor=None, stretch=None, **kw):
        if width is not None:
            self._colw[col] = int(width)
        if anchor is not None:
            self._anchor[col] = anchor
        if stretch is not None:
            self._stretch[col] = bool(stretch)
        self._redraw()

    def configure(self, **kw):
        if "height" in kw:
            n = int(kw.pop("height"))
            self._visible_rows = n
            kw["height"] = int(n * self._row_h()) + self._row_h() - 4
        if "yscrollcommand" in kw:
            self._cv.configure(yscrollcommand=kw.pop("yscrollcommand"))
        self._cv.configure(**kw)

    config = configure    # tk 风格别名

    def bbox(self, iid, col=None):
        if iid not in self._kids:
            return ""
        canvas_w = max(self._cv.winfo_width(), 200)
        RH = self._row_h()
        offs, widths = self._x_offsets(canvas_w)
        if col is None:
            x, w = 2, sum(widths.values())
        else:
            x, w = offs[col], widths[col]
        return (x, RH - 4 + self._kids.index(iid) * RH, int(w), int(RH))

    def yview(self, *args):
        return self._cv.yview(*args)

    def bind(self, seq, func=None, add=False):
        return self._cv.bind(seq, func, add)

    def update_idletasks(self):
        return self._cv.update_idletasks()

    def pack(self, **kw):
        return self._cv.pack(**kw)

    def grid(self, **kw):
        return self._cv.grid(**kw)

    def __getitem__(self, key):
        if key == "columns":
            return self.cols
        raise KeyError(key)

    def __getattr__(self, name):
        # 其余属性/方法透传给底层 Canvas（winfo_*、update 等）
        return getattr(self._cv, name)
