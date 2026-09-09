# 🍐 梨网通 开发日志（开发者视角）

> 项目：Dr.COM 校园网自动登录客户端，面向安徽理工大学（AUST）校园网。
> 技术栈：Python 3.8 + tkinter + PIL + pystray + requests + Windows Credential Manager（ctypes）。
> 打包：PyInstaller onedir + Inno Setup 6。架构：daemon(托盘+守护) / popup(主界面) / init(配置窗口) 三进程，靠 runtime.json + Windows 命名互斥锁做 IPC。

---

## 一、总体思路

这个项目最大的难点不在「登录协议」，而在于**多进程生命周期管理**：daemon / popup / init 三个独立进程，靠一个共享的 `runtime.json` 和三个命名互斥锁（`MUTEX_DAEMON/POPUP/INIT`）协作。任何一个进程的退出时机、状态标记、谁该负责“结束整个程序”，都极易踩坑。

本阶段（4.0.3 → 4.0.10）的核心，就是**把“显示窗口”“自动连接”“退出程序”这三件事的职责彻底理清**，并消除它们之间的竞态。

---

## 二、前几轮对话的历史过程（v45 → 4.0.2）

> 这一段记录的是早前几轮对话里，从 onefile 时代一路修到 onedir 定稿（4.0.2）的 Debug 过程。当时大多是在**反复“修不好”**的拉锯里逐层逼近真相，很多根因都是被「看似很像、其实不是」的假线索带着跑后才找到的。

### 2.1 拉锯的开端：注销退出、注销无效果、配置窗口闪烁、连不上

用户最初反馈的四个现象，早期每一版都“以为修好了，实际没修好”：

- **「注销就自动退程序」**：popup 的定时器直接 `if quit_requested(): os._exit(0)`，而 popup 和 daemon 共用 runtime.json。daemon 普通退出时会写 `quit=True`，导致 popup 把“daemon 退出”误判成“用户要退出整个软件”，于是被误杀。
- **「注销没看见效果」**：注销后 daemon 立刻自动重连，界面刚变“未连接”又弹回，看起来像没生效。
- **「修改账号密码窗口只闪一下就没」**：`init` 是用 `CREATE_NEW_CONSOLE` 启动的（人为建了控制台又 `hide_console`，控制台一闪），且保存后还去重启 daemon+popup，与已运行的 daemon 抢 `MUTEX_DAEMON`，新实例瞬间退出。
- **「连不上」**：报错指向 onefile 临时目录里的 `certifi\cacert.pem` 找不到。

### 2.2 决定性的一步：崩溃遥测（v55/v56）立功

之前全靠“猜”。加了 `_setup_crash_logging()`（接住 `sys.excepthook` / `threading.excepthook` / Tk 回调异常并写日志）后，**第一次拿到了真实报错**：

```
[CRASH][thread] FileNotFoundError: ...\_MEI864962\base_library.zip
  at net_worker → resolve_password → cred_read
```

- 这是 **PyInstaller onefile 临时目录（`_MEIxxxx`）在运行中被清理**导致的：后台线程再懒加载 `utf-16-le` 编解码器就 `FileNotFoundError`，而 `net_worker` 把 `resolve_password` 放在 `try` 之外 → 线程崩、没写 `last_net` → 注销界面永久转圈。
- 修法：启动即预加载常用编码（utf-8/utf-16/utf-16-le/utf-16-be/latin-1/ascii/gbk），并把 `net_worker` 的取密码/网络/写状态全部包进 `try/except`，保证 `last_net` 一定写入 → busy 必清除，不再转圈卡死。

### 2.3 真正的根因：`ctypes.DWORD`（v54，全网走一天才定位）

`is_popup_running()` / `is_daemon_running()` 用了**不存在的 `ctypes.DWORD`**（应写 `wintypes.DWORD`）。这个 `AttributeError` 被裸 `except: return False` 吞掉 → **这两个探测函数恒返回 False**。于是托盘左键永远认为“popup 没运行”→ 每次都 `Popen` 新实例 → 各实例互相争抢（onefile 解压慢、拿不到单例就退）→ 表现为“唤不醒/抖动/左键无响应”。

修：全文 `ctypes.DWORD` → `wintypes.DWORD`。这一步同时修好了凭据结构体的 argtypes、`is_popup_running`、`is_daemon_running`。

### 2.4 托盘左键链路的多轮修正（v49 → v53）

