# Windows 版 llama-shell

## 构建

```bat
:: 前置：Python 3.11+、PyInstaller
pip install pyinstaller

:: 打包
pyinstaller --noconfirm --onedir --noconsole ^
  --name llama-shell ^
  --add-data "recommend.py;." ^
  --add-data "gguf_fast.py;." ^
  --add-data "gguf_parse.py;." ^
  gui.py

:: 产物：dist\llama-shell\llama-shell.exe + _internal\
:: 首次运行生成 llama-shell-config.json（同目录）
```

## 依赖

- 零第三方 GUI 依赖（仅 tkinter + 本地模块）
- 需本机有 `llama-server.exe`（llama.cpp 1.0.0 Vulkan 版）
- 启动时硬件探测 <0.1s（30 天内 GPU 缓存命中时）

## 功能

- 模型库扫描 + GGUF 解析（零依赖，`gguf_fast.py`）
- 参数推荐引擎（`recommend.py`）
- 双引擎：vanilla (llama-server) / 自定义路径
- 端口 8080 自动清理（netstat + taskkill）
- 配置持久化（`llama-shell-config.json`，exe 同目录）
