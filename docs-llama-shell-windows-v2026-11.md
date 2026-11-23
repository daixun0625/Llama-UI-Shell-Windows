# llama-shell Windows v0.2 · 最终形态文档（2026-11-23）

> 基线：macOS `Llama-UI-Shell` v0.2（SHA `31c2640`）→ Windows 双引擎移植（vanilla + ik，tosh 隐藏）。
> 出货物：`D:\llama-shell-work\src\dist\llama-shell\llama-shell.exe`（PyInstaller onedir，onedir 布局含 `_internal\`）。
> 桌面快捷方式 `C:\Users\dx\Desktop\llama-shell.exe.lnk` 指向该路径（无需改）。
> 0.1 历史形态见 git（`Llama-UI-Shell-Windows` 仓库 bc15dfb），本文只记 0.2 现状。

## 1. 引擎与二进制（双引擎）

| 引擎 | 二进制 | 说明 |
|---|---|---|
| vanilla | `D:/llama-vulkan-x64/llama-server.exe`（b12374 Vulkan） | GPU 主力；`-ngl`/`--agent`/`--cache-reuse`/LoRA 全支持 |
| ik | `D:/ik-win/bin/llama-server.exe`（ikawrakow CPU AVX2 自编译） | 纯 CPU；`build_command` 自动剔 `-ngl`/`--agent`/`--cache-reuse`，MTP 映射为 ik 原生语法 `mtp:n_max=1,p_min=0.0`；bench E4B ≈12.3 t/s（8 线程） |
| tosh | （隐藏，Windows 无此引擎） | `pick_engine` 强制集 = {vanilla, ik}；UI 无 tosh 单选 |

- `D:\ik` 是 mac Mach-O 产物（.dylib），Windows 不可用；Windows 版在 `D:\ik-win\`（CMake+MSVC 源码构建）。
- 文生图引擎（T2I/I2I 页）：`C:\Users\dx\AppData\Local\LocModel\engines\sd\master-1106-532d2c1-vulkan\sd-cli.exe`（LocModel 自带，flag 全支持：`-m -e -r --strength -v --offload` 等）。
- 模型目录：文字 `D:/models`（含 mmproj/*.gguf），图片 `D:/models/image`（子目录=模型、`common\`=编码器/VAE 池、`loras\`=LoRA；与 mac `scan_image_models` 布局同构）。

## 2. 配置文件（出货 dist\llama-shell\llama-shell-config.json）

```json
{"models_dir": "D:/models",
 "vanilla_bin": "D:/llama-vulkan-x64/llama-server.exe",
 "ik_bin": "D:/ik-win/bin/llama-server.exe",
 "image_bin": "C:/Users/dx/AppData/Local/LocModel/engines/sd/master-1106-532d2c1-vulkan/sd-cli.exe",
 "vram_override_gb": 8,
 "gpu_cache": {...}}
```

- 启动时 30 天 GPU 探测缓存写回 `gpu_cache`（本机 dxdiag 常取不到 6600 XT → WMI 兜底，`src=wmi` 正常）。
- 旧 0.1 `custom_srv` 键自动迁移为 `vanilla_bin`。

## 3. 出货门禁（必须全过再打包）

```
venv python src\test_regression.py     # 40 项全 PASS
venv python src\_e2e_text.py           # GUI 驱动真实 ik 引擎对话+干净停止（11.9 t/s）
venv python src\_e2e_img.py            # GUI 驱动 LocModel sd-cli 真实出图（z-image-turbo）
taskkill /F /IM llama-shell.exe        # 先杀旧实例（锁 _internal\base_library.zip）
venv pyinstaller --noconfirm llama-shell.spec
重写入 dist\llama-shell\llama-shell-config.json + gui_state.json={}
启动冒烟 45s：无 Traceback + gpu_cache 已写回
```

回归 40 项 = 0.1 历史 1–10（按钮绑定/日志滚动/停止语义/引号/双轨不变量等）+ 0.2 新增（36 参数面板、FREE_EDIT_KEYS、MTP/LoRA 候选、build_image_command、sd-cli 检测、引擎旗标语义、图片页控件、moe-cache-mib、I2I `-r/--strength`、Windows 引擎单选恰为 ik/vanilla）。

## 4. Windows 适配层要点（相对 mac 0.2 基线）

1. **双轨命令串**：`_shell_str` 生成显示串（Windows 双引号，可复制进 cmd）；`_spawn`/`_run_image` 用 **posix `shlex.split`** 解析回不带引号的原始 token 给 Popen。
   - 引号规则（`_shell_str`）：`.gguf`、含空格/`"`/**反斜杠**（Windows 路径！）/非 ASCII 的 token 必引；引号内 `"` 转义为 `\"`。
   - **反斜杠必须引**：posix shlex 引号外反斜杠是转义符（会吃掉路径字符），引号内是字面量。
   - 不变量（回归 #6b）：`shlex.split(_shell_str(tokens)) == tokens`。
   - 历史坑：曾用 `shlex.split(posix=False)` 非 POSIX 解析 → 外层引号没剥掉 → llama-server 收到带引号文件名（`Invalid argument`）。