- **v50**：`OpenMutexW` 访问掩码写错成 `0x1F001`，应为 `MUTEX_ALL_ACCESS=0x1F0001`，导致探测恒失败。
- **v51**：裸 `except: pass` 把所有启动失败吞成“没反应”。改为 `_tray_open_popup/_tray_run/tray_start` 全程带 `[TRAY]` 日志、启动前校验 `os.path.isfile(exe)`、打印 PID；`acquire_singleton` 保存句柄并记录 `[MUTEX]`。
- **v52**：把“激活已有窗口”从 runtime.json IPC 换成**直接 Win32 `POPUP_HWND_FILE`**（`popup.hwnd`）：`IsWindow` 校验 + `ShowWindow` + `SetForegroundWindow` + 短暂置顶。
- **v53（关键语义变更）**：右上角 `×` / `Esc` 由 `os._exit(0)` 改为 **`root.withdraw()`（隐藏进程/单例/句柄都保留）**。原因：onefile 每次左键都 `Popen` 一个新 popup，每次都要重新解压（~3s），快速连点会挤多个实例、拉锯抖动。改为隐藏后，托盘左键发现 popup 在跑 → 写 `popup_activate` → 隐藏的 popup 自己 `deiconify+lift+topmost` 瞬时显示。

### 2.5 连不上（certifi SSL，v57）——同一 onefile 临时目录根因

