# 🍐 梨网通 — Handoff 交接文档

## 项目概述
Dr.COM 校园网自动登录客户端，Python 3.8 + tkinter + PIL，PyInstaller onefile 打包，Inno Setup 安装。纯 Windows 专用（winreg/ctypes.windll/pystray/Credential Manager）。

## 关键路径
- 工作区：`D:\deep Workspace\campus_login_v2\`
- **主源码**：`campus_login.py`（1886 行，含全部修复）
- **备份**：`BACKUP_campus_login.py`
- **全局色常量**：`C_GREEN/C_BLUE/C_PINK/C_PEAR`（line ~48）
- **安装脚本**：`campus_installer.iss` → `output\LiWangTong-Setup-3.2.0.exe`
- **桌面安装包**：`C:\Users\33389\Desktop\LiWangTong-Setup-3.2.0.exe`
- **打包**：`C:\Program Files\python\Scripts\pyinstaller.exe`（产物在 `D:\deep Workspace\dist\campus_login.exe`）
- **Inno**：`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`
- **测试床**：`test_run\梨网通.exe`（部署测试）

## 当前状态（v45 已定稿，11:19 打包）
源码已包含**全部确认修复**，桌面安装包已更新。逻辑路径经 Linux 代码走查 + Windows 实机验证。

### 已修复的关键 bug（全部实测）
1. `bot_y→BOT_Y` —— Toast 触发不再 NameError 崩溃
2. `begin_busy` 加 `anim_on[0]=True` + `root.after(150, anim_tick)` —— **注销/连接不再永久转圈卡死**
3. popup `tick()` **不再检查 quit_requested()** —— 修复"操作被共享 runtime 的 quit 标记误杀、自动退程序"。popup 是独立进程，只响应自身退出确认
4. 紫色圈 —— 渲染图背景 alpha=0→255（`(255,0,254,255)`），圆角外纯洋红被 transparentcolor 挖掉
5. 双击绑定点不了 —— 只留 `c.bind("<Button-1>")`，删 `ButtonPress-1`（Tk 里与 Button-1 同一事件会覆盖 click）
6. 眼睛点击、保存后开主界面 —— 同上绑定修复
7. 颜色常量去重（COLOR_TEXT_PRIMARY 5份→1份）
8. `nid.uTimeout` 删除 —— 系统通知失效修复（ctypes 结构体无此字段，赋值必 AttributeError）
9. runtime tmp PID 隔离 —— 多进程不互覆盖
10. `set_autostart` 返回 bool，cmd_autostart 显示 [OK]/[FAIL]
11. 通知失败记 `log()`（不再静默吞异常）
12. `_cleanup_orphan_tmp()` 启动时清理孤儿 tmp（main 里调用）

### 语义确认（用户明确）
- **连接/注销**：网络操作，**不退出程序**；注销后状态变"未连接"可见
- **右上角 ×**：关闭 popup 界面，**不退出托盘**（popup 是独立进程，os._exit 只退 popup，daemon/托盘存活）
- **退出（底部粉色按钮）**：确认后退出**程序+托盘**（do_quit_app → request_quit → daemon 循环 break → 托盘 stop）
- **后台守护开关**：默认**关**（daemon_state_read 默认 False），用户可手动开

### 配置保存后行为（安装完成链路）
`[Run] Parameters: "init"` → 弹出配置界面 → 保存 → teardown 拉起 `popup`(主界面) + `daemon`(托盘)，`daemon_state_write(False)` 后台守护默认关。

## 未修复 / 待确认（低优先级）
- **协议层健壮性**：`_login_post_a79` 条件过宽（`"ac0=" in t`）；`result=="1"` 类型严格。**改错会伤登录判断**，实测校园网无异常就不要动。
- **daemon 断网重连最坏约 54s**（留 grace_period + 24s 重试 + 30s 等待）——不卡死但慢。
- `os._exit(0)` 多处跳过清理（托盘/临时文件）——当前不影响功能，属 P2。

## 构建命令
```powershell
# 编译 exe（产物在 D:\deep Workspace\dist\campus_login.exe）
& "C:\Program Files\python\Scripts\pyinstaller.exe" --onefile --console --clean --noconfirm --collect-all pystray --collect-all PIL --collect-all pillow --hidden-import tkinter --manifest "...\campus_login.manifest" --icon "...\assets\app.ico" "...\campus_login.py"
# 打包 Inno（需先复制最新 exe 到 stage\梨网通.exe）
Copy-Item "D:\deep Workspace\dist\campus_login.exe" "...\stage\梨网通.exe" -Force
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "...\campus_installer.iss"
# 复制到桌面
Copy-Item "...\output\LiWangTong-Setup-3.2.0.exe" "C:\Users\33389\Desktop\LiWangTong-Setup-3.2.0.exe" -Force
```

## 实测验证方式（可靠，模拟鼠标）
写 python 脚本用 `ctypes`+`user32`（SetCursorPos/mouse_event/keybd_event）模拟点击。窗口逻辑坐标 ×1.5(DPI)=物理，加窗口 rect.left/top。关键用 `PrintWindow` 抓窗口内容（不受遮挡），**别用 ImageGrab 抓屏幕**（会被浏览器盖住）。

注意：`<Button-1>` 与 `<ButtonPress-1>` 是 Tk 同一事件，后绑覆盖先绑（大坑）。

## 已知环境注意
- PowerShell `bot_y` 匹配大小写不敏感，会误匹配 `BOT_Y`
- `Select-String` 的 `\b...\b` 在 PS 里大小写不敏感
- pyinstaller 无 --distpath 时产物在 `D:\deep Workspace\dist\`（cwd 项目根）
- 外部 AI 审查的 bug 清单**有误报**，逐条 grep 源码核实再改

## 建议规范
- **交接文档今后写工作区**（如本文件 `HANDOFF.md`），不要写系统 temp
- 记忆文件：`D:\deep Workspace\memory\2026-09-05.md`

## suggested skills
- `diagnose` —— 若遇到运行期 bug/回归，纪律化定位
- `mimo-vision` —— 若需分析截图确认 UI 渲染（当前模型不直接支持读图）
- `review` —— 若需对照规范审查改动
