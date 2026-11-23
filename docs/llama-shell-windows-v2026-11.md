# llama-shell Windows v0.2 · 最终形态文档（2026-11-23）

> 基线：macOS `Llama-UI-Shell` v0.2（SHA `31c2640`）→ Windows 双引擎移植（vanilla + ik，tosh 隐藏）。
> 出货物：`D:\llama-shell-work\src\dist\llama-shell\llama-shell.exe`（PyInstaller onedir，onedir 布局含 `_internal\`）。
> 桌面快捷方式 `C:\Users\dx\Desktop\llama-shell.exe.lnk` 指向该路径（无需改）。
> 0.1 历史形态见 git（`Llama-UI-Shell-Windows` 仓库 bc15dfb），本文只记 0.2 现状。

## 1. 引擎与二进制（双引擎）

| 引擎 | 二进制 | 说明 |
|---|---|---|
| vanilla | `D:/llama-vulkan-x64-b12651/llama-server.exe`（**b12651 Vulkan**，11-23 升级；b12650 留作回退；`D:/llama-vulkan-x64`（b12374）不动） | GPU 主力；`-ngl`/`--agent`/`--cache-reuse`/LoRA 全支持。**升级规矩（主人 11-23 定规）：「检查并升级」落盘 = 版本化目录 `D:\llama-vulkan-x64-bXXXX`（取消 BASE/bin/.dl）+ 保留最近 2 个版本目录滚动删 + 两份 config `vanilla_bin` 指向新目录再出货** |
| ik | `D:/ik-win/bin/llama-server.exe`（ikawrakow CPU AVX2 自编译） | 纯 CPU；`build_command` 自动剔 `-ngl`/`--agent`/`--cache-reuse`，MTP 映射为 ik 原生语法 `mtp:n_max=1,p_min=0.0`；bench E4B ≈12.3 t/s（8 线程） |
| tosh | （隐藏，Windows 无此引擎） | `pick_engine` 强制集 = {vanilla, ik}；UI 无 tosh 单选 |

- `D:\ik` 是 mac Mach-O 产物（.dylib），Windows 不可用；Windows 版在 `D:\ik-win\`（CMake+MSVC 源码构建）。
- 文生图引擎（T2I/I2I 页）：`C:\Users\dx\AppData\Local\LocModel\engines\sd\master-1106-532d2c1-vulkan\sd-cli.exe`（LocModel 自带，flag 全支持：`-m -e -r --strength -v --offload` 等）。
- 模型目录：文字 `D:/models`（含 mmproj/*.gguf），图片 `D:/models/image`（子目录=模型、`common\`=编码器/VAE 池、`loras\`=LoRA；与 mac `scan_image_models` 布局同构）。

## 2. 配置文件（出货 dist\llama-shell\llama-shell-config.json）

```json
{"models_dir": "D:/models",
 "vanilla_bin": "D:/llama-vulkan-x64-b12651/llama-server.exe",
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

- 左栏（模型库+日志框）= **686×`_dsc`**（v4：760 基础上主人令收窄 10 字符≈74px）；树列（v6 主人令调宽）：模型名 **360**(stretch 吃满余量) / B 44 / 类型 **148** / GB 48 / 层 48（均 ×`_dsc`，min 40；基宽 360+148 下 stretch 生效，总宽不超树宽 1173，layer 右缘贴边不溢出）。
- **列表控件 = TreeList（v5，根治"文字堆叠/无法辨认"）**：本构建 vista 主题下 `ttk.Treeview` 行距**硬编码 ~20px**（168dpi 实测），而 9pt 中字行距 28px → 行距 < 字行距，相邻行文字粘连 8px。关键坑：**`ttk.Style().configure("Treeview", font/padding=…)` 在此构建全部静默失效**（lookup 恒 `''`，行距不变）——v4 的 padding"+27% 行高"实为 0 效果（主人 4 次反馈"没改过"的根因）。解法：`src\treelist.py`（Canvas 自绘、Treeview API 兼容的列表），行高 = 字行距×1.35（168dpi 实测 37px vs 字 28px → 留白 10px）；**仅 Windows 启用**（`gui.py` 构造行 `TreeList if IS_WIN else ttk.Treeview`，Mac 行距正常保持真 Treeview）。PyInstaller spec datas 需显式加 `('treelist.py','.')`（`_internal\` 下，bootstrap 双 sys.path 命中）。
- **防残影重绘**：窗口 `<Configure>` 防抖 300ms 全量重绘树（PMDv2 缩放/移屏旧文字残影，delete+原 iid 重插，选中态保留）。
- **v6 按钮同色（主人令）**：run 钮 idle/加载态底色 `#e9eaef`→`#d5d7dd`——初始页四钮（运行/停止/⚡生成指令/复制）统一浅灰 `#d5d7dd`+`#999` 字；状态色不变（就绪绿 `#2e7d32` / 运行中红 `#c62828` / 可用深灰 `#4a5568` / 按下反馈 `#c9cbd4`）。
- **v7 升级行取消（主人令）**：原独立「llama 升级:」行（upgbar）删除；「检查并升级」按钮移入 engbar 的 vanilla ⋯ 右边（width 10）；版本信息（本地 bX·稳定 bY·最新 bZ / 已中断 / 已安装）改走日志 `[升级]` 行；upg_btn 随 engbar 切页显隐（仅文字页）。回归断言改「升级按钮在 engbar 且无独立升级行」（`upg_btn.master is engbar`、无 `upgbar`/`up_ver_lbl`）。升级功能本体（upgrade_check/下载/解包/bin 轮替/vanilla_bin 回写）不变。
- **v8 升级流改造（主人令：取消 BASE/bin/.dl 默认升级路径，默认 D:/llama-vulkan-x64-b1xxxx/llama-server.exe）**：
  - 「检查并升级」Windows 落盘改**版本化目录 `D:\llama-vulkan-x64-bNNNN`**（下载进 `<目录>\.dl` → 解包 → zip 顶层若带 `llama-bNNNN/` 自动拍平 → `--version` 校验 → 写 config `vanilla_bin` 指向新版）；macOS 保持 `bin/llama-bNNNN` 不变；
  - **保留最近 2 个版本目录、滚动删旧**（`rotate_win_vulkan_builds`；正在运行引擎所在目录不删；无 b 后缀的旧生产目录 `D:\llama-vulkan-x64` 不在轮次范围，主人令才动）；
  - 空配置默认探测链 ② 改版本化目录中**编号最高者**（`_find_default_llama_server` 内联扫描 D:\ 顶层，模块级执行不可前向引用 `win_vulkan_builds`）；
  - `current_vanilla_num` 的 `b(\d+)` 正则天然兼容版本化目录；`win_vulkan_builds()` 枚举 D:\ 顶层（版本化目录是兄弟目录，**不在** `D:\llama-vulkan-x64` 之内——11-23 首版枚举写错已修）；
  - 现行指向 **b12651**（sha256 `e58fa9a1…` 官方匹配；旧 b12650 留作回退，b12374 目录不动）；回归 +2（版本化目录枚举/轮次函数在位）。
- **v9 默认参数 + 选中态按钮色（主人令 11-23）**：
  - 面板默认值改：`batch -b` 512→**2048**、`ubatch -ub` 512→**2048**、`image_min_tokens` 256→**1024**、`image_max_tokens` 4096→**2048**、`cache_ram` 8192→**4096**；采样/思考区对齐主人定值：`frequency_penalty` 0→**0.5**、`repeat_last_n` default→**64**（temp 0.75 / top-k 40 / top-p 0.95 / min-p 0.05 / repeat 1.1 / presence 0.05 / seed -1 / reasoning off / jinja on 原已一致）；recommend 推荐计划（选模型时回填）同步统一 `-b 2048 -ub 2048 cache-ram 4096`（原 MoE 最佳 `-b 4096 -ub 1024` 如需可面板手改，按模型持久化）；
  - 选中模型后（cmd 可用 = ⚡生成/复制 同态）**运行/停止钮亦变深灰 `#4a5568`**（白字）；未选中初始页四钮仍统一浅灰 `#d5d7dd`；就绪绿/运行中红不变。`_draw_run/_draw_stop` 增 `avail` 参数（`_paint_all` 传 `_cmd_available`），头less 断言四色态 PASS。
- **v10 图片页进度条修（主人报"不反映实际进度"）**：根因 = sd-cli 实际进度行 `|…| CUR/TOT - X.XX(GB/s|s/it|it/s)`，旧正则只匹配 `it/s` → 采样主循环（多为 `s/it`，如 `6/16 - 12.90s/it`）几乎不发进度事件，进度条卡住。修法：`_img_reader` 新正则 `sd_re` 覆盖三单位（真实行 5/5 命中验证）；`GB/s`=张量加载相位发 `L:cur:tot`（进度条按张量数走，标签「加载模型 X/Y」），`s/it|it/s`=采样相位 X/N 步；OUT 兜底分支同步改（排除 GB/s 防张量数误当步数）。
- 窗口 `_fit_frame` 内容自适应；回退 1560×954；图片页切页恢复文字页尺寸。
- 诊断方法沉淀：**"文字重叠"先量 bbox 行距 vs 字体行距**（`tree.bbox(iid,col)` 相邻行 y 差 vs `tkfont.Font(root,(fam,sz)).metrics("linespace")`），再像素验证（ImageGrab + 暗像素行带投影）；style 失效时以 lookup 回值为准，不以"我设过"为准。

## 6. 默认参数（mac 0.2）

`-t 8`、cache-ram 8192、jinja on、reasoning off；`reasoning_effort` 主人固定 low（与 bat 对齐）；bat 启动脚本主人自管，勿动。

## 7. 性能参考（本机 i5-12400F/32G/RX6600XT-8G）

- 35B-A3B MoE（vanilla Vulkan）：本机天花板 ~15 t/s（CPU MoE 侧定）。
- E4B（ik CPU AVX2）：≈12.3 t/s。
- E4B（vanilla Vulkan b12374）：prefill ≈1005 t/s、decode ≈51 t/s（快于 ik 与 TQ3-HIP 包）。
- **Inkling-2-Mini（16.5GB Q4_K，推理模型，11-23 A/B 实证）**：chat 路径先写思考块（`reasoning_content`，本次 ~50 token）再写答案；`--reasoning off` 只关 effort、不关模板自带思考。**max_tokens=48 < 思考块 → `content` 空串但 completion_tokens 已消耗**（= 主人"加载成功、输出失败"症状；b12374/b12650 同病，非版本缺陷）；≥256 正常出 `content`（实测 104/98 token 收尾 stop）。诊断此类问题看 `/v1/chat/completions` 全量 JSON 的 `reasoning_content` 字段，raw `/completion` 不走 jinja 模板、不能复现。预填速度调参 A/B（ctx/KV 量化/线程）结果见 11-23 对话。

## 8. 仓库与文件

- `D:\llama-shell-work\Llama-UI-Shell`（mac，同步 origin/main）· `Llama-UI-Shell-Windows`（Win 0.1 基准）· `src\`（0.2 开发树，非 git 仓）。
- 过程文件：`_check_rec.py`（recommend 断言）、`_e2e_text.py`/`_e2e_img.py`（e2e）、`_test_out.txt`/`_build_out.txt`（日志）、`probe\`（0.1 探针，弃用）、`extract\`（0.1 解包历史，弃用）。
- `D:\llama-bin-win-vulkan-x64` 已不存在（主人确认）——所有引用已清。

## 9. 默认值与按钮（11-23，主人令×3）

① **默认值×6**：`-b 2048`、`-ub 2048`、图像最小 1024、图像最大 2048、`--cache-ram 4096`（面板默认值+推荐方案同步，替换 MoE 优化 4096/1024）；
② **按钮色**：选中模型后 运行/停止 变深灰 `#4a5568`（同 生成/复制）；未选中保持 `#d5d7dd`；ready/running 状态色不变；
③ **采样默认**（主人截图）：frequency-penalty **0.5**、repeat-last-n **64**（其余 10 项已一致）。
出货 @`6439c12`/`302f629`（2026-11-23）。

## 10. 图片页进度条相位跳变（11-23）

主人："一开始就刷全绿，然后才从头开始"。根因：sd-cli 两相日志（①张量加载 `GB/s` 65/453→453/453 ②采样 `s/it` 1/16）旧逻辑各自 0-100% → 加载满 100% 全绿后采样归 0。**修**：`_img_reader` 正则扩展 `GB/s`（加载）+ `s/it`/`it/s`（采样，sd-cli 实际单位）；统一标尺 **加载 0–50 / 采样 50–100**（`L:`/`S:` 事件带相位标签，done=100、fail=0），全程单调。
**验证**：回归 42/42（+进度相位标签/采样 50-100 锚点 2 项）；E2E-B 同款日志回放：加载 0→50、采样 50→100 单调。
出货 @`ac1d324`/`0d8e21a`（2026-11-23）。

## 11. h3 三件套修复 + config 斜杠归一（11-23）

主人两条指令：「你的 / 都是反的」→ config 路径全反斜杠；「encoder 手改成 32B」→ h3 族默认编码器 32B。
① **config 反斜杠化**：src+dist 五键路径统一 `D:\...`（两种斜杠 Windows 都能跑，纯习惯归一）；
② **h3 报错三层链**（sd-cli rc=1 逐层剥离）：
 1. `default` 字面值塞 `--steps`/`--cfg-scale`/`--seed` → 一选 h3 即 rc=1（§10 同批已修：default/空省略 flag，显式值照发）；
 2. `MiniMax-H3-Image + Qwen3-VL-4B 需 P-H3-Image 文本连接器 / 或改用 32B 编码器` → 主人选 32B：`_pick_encoder` 改**族前缀逐级匹配**（`minimax_h3_image → minimax_h3` 命中 `qwen3vl_32b_minimax_h3` 18GB 编码器；z-image-turbo 不受影响仍 Qwen3-4B）；
 3. `failed to build VAE runners: requires its 2D image VAE` → VAE 挑选加**族前缀优先**（common/ 先找 `minimax_h3_image_vae_fp16.safetensors`，再 ae.safetensors）；
 4. `failed to encode negative prompt`（32B Qwen3-VL 编码器不接受空负向词）→ `build_image_command` 对 `edit_capable` 模型 neg 留空时注入默认负向词 `blurry, low quality, watermark, text, logo`。
③ **验证**：h3 条目扫描 = 32B 编码器 + h3 VAE ✓；GUI 形态命令全链路后台生成验证中（11.4GB 扩散 + 18GB 编码器压 32GB 内存，主人知悉接受，慢/顶 G 盘页文件属预期）；回归 42/42。
出货 @`59e4363`；dist 已重建 + 反斜杠 config 注入。