报错 `...\_MEI549682\certifi\cacert.pem` 找不到。根因与 2.2 相同：onefile 临时目录被清理，`requests` 做 HTTPS 联网检测时找不到 CA 证书 → SSL 校验失败 → 判定“未连接/登录失败”。修：启动时把 `certifi.where()` 的 `cacert.pem` 复制到稳定的用户数据目录，并设 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` 指向它。

### 2.6 托盘生命周期重构（v58）——最终架构定型

- 旧：`tray_start()` 在 daemon 主线程 `icon.run_detached()` + daemon 主线程 `while not _shutdown` 循环 + runtime.json `popup_activate` IPC 激活窗口，复杂易踩线程坑。
- 新（沿用至今）：daemon **主线程** `icon.run(setup=_tray_setup)`（pystray 占用主线程跑 Windows 消息循环，官方推荐用法）；`setup` 回调里启动 `_daemon_worker` 独立线程跑网络检测/自动登录循环；删除 `run_detached`、`popup_activate` 等。

### 2.7 修改账号密码窗口问题（v59 → v61）

- **v59 打不开**：`cmd_init` 的 overrideredirect 窗口设了 `geometry` 后**没 `update_idletasks()/update()`**，窗口没真正应用尺寸并显示 → 加这两行 + `[INIT]` 日志。
- **v61 只闪一下**：根因同 2.1 的第 3 点——`CREATE_NEW_CONSOLE` 建控制台 + 保存后重启抢 `MUTEX_DAEMON`。修：`launch_windowed` 改 `CREATE_NO_WINDOW`；popup 的“修改账号密码” `Popen` 也改 `CREATE_NO_WINDOW` 并 `cwd=DIR`；`cmd_init` 去掉开头的 `hide_console`；`teardown` 去掉“保存后重启 popup+daemon”；新增 `MUTEX_INIT` 单例。

### 2.8 onedir 改造（4.0.1）+ 首装打不开 + 黑命令窗（4.0.0）

- **4.0.1 onedir**：从 onefile 改 onedir，彻底规避 `_MEI` 临时目录被清/删不掉的问题；用户数据迁到 `%LOCALAPPDATA%\Liwangtong`（config/log/runtime/popup.hwnd/cacert.pem），`DIR` 指向安装目录；新增 `_migrate_old_data()` 迁移旧数据。
- **4.0.0 两项**：
  - 命令黑窗：打包改 `console=False`。
  - 配置保存后主界面不出现：`teardown` 改为“daemon 未在跑才拉起；popup 未在跑 `launch_windowed(['popup'])`，在跑则 `activate_popup()` 置顶”。

### 2.9 安装器（Inno）与卸载遗留（v49/v50）

- **安装位置可自选**：`DisableDirPage=auto → no`（强制显示“选择目标位置”页）。
- **卸载遗留一个 exe**：安装后 daemon/popup 占用 `{app}\梨网通.exe`，卸载删除目录时被锁→遗留一个 exe。修：新增 `quit` 命令（置 `app_quit` + sleep 释放文件锁），`[UninstallRun]` 卸载前先 `梨网通.exe quit`，再加 `CloseApplicationsFilter=梨网通.exe`。

### 2.10 4.0.2 onedir 定稿 + 首装后主界面打不开

- **4.0.2**（本阶段上游）：onedir 定稿；新增 `launch_and_activate_popup(timeout=5.0)`——已存在且 HWND 有效→直接激活；进程在跑但 HWND 未就绪→轮询 5s；进程不存在→Popen + 轮询等激活。`teardown` 与 `_tray_open_popup` 统一走它。
- 生产中的真实坑：**「配置窗口用 `×` 关闭时没走 `teardown()`」**——`cmd_init` 是 `overrideredirect(True)` 窗口，只绑了 `<Escape>`，没绑 `root.protocol("WM_DELETE_WINDOW", teardown)`，导致 `config_editing=True` 残留在 runtime.json，daemon 一直认为“正在改配置”而暂停自动认证。修：补 `root.protocol("WM_DELETE_WINDOW", teardown)`。

> 到 4.0.2 为止，整个体系已经稳定：onedir 打包、三进程 IPC、单实例互斥、`app_quit` 专属退出标记、`manual_logout` 禁止自动重连、crash 遥测、无明文密码。后续（4.0.3 → 4.0.10）主要是在此基础上把“显示窗口 / 自动连接 / 退出程序”的边界继续理顺。

---

## 三、本阶段版本演进与修复记录

### 4.0.3 —— 托盘恢复主窗口：从「外部强改窗口」改为「自己收到请求再显示」

**症状**：窗口用 `×` 隐藏后，托盘左键打不开主界面（或唤不醒）。

**排查**：原链路是托盘发现 popup 存在时，直接调 `activate_popup()` 用 Win32 `ShowWindow/SetForegroundWindow` 强改 Tk 窗口。这在 onefile 时代 popup 经常“假激活/抖动”，且容易在窗口未完全初始化时拿到无效句柄。

**方案（核心的一句话）**：把「外部 Win32 强行恢复 Tk 窗口」改成「popup 自己收到请求后 `root.deiconify()`」。

- 新增 `POPUP_SHOW_FILE`（`popup.show`）+ `request_popup_show()/popup_show_requested()/clear_popup_show_request()`：托盘只负责“写一个显示请求文件”，不再强改窗口。
- 新增 `popup_window_valid()`：被动检查 HWND 是否有效，不再外部激活。
- `launch_and_activate_popup()` / `_tray_open_popup()`：已存在进程 → `request_popup_show()`；不存在 → 启动新进程并轮询 HWND 就绪。
- `cmd_popup()` 的 `tick()`：检测到 `popup.show` → `root.deiconify()+lift()+临时topmost` 自行恢复，并清掉请求文件；启动时清一次残留请求。
- **教训**：跨进程“操作别人的 UI”永远不如“让对方自己响应请求”稳定。用文件做轻量 IPC 比跨进程 Win32 操作窗口可靠得多。

### 4.0.4 —— 电信登不上：运营商后缀 `@dx` → `@aust`

**症状**：同学（电信用户，学号 `@aust`）用本程序登录失败。

**排查**：对照 AUST 官方仓库，其学生线路映射为 `电信→aust / 联通→unicom / 移动→cmcc`，登录串是 `学号@服务器`。而本项目写的是 `电信→@dx`，导致电信账号 `学号@dx` 被拒绝。

**方案**：改 `OPERATORS` 表 `{"id":"aust","name":"电信","suffix":"@aust"}`，`DEFAULT_SUFFIX`（`@unicom`）不变。确认 `id` 字段只用于配置窗口内部选择、**不落盘**（config 存的是 `suffix`），凭据仍按固定 `CM_TARGET` 读取，故改动零迁移风险、安全。
- **教训**：运营商这类“网络环境强相关”的映射，一定要以目标校园网的真实实现为准，不能拍脑袋。

### 4.0.5 —— 开机自启“显示已连接但实际未连接” + 开机默认连接一次

**症状**：断网/开机自启后，界面显示“已连接”，但校园网实际上没认证。

**排查**：两个叠加根因。
1. `DrcomClient.get_status()` 有回退逻辑：`chkstatus` 失败就 `if internet_ok(): return connected=True`。也就是“百度能访问 = 校园网已连接”，这在开机/外网通但未认证时是**误判**。
2. `daemon_state_read()` 默认返回 `False`（后台守护默认关），导致自启后 daemon 空闲、不连接、也不写真实状态；popup 默认 `state` 又是 `"connected"`，于是显示假“已连接”。

**方案**：
- `get_status()` 改严格：**仅 `chkstatus` 明确 `result=="1"` 才 `connected=True`**；`is_connected()` 同步改严格。外网可达不再作为校园网已认证依据。
- `daemon_state_read()` 默认改 `True`（开机自启=自动连接；用户手动关掉后 runtime 记录 `False` 仍生效）。
- `_daemon_worker()` 增加「开机首次认证」：等 `smart_sleep(2)` 让网卡/DHCP 初始化 → 严格探测 → 未认证则 `login_with_retry()` → 再 `get_status()` 复核 → 写真实状态。
- **配套**：`teardown()`（首次安装）不再写 `daemon_state_write(False)`，否则会覆盖新的默认 `True`。
- **教训**：“可达”不等于“已认证”。状态判定要锚定权威来源（这里就是 Dr.COM 的 `result`）。

### 4.0.6 —— 开机自动登录不该被「守护开关」卡住

**症状**（用户实测 4.0.5 后）：重启仍不自动登录，问“是不是要先开守护功能”。

**排查**：4.0.5 的“开机首次认证”被 `daemon_state_read()` 门控了。而用户机器 `runtime.json` 里是旧版留下的 `daemon_enabled:false`，于是开机时这次认证被跳过。

**方案**：把「开机首次认证」从“依赖守护开关”改为**无条件执行**（只要有账号且未手动注销）——`if not _shutdown and not manual_logout_read()`。守护开关只负责“掉线后是否持续自动重连”，不再决定“开机连不连”。同时修 daemon 空闲分支的托盘显示，改为如实反映 runtime 状态，避免恒显“连接中”。
- **教训**：“开机连一次”和“持续自动重连”是两个不同需求，别用一个布尔开关把它们绑死。

### 4.0.7 —— 卸载残留：退出信号退得慢导致文件被锁

**症状**：卸载完成后 `梨网通.exe` 和 `_internal` 删不掉（“Some elements could not be removed”）。

**排查**：daemon 可能在最长 30s 的 `smart_sleep(interval)` 里睡觉，而 `smart_sleep` 不检查 `app_quit`；`cmd_quit()` 只 `sleep(2)`。于是卸载时 daemon 还活着、锁着 `梨网通.exe`/`_internal` 里的 DLL，Inno 删不掉。

**方案**：
- `smart_sleep()` 检测到 `app_quit` → `_shutdown=True` 并跳出（不再睡满 30s）。
- `cmd_quit()` 改为**等到 daemon 和 popup 进程真正都没了**（最多 15s，轮询 `is_daemon_running()/is_popup_running()`）再返回，确保文件锁释放。

### 4.0.8 —— 我自己引入的一个致命 Bug：`smart_sleep` 缺 `global _shutdown`

**症状**（4.0.7 后）：开机仍不自动登录，日志报：
```
[CRASH][thread] UnboundLocalError: local variable '_shutdown' referenced before assignment
  File "campus_login.py", line 988, in _daemon_worker
  File "campus_login.py", line 654, in smart_sleep
