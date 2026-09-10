# 变更记录

格式大致是「版本 —— 改了什么」，越靠前越新。细节原因写在 [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md)。

## 4.0.11

- Inno 脚本里的相对路径修正（`OutputDir`、`SetupIconFile`、`[Files]` 的 stage 路径），现在从仓库根执行 `ISCC installers\campus_installer.iss` 就能构建
- 用 PyInstaller 6.21.0 重新打包
- 仓库整理：源码收进 `src/`，图标收进 `assets/`，安装脚本收进 `installers/`；补 README、架构、构建、排错文档，补 `campus_login.spec` / `campus_login.manifest`，让外人能照着打出一个能用的包
- 依赖清单收敛：实际只用到 `requests` / `certifi` / `pillow` / `pystray`，早年文档里写的 `psutil`、`pywin32` 代码里从来没用上，去掉

## 4.0.10

- 双击 exe（无参数）不再只静默登录：清残留退出标记 → 确保 daemon 在跑 → 弹出主界面
- 快捷方式启动参数由 `daemon` 改为空；开机自启仍用 `daemon`

## 4.0.9

- 修「点退出后进程残留」：`app_quit` 现在会真正停掉 `icon.run()`（新增 `_stop_tray()`，在工作线程退出、`smart_sleep`、托盘菜单、`cmd_daemon` 收尾多处兜底）
- 修「退出后左键托盘闪退」：退出状态下拒绝启动/恢复 popup

## 4.0.8

- 修 `smart_sleep()` 缺 `global _shutdown` 导致的 `UnboundLocalError`（4.0.7 引入，表现为开机完全不自动登录）

## 4.0.7

- `smart_sleep()` 响应 `app_quit`，不再睡满整个轮询间隔
- `quit` 等到 daemon / popup 进程真正消失再返回，修卸载遗留 exe 和 `_internal`

## 4.0.6

- 「开机首次认证」不再受后台守护开关门控，改成无条件执行
- daemon 空闲分支的托盘图标改为如实反映状态，不再恒显「连接中」

## 4.0.5

- `get_status()` / `is_connected()` 严格化：只有 `chkstatus` 回 `result == "1"` 才算已连接，外网可达不再当作已认证
- 后台守护默认开启，首次安装后配置保存不再把它关掉
- 新增开机首次认证：等网卡就绪 → 探测 → 未认证则重试登录 → 复核并写真实状态

## 4.0.4

- 电信运营商后缀 `@dx` 改为 `@aust`（原值是按经验写的，实际会被认证服务器拒）

## 4.0.3

- 托盘不再用 Win32 强行恢复 Tk 窗口，改为写 `popup.show` 请求、由 popup 自己 `deiconify()`；新增 `popup_window_valid()` 被动校验句柄

## 4.0.2

- onedir 定稿
- 新增 `launch_and_activate_popup()` 统一「显示主界面」的入口（已跑→激活、HWND 未就绪→轮询、没跑→启动）
- 修配置窗口用 `×` 关闭时不走 `teardown()`，导致 `config_editing` 残留、守护一直暂停认证

## 4.0.1

- 打包从 onefile 改为 onedir，绕开 `%TEMP%\_MEIxxxx` 被清理导致的编码器和 `cacert.pem` 找不到
- 用户数据迁到 `%LOCALAPPDATA%\Liwangtong\`，加旧数据迁移

## 4.0.0

- 打包改 `console=False`，不再闪黑窗
- 修「配置保存后主界面不出现」

## 更早（3.x / v49–v61）

onefile 时代的单文件版本。这阶段修的坑主要记在开发记录里：崩溃遥测、`ctypes.DWORD` → `wintypes.DWORD`、`OpenMutexW` 访问掩码、托盘线程模型、明文密码迁移到凭据管理器。没有正式的版本号发布记录。
