# 🍐 梨网通 (LiWangTong)

Dr.COM 校园网自动登录客户端 — Windows 专用。

## 功能

- **自动登录**：支持 Dr.COM 校园网认证，多通道兜底（POST /a79.htm + GET/POST /drcom/login）
- **系统托盘**：pystray 常驻后台，状态一目了然（绿色=已连接，灰色=未连接，粉色=异常）
- **凭据安全**：凭据存入 Windows Credential Manager，`config.json` 不落明文
- **弹出界面**：独立 popup 进程，操作不干扰守护进程
- **后台守护**：可选常驻守护，断线自动重连
- **Inno Setup 安装包**：支持一键安装、开机自启

## 项目结构

```
campus_login_v2/
├── src/                          # 源代码
│   ├── campus_login.py           # 主程序 (~2400 行单体)
│   ├── auto_login.vbs            # 自动登录 VB 脚本
│   ├── cacert.pem                # CA 证书 (构建时下载)
│   ├── config.json               # 用户配置 (已 gitignore)
│   └── runtime.json              # 运行时状态 (已 gitignore)
├── assets/                       # 图标与图片资源
│   ├── app.ico
│   ├── campus_notify.ico
│   ├── campus_notify_err.ico
│   ├── campus_gray.ico
│   ├── campus_ok.ico
│   └── old_popup.png
├── docs/                         # 文档
│   ├── DEVELOPMENT_LOG.md        # 开发日志
│   ├── HANDOFF.md                # 项目交接文档
│   └── liwangtong_handoff.md     # 交接记录
├── installers/                   # 安装脚本
│   └── campus_installer.iss      # Inno Setup 脚本
├── archive/                      # 历史存档 (已 gitignore)
│   ├── backup/                   # 旧版源码备份
│   ├── recovery/                 # 恢复与调试脚本
│   ├── reference/                # 参考实现 (C++ / Python)
│   ├── build_logs/               # 构建版本日志
│   └── releases/                 # 已发布的安装包 (.exe)
├── .gitignore
└── README.md
```

## 环境要求

- Python 3.8（当前打包目标）
- Windows（依赖 `winreg`, `ctypes.windll`, `pystray`, Credential Manager）
- 依赖：`requests`, `Pillow`, `pystray`, `psutil`, `pywin32`

## 快速开始

```bash
# 1. 配置账号
#    编辑 src/config.json:
#    {"username": "你的学号", "suffix": "@unicom"}

# 2. 运行
python src/campus_login.py

# 3. 打包 (PyInstaller)
pyinstaller campus_login.spec
```

> ⚠️ `config.json` 和 `runtime.json` 已在 `.gitignore` 中，不会提交到仓库。

## 打包与安装

```bash
# PyInstaller onefile 打包
pyinstaller --onefile --windowed --icon=assets/app.ico --add-data "assets;assets" src/campus_login.py

# Inno Setup 制作安装包
ISCC.exe installers/campus_installer.iss
```

## 技术栈

| 模块 | 技术 |
|------|------|
| GUI | tkinter (popup)、pystray (托盘) |
| 网络 | requests (Dr.COM HTTP 协议) |
| 打包 | PyInstaller onefile |
| 安装 | Inno Setup |
| 凭据 | Windows Credential Manager (win32cred) |

## 许可

本项目仅供学习交流使用。