```

**排查**：我在 `smart_sleep` 里写了 `_shutdown = True`（让它响应退出），但函数里**没有 `global _shutdown`**。Python 因此把 `_shutdown` 当局部变量，循环首行 `not _shutdown` 就“未绑定” → daemon 工作线程一启动就崩 → 开机登录根本没跑。

**方案**：`smart_sleep` 顶部加 `global _shutdown`。用 `python -c` 直接调 `smart_sleep` 回归验证不再报错。
- **教训**：**在任何函数内对模块级全局变量赋值，都必须声明 `global`。** 越小的函数越容易漏，回归测试（哪怕是 `python -c` 调一下）能立刻暴露。

### 4.0.9 —— 退出后进程没杀死 + 左键托盘弹窗闪退（app_quit 生命周期竞态）

**症状**：点「退出」后 daemon/托盘进程没死；左键托盘 → 主界面闪一下又退，但托盘仍在。

**排查**（读日志 + runtime）：
- `runtime.json` 里 `app_quit:true` 卡住了。
- **真正的断点**：daemon 的托盘主线程 `icon.run()` 是**阻塞**的消息循环，**只有 `icon.stop()` 才会返回**。而「从 popup 点退出」只写了 `app_quit=true` 并把 popup `os._exit`，**没去停 daemon 的托盘**。于是：popup 死、daemon 工作线程退，但 `icon.run()` 还挂着 → daemon/托盘进程残留。之后左键托盘又拉起 popup，popup 一看 `app_quit=true` → 立刻 `popup_exit()` → 闪退。

**方案**（全链路兜底）：
- 新增 `_stop_tray()`（内部 `_tray_icon.stop()`）。
- `_daemon_worker()` 检测到 `app_quit` → `_shutdown=True` + **`_stop_tray()`** → `icon.run()` 返回 → daemon 真正退出。
- `smart_sleep()` 同样加 `_stop_tray()`。
- `_tray_exit()` 统一走 `_stop_tray()`。
- `cmd_daemon()` 在 `_tray_run()` 后加 `_stop_tray()` 最后保险。
- `_tray_open_popup()` / `launch_and_activate_popup()`：`app_quit_requested() or _shutdown` 时**拒绝启动/恢复 popup**，防止退出状态下拉起弹窗闪退。
- **保留** popup 端 `if app_quit_requested(): popup_exit()`（真退出后弹窗本就该死，不能为了防闪退而删掉）。

**实测**：`quit` → `守护工作线程退出 → icon.run() 已退出 → 托盘已退出 → 守护退出`，进程残留 0。

### 4.0.10 —— 退出后“打不开程序”：双击 exe 走的是无界面的 `login`

**症状**：退出后双击 exe，没反应（打不开程序）。

**排查**：双击 `梨网通.exe`（无参数）→ `main()` 默认 `cmd="login"` → 走 `cmd_login()`（命令行登录），**静默网络请求后进程退出，不显示任何窗口**。主界面窗口只有通过托盘才出现。

**方案**：
- `main()`：默认 `login` + 已配置账号 → 视为「打开程序」——清掉残留 `app_quit` → 确保 daemon 在跑 → `launch_and_activate_popup()` **弹出主界面窗口**。
- `.iss` 的快捷方式 `Parameters` 从 `"daemon"` 改为空（双击快捷方式也直接出窗口）；**开机自启仍用 `daemon`**（只出托盘，安静）。

**实测**：无参数启动 → daemon+popup 两进程常驻、popup 窗口打开；`quit` 后进程残留 0。

---

## 四、架构与约定（本阶段沉淀）

1. **显示窗口**：托盘/其它进程从不直接 `ShowWindow` 别人的 Tk 窗口，而是写 `popup.show` 请求文件，由 popup 自己 `deiconify()`。
2. **自动连接**：「开机首次认证」无条件执行（有账号且未手动注销）；「后台守护」开关只控制掉线后是否持续重连。
3. **状态判定**：`get_status()` 严格以 Dr.COM `chkstatus result=="1"` 为准，外网可达不算已连接。
4. **退出**：只有「退出按钮 / 托盘退出」写 `app_quit=true`；daemon 工作线程检测到它 → `_stop_tray()` 停掉 `icon.run()` → daemon 进程真正结束。popup 对 `app_quit` 无条件 `popup_exit()`。
5. **会话生命周期**：只有拿到 `MUTEX_DAEMON` 的 daemon 才在会话开始时清 `app_quit`（保证单实例、无“退出中被二次启动清标记”的竞态）。
6. **卸载**：`quit` 会等到 daemon/popup 全退出再返回，确保 exe/`_internal` 文件锁释放。
7. **一键打开**：双击 exe 或快捷方式（无参数）→ 清退出标记 + 起 daemon + 弹主界面窗口。

---

## 五、遗留 / 待改进（低优先级）

- popup 的兜底默认状态仍是 `rt.get("state","connected")`（本阶段未改，因为开机链路已能如实写状态；若在“守护关闭且无历史状态”时打开界面，仍可能短暂兜底成已连接，可考虑改成 `disconnected`）。
- 密码机制本身没问题（存一次到凭据管理器，不落明文、不累积），无需改动。
- daemon 断网重连最坏约 30s~54s（grace_period + 重试），不卡死但偏慢。
- `get_status()` 每次失败都会打 `[STATUS] chkstatus 请求失败` 日志，轮询频繁时日志略多，可考虑降频。

---

## 六、经验总结（写给未来的自己）

- **多进程协作，先用日志/共享状态把“谁在什么时候该干什么”理清。** 这些 bug 几乎全是“职责边界不清”或“信号没传到真正该响应它的人”。
- **不要用跨进程 Win32 去操作别人的 UI。** 用轻量 IPC（文件/共享状态）+ 让对方自己响应。
- **在函数里给模块级全局变量赋值，必须 `global`。** 越小的函数越容易漏。
- **“可达 ≠ 已认证”、“能连接 ≠ 已连接”。** 状态判定一定要锚定权威来源。
- **开机连一次 和 持续重连 是两个需求，别用一个布尔开关绑死。**
- **每改一处，最好用真实进程做个能自动验证的冒烟测试**（本阶段我用 `python -c` 回归 `smart_sleep`、用真实 exe 跑 `daemon`→`quit` 验证退出链路），能立刻暴露“看似改了但其实崩了”的问题。

—— 定稿于 2026-09-06，v4.0.10