2. **Popen**：`CREATE_NO_WINDOW`(0x08000000) + `cwd=二进制目录`（DLL 同目录）+ `encoding=utf-8, errors=replace`（日志读线程）。
3. **停止**：`taskkill /F /T`（进程树）；`kill_port` 用 `netstat -ano` 找 PID（`encoding=utf-8` 防 GBK 头解码崩）+ taskkill，psutil 兜底。
4. **GPU 监控**：`_gpu_sampler` 后台线程（PowerShell Get-Counter AMD / nvidia-smi），主线程 `_update_hwload` 只读缓存 `_gpu_last`（<100ms 门禁）。
5. **`_health_poll` 后台线程不读 Tk 变量**：`_spawn` 时主线程缓存 port/host。
6. **DPI**：`main()` 里 `SetProcessDpiAwareness(2)`（PMDv2，失败退 `SetProcessDPIAware`）。
   - **高 DPI 布局缩放（11-23 新坑）**：本机 5120×2160 @175%。固定像素布局（树列宽/左栏宽）不随 DPI 缩 → 文字截断"重叠"。解法：`self._dsc = winfo_fpixels("1i")/96`（纯 Tk，**不要**用 user32 `GetDeviceCaps`/shcore `GetDpiForWindow`——本机 Win11 26300 build 已裁掉这些旧导出，ctypes 解析不到），树列宽/左栏宽全部 ×`_dsc`。
7. **`_make_thumb`**：PIL → tk.PhotoImage(png/gif) → 占位图 三级回退（venv Pillow 缺 _imaging，不可靠，勿把 PIL 当硬依赖）。

## 5. 布局（11-23 主人定规：列表/日志加宽）

- 左栏（模型库+日志框）= **686×`_dsc`**（v4：760 基础上主人令收窄 10 字符≈74px）；树列：模型名 420(stretch 吃满余量) / B 44 / 类型 70 / GB 48 / 层 48（均 ×`_dsc`，min 40）。
- **列表控件 = TreeList（v5，根治"文字堆叠/无法辨认"）**：本构建 vista 主题下 `ttk.Treeview` 行距**硬编码 ~20px**（168dpi 实测），而 9pt 中字行距 28px → 行距 < 字行距，相邻行文字粘连 8px。关键坑：**`ttk.Style().configure("Treeview", font/padding=…)` 在此构建全部静默失效**（lookup 恒 `''`，行距不变）——v4 的 padding"+27% 行高"实为 0 效果（主人 4 次反馈"没改过"的根因）。解法：`src\treelist.py`（Canvas 自绘、Treeview API 兼容的列表），行高 = 字行距×1.35（168dpi 实测 37px vs 字 28px → 留白 10px）；**仅 Windows 启用**（`gui.py` 构造行 `TreeList if IS_WIN else ttk.Treeview`，Mac 行距正常保持真 Treeview）。PyInstaller spec datas 需显式加 `('treelist.py','.')`（`_internal\` 下，bootstrap 双 sys.path 命中）。
- **防残影重绘**：窗口 `<Configure>` 防抖 300ms 全量重绘树（PMDv2 缩放/移屏旧文字残影，delete+原 iid 重插，选中态保留）。
- 窗口 `_fit_frame` 内容自适应；回退 1560×954；图片页切页恢复文字页尺寸。
- 诊断方法沉淀：**"文字重叠"先量 bbox 行距 vs 字体行距**（`tree.bbox(iid,col)` 相邻行 y 差 vs `tkfont.Font(root,(fam,sz)).metrics("linespace")`），再像素验证（ImageGrab + 暗像素行带投影）；style 失效时以 lookup 回值为准，不以"我设过"为准。

## 6. 默认参数（mac 0.2）

`-t 8`、cache-ram 8192、jinja on、reasoning off；`reasoning_effort` 主人固定 low（与 bat 对齐）；bat 启动脚本主人自管，勿动。

## 7. 性能参考（本机 i5-12400F/32G/RX6600XT-8G）

- 35B-A3B MoE（vanilla Vulkan）：本机天花板 ~15 t/s（CPU MoE 侧定）。
- E4B（ik CPU AVX2）：≈12.3 t/s。
- E4B（vanilla Vulkan b12374）：prefill ≈1005 t/s、decode ≈51 t/s（快于 ik 与 TQ3-HIP 包）。

## 8. 仓库与文件

- `D:\llama-shell-work\Llama-UI-Shell`（mac，同步 origin/main）· `Llama-UI-Shell-Windows`（Win 0.1 基准）· `src\`（0.2 开发树，非 git 仓）。
- 过程文件：`_check_rec.py`（recommend 断言）、`_e2e_text.py`/`_e2e_img.py`（e2e）、`_test_out.txt`/`_build_out.txt`（日志）、`probe\`（0.1 探针，弃用）、`extract\`（0.1 解包历史，弃用）。
- `D:\llama-bin-win-vulkan-x64` 已不存在（主人确认）——所有引用已清。
