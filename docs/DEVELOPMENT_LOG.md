# 开发记录

这个项目真正的难点不在「怎么发登录请求」，而在**多进程的生命周期**：daemon（托盘+守护）、popup（主界面）、init（配置窗口）是三个独立进程，靠一个共享的 `runtime.json` 和三把命名互斥锁协作。谁负责退出、什么时候该清标记、谁的窗口该由谁来显示，每一步都踩到了坑。

下面按版本记，从 4.0.0 之前的 onefile 时代开始。前面那段是最惨烈的拉锯期，症状相似、根因不同，好几版都是「以为修好了，其实没修好」。

---

## 一、onefile 时代（v49 → v61）

### 1.1 一批相互纠缠的症状

- **点注销会把整个程序退掉**：popup 的定时器里有 `if quit_requested(): os._exit(0)`，而 popup 和 daemon 共用同一个 `runtime.json`。daemon 正常退出时会写 `quit=True`，popup 把「daemon 要退了」误判成「用户要退出软件」，于是被一起带走。
- **注销看起来没效果**：注销后 daemon 立刻自动重连，界面刚变「未连接」就弹回「已连接」。
- **修改账号密码的窗口只闪一下**：`init` 用 `CREATE_NEW_CONSOLE` 启动（人为建了个控制台又 `hide_console`，所以会闪），保存后又去重启 daemon+popup，跟已经在跑的 daemon 抢 `MUTEX_DAEMON`，新实例拿不到锁瞬间退出。
- **偶尔连不上**：报错指向 onefile 临时目录里的 `certifi\cacert.pem` 找不到。

### 1.2 转折点：把崩溃记下来（v55/v56）

之前全靠猜。加上 `_setup_crash_logging()`（接 `sys.excepthook` / `threading.excepthook` / `Tk.report_callback_exception`，全部写进日志文件）之后，第一次拿到真实报错：

```
[CRASH][thread] FileNotFoundError: ...\_MEI864962\base_library.zip
  at net_worker → resolve_password → cred_read
```

**onefile 的临时目录（`_MEIxxxxxx`）会在运行中被清掉**。后台线程再去懒加载 `utf-16-le` 编解码器就 `FileNotFoundError`；而 `net_worker` 把取密码的调用写在 `try` 之外，线程一崩，`last_net` 就没写出去 → 注销界面永久转圈。

改法两条一起上：启动时预加载常用编码（utf-8 / utf-16 / utf-16-le / utf-16-be / latin-1 / ascii / gbk），`net_worker` 里取密码、发请求、写状态全部包进 `try/except`，保证 `last_net` 一定写。

### 1.3 真正的根因常常很蠢：`ctypes.DWORD`（v54）

`is_popup_running()` / `is_daemon_running()` 里写成了 `ctypes.DWORD` —— 这玩意儿根本不存在，应该是 `wintypes.DWORD`。`AttributeError` 被裸 `except: return False` 吞掉，于是**这两个探测函数永远返回 False**。

结果就是：托盘左键永远认为「popup 没在跑」→ 每次都 `Popen` 一个新实例 → 多个实例互相抢单例、onefile 解压又慢 → 表现为「唤不醒 / 抖动 / 左键没反应」。

全文替换成 `wintypes.DWORD` 之后，凭据结构体的 argtypes、两个探测函数一起好了。这种事查起来最费时间。

### 1.4 托盘左键链路的连环修正（v49 → v53）

- **v50**：`OpenMutexW` 的访问掩码写成 `0x1F001`，正确是 `MUTEX_ALL_ACCESS = 0x1F0001`，少个 0，探测恒失败。
- **v51**：裸 `except: pass` 把所有启动失败吞成「点了没反应」。改成 `_tray_open_popup` / `_tray_run` / `tray_start` 全程打 `[TRAY]` 日志，启动前 `os.path.isfile(exe)` 校验，打印 PID；`acquire_singleton` 保存句柄并记 `[MUTEX]`。
- **v52**：把「激活已有窗口」从 `runtime.json` 轮询改成直接记 HWND 文件（`popup.hwnd`），用 `IsWindow` 校验 + `ShowWindow` + `SetForegroundWindow` + 短暂置顶。
- **v53（语义变了）**：右上角 `×` / `Esc` 从 `os._exit(0)` 改成 `root.withdraw()` —— 进程、单例锁、HWND 全留着，只是看不见。因为 onefile 下每次左键都要 `Popen` 一个新 popup、重新解压（约 3 秒），连点就挤出一堆实例互相拉锯。改成隐藏后，托盘发现 popup 还活着，只要让它自己显示出来。

### 1.5 certifi 的 SSL 问题（v57）

`...\_MEI549682\certifi\cacert.pem` 找不到，根因和 1.2 是同一个（onefile 临时目录被清）。改：启动时把 `certifi.where()` 指向的文件复制到用户数据目录，并把 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` 指过去。

### 1.6 托盘生命周期定型（v58）

- 旧写法：daemon 主线程 `icon.run_detached()` + 自己再写一个 `while not _shutdown` 循环 + 用 `runtime.json` 传「激活窗口」的信号。看着灵活，实际全是线程坑。
- 现在的写法：daemon **主线程**直接 `icon.run(setup=_tray_setup)`（pystray 在 Windows 上就是占住一个线程跑消息循环，官方推荐），网络检测和自动登录放到 `setup` 回调里起的独立线程。`run_detached`、`popup_activate` 全删了。

### 1.7 配置窗口（v59 → v61）

- **v59 打不开**：`cmd_init` 的 `overrideredirect(True)` 窗口设了 `geometry` 之后没有 `update_idletasks()` / `update()`，尺寸和显示都没真正生效。补上，并加 `[INIT]` 日志。
- **v61 只闪一下**：根因就是 1.1 第三条。`launch_windowed` 改 `CREATE_NO_WINDOW`，popup 里 `Popen` 也改掉并带上 `cwd=DIR`；`cmd_init` 去掉开头的 `hide_console`；`teardown` 去掉「保存后重启 popup+daemon」；再加一把 `MUTEX_INIT` 保证配置窗口自己也不许多开。

## 二、转向 onedir（4.0.0 → 4.0.2）

- **4.0.0**：打包改 `console=False`（黑窗一闪的问题）；修「配置保存后主界面不出现」——`teardown` 改成「daemon 没在跑才拉起；popup 没在跑就 `launch_windowed(['popup'])`，在跑就 `activate_popup()` 置顶」。
- **4.0.1**：onefile → **onedir**，一次性绕开所有 `_MEI` 临时目录被清 / 删不掉的问题。用户数据同时迁到 `%LOCALAPPDATA%\Liwangtong`（config / runtime / log / popup.hwnd / cacert.pem），程序目录 `DIR` 只读；加了 `_migrate_old_data()` 把老位置的文件搬过去。
- **4.0.2**：新增 `launch_and_activate_popup(timeout=5.0)` 统一入口：已有进程且 HWND 有效 → 直接激活；进程在跑但 HWND 还没就绪 → 轮询等 5 秒；进程不存在 → `Popen` 后轮询等它就绪。`teardown` 和 `_tray_open_popup` 都走它。
  同期一个真实生产坑：**配置窗口用 `×` 关掉时没走 `teardown()`**。`cmd_init` 是 `overrideredirect` 窗口，只绑了 `<Escape>`，没绑 `root.protocol("WM_DELETE_WINDOW", teardown)`，导致 `config_editing=True` 留在 `runtime.json` 里，daemon 一直以为「用户正在改配置」而暂停自动认证。补上 `WM_DELETE_WINDOW` 绑定。

到这里整体就稳了：onedir 打包、三进程、单实例互斥、`app_quit` 专属退出标记、`manual_logout` 抑制重连、崩溃遥测、密码只进凭据管理器。后面 4.0.3 起都是在把「显示窗口 / 自动连接 / 退出程序」的边界继续磨细。

## 三、4.0.3 之后的逐版修复

### 4.0.3 托盘恢复主窗口：从「外部强改窗口」改成「自己收到请求再显示」

`×` 隐藏之后托盘左键打不开界面（或者唤不醒）。原来的链路是托盘直接调 `activate_popup()`，用 Win32 `ShowWindow` / `SetForegroundWindow` 去改别人的 Tk 窗口 —— onefile 时代就经常「假激活 + 抖动」，还容易在窗口没初始化完时拿到无效句柄。

改成 popup 自己响应：

- 新增 `popup.show` 请求文件 + `request_popup_show()` / `popup_show_requested()` / `clear_popup_show_request()`，托盘只负责写一个文件。
- 新增 `popup_window_valid()` 只被动检查 HWND。
- `cmd_popup()` 的 `tick()` 发现 `popup.show` 就 `deiconify() + lift() + 临时 topmost`，然后自己删掉请求文件；启动时也清一次残留。

**教训**：跨进程「操作别人的 UI」永远不如「让对方自己响应请求」稳定。用文件做轻量 IPC 比跨进程 Win32 调用可靠得多。

### 4.0.4 电信登不上：后缀 `@dx` → `@aust`

有同学（电信）用不了。对照网页端实际提交的参数，学校三条线是 `电信→@aust`、`联通→@unicom`、`移动→@cmcc`，登录串是 `学号@服务器`；程序里写的 `电信→@dx` 是拍脑袋来的，直接被拒。

改 `OPERATORS` 表里电信那条，`DEFAULT_SUFFIX`（`@unicom`）不动。确认过 `id` 字段只在配置窗口内部选择用、**不落盘**（配置里存的是 `suffix`），凭据也是按固定的 `CM_TARGET` 读，所以这个改动零迁移风险。

**教训**：运营商映射这类跟网络环境强相关的东西，必须以目标校园网实际提交的内容为准。

### 4.0.5 「显示已连接但实际没连上」+ 开机默认连一次

断网或开机自启之后，界面写「已连接」，校园网其实没认证。两个根因叠在一起：

1. `DrcomClient.get_status()` 有条回退：`chkstatus` 失败就 `if internet_ok(): return connected=True`——「百度能打开 = 校园网已连接」，在「外网通但未认证」时是彻底误判。
2. `daemon_state_read()` 默认 `False`（守护默认关），自启后 daemon 空闲、不认证也不写真实状态；而 popup 的兜底默认 `state` 又是 `"connected"`，于是显示了一个假的「已连接」。

改：

- `get_status()` 严格化，只有 `chkstatus` 明确回 `result == "1"` 才算已连接，`is_connected()` 同步。
- `daemon_state_read()` 默认改 `True`（用户手动关掉之后 runtime 里会记 `False`，仍然生效）。
- `_daemon_worker()` 加「开机首次认证」：先 `smart_sleep(2)` 等网卡和 DHCP 起来 → 严格探测 → 没认证就 `login_with_retry()` → 再 `get_status()` 复核 → 写真实状态。
- 配套：`teardown()`（首次安装那条路）不再写 `daemon_state_write(False)`，否则会把新默认值盖掉。

**教训**：「可达」不等于「已认证」。状态判定必须锚定权威来源，这里就是 Dr.COM 自己回的 `result`。

### 4.0.6 开机自动登录不该被守护开关卡住

4.0.5 放出去的版本，重启还是不自动登录。原因是「开机首次认证」被 `daemon_state_read()` 门控了，而老机器 `runtime.json` 里躺着上一版写的 `daemon_enabled:false`，这次认证就被跳过了。

改：开机那次认证**无条件执行**（有账号且没被手动注销），守护开关只管「掉线后是否持续重连」。顺手把 daemon 空闲分支的托盘显示改成如实反映 runtime 状态，不再恒显「连接中」。

**教训**：「开机连一次」和「持续自动重连」是两个需求，别用一个布尔量把它们绑死。

### 4.0.7 卸载残留：退出信号退得太慢，文件还锁着

卸载完 `梨网通.exe` 和 `_internal` 删不掉（"Some elements could not be removed"）。daemon 可能正睡在最长 30 秒的 `smart_sleep(interval)` 里，而 `smart_sleep` 不检查 `app_quit`；`cmd_quit()` 又只 `sleep(2)` 就返回。于是卸载器动手时 daemon 还活着，DLL 和 exe 都是锁着的。

改：`smart_sleep()` 看到 `app_quit` 立刻置 `_shutdown` 跳出；`cmd_quit()` 改成轮询 `is_daemon_running()` / `is_popup_running()`，等两个进程真没了（最多 15 秒）才返回。

### 4.0.8 我自己写出来的一个致命 bug：`smart_sleep` 少了 `global`

4.0.7 之后开机更不登录了，日志：

```
[CRASH][thread] UnboundLocalError: local variable '_shutdown' referenced before assignment
  File "campus_login.py", line 988, in _daemon_worker
  File "campus_login.py", line 654, in smart_sleep
```

在 `smart_sleep` 里写了 `_shutdown = True`（想让它响应退出），但忘了在函数顶部声明 `global _shutdown`。Python 于是把 `_shutdown` 当局部变量，循环第一行的 `not _shutdown` 就直接「未绑定」→ daemon 工作线程一启动就崩 → 开机登录根本没跑起来。

补 `global _shutdown`，然后 `python -c` 直接调一次 `smart_sleep` 做回归。

**教训**：在函数里给模块级变量赋值就必须 `global`，越小的函数越容易漏。回归哪怕只是 `python -c` 调一下，也能立刻暴露「改了但其实崩了」。

### 4.0.9 退出后进程没杀死 + 左键托盘闪退（`app_quit` 生命周期竞态）

现象：点「退出」之后 daemon / 托盘进程还在；再左键托盘，主界面闪一下就退，托盘仍在。

读日志和 `runtime.json` 定位到的断点：daemon 主线程的 `icon.run()` 是**阻塞**的，只有 `icon.stop()` 才会返回。而「从 popup 点退出」只写了 `app_quit=true` 并把 popup `os._exit`，**没人去停 daemon 的托盘**。于是 popup 死了、工作线程退了，`icon.run()` 还挂着 → 进程残留；之后左键托盘又拉起一个 popup，popup 一看 `app_quit=true` 立刻 `popup_exit()` → 闪退。

全链路补兜底：

- 新增 `_stop_tray()`（内部调 `_tray_icon.stop()`）。
- `_daemon_worker()` 检测到 `app_quit` → `_shutdown = True` + `_stop_tray()` → `icon.run()` 返回 → daemon 真退出。
- `smart_sleep()` 里同样加 `_stop_tray()`。
- `_tray_exit()` 统一走 `_stop_tray()`。
- `cmd_daemon()` 在 `_tray_run()` 之后再加一次 `_stop_tray()` 作为最后保险。
- `_tray_open_popup()` / `launch_and_activate_popup()` 在 `app_quit_requested() or _shutdown` 时**拒绝**启动或恢复 popup。
- popup 那边的 `if app_quit_requested(): popup_exit()` 保留 —— 真退出之后弹窗本来就该死，不能为了藏闪退把它删了。

实测 `quit` 之后日志顺序是：守护工作线程退出 → `icon.run()` 已退出 → 托盘已退出 → 守护退出，进程残留 0。

### 4.0.10 退出后「打不开程序」：双击 exe 走的是无界面的 login

退出之后双击 exe 没反应。原因：无参数启动 → `main()` 默认 `cmd = "login"` → `cmd_login()` 静默发一次登录请求就退进程，**一个窗口都不弹**。界面只有从托盘才出得来。

改：

- `main()` 里，默认 `login` + 已有账号 → 当成「打开程序」处理：清掉残留的 `app_quit` → 确保 daemon 在跑 → `launch_and_activate_popup()` 弹主界面。
- `.iss` 里快捷方式的 `Parameters` 从 `"daemon"` 改成空（点快捷方式也直接出窗口）；**开机自启仍然用 `daemon`**，只出托盘，安安静静。

## 四、沉淀下来的几条约定

1. **显示窗口**：别的进程绝不直接 `ShowWindow` popup 的 Tk 窗口，只写 `popup.show`，由 popup 自己 `deiconify()`。
2. **自动连接**：开机那次认证无条件执行；「后台守护」开关只决定掉线后要不要持续重连。
3. **状态判定**：只认 `chkstatus` 的 `result == "1"`，外网可达不算已连接。
4. **退出**：只有「退出」按钮和托盘退出菜单写 `app_quit=true`；daemon 工作线程检测到它必须去 `_stop_tray()`，否则进程退不掉。popup 见到 `app_quit` 无条件退出。
5. **标记的生命周期**：只有拿到 `MUTEX_DAEMON` 的那个 daemon 才在会话开始时清 `app_quit`，避免「正在退出时被二次启动把标记擦掉」的竞态。
6. **卸载**：`quit` 要等到 daemon / popup 进程真的消失再返回，保证文件锁释放。
7. **一键打开**：双击 exe 或快捷方式（无参数）= 清退出标记 + 起 daemon + 弹主界面。

## 五、还没处理的

- popup 的兜底默认状态还是 `rt.get("state", "connected")`。开机链路已经能如实写状态，所以暂时没动；但「守护关着 + 没有历史状态」时打开界面，仍可能短暂显示成已连接，应该改成 `disconnected`。
- 断网后的重连最坏要 30~54 秒（宽限期 + 重试 + 等待），不卡死，但偏慢。要不要改成事件驱动还没想好。
- `get_status()` 每次失败都打一行 `[STATUS] chkstatus 请求失败`，轮询密的时候日志挺吵，需要降频或合并。
- 多处 `os._exit(0)` 会跳过清理（临时文件、托盘句柄），目前只在 popup 侧用，影响可控，属 P2。
- 认证判断里 `"ac0=" in t` 这个条件写得偏宽，`result == "1"` 又偏严。实测学校环境没问题就不动它 —— 这块改错了会直接影响登录判定，属于「知道不好但先留着」。

## 六、几句总结

- 多进程协作，第一件事是用日志和共享状态把「谁在什么时候该干什么」写清楚。这一轮的 bug 几乎全是职责边界不清，或者信号没传到真正该响应它的那个进程。
- 别跨进程操作别人的 UI。文件做轻量 IPC + 让对方自己响应。
- 「可达 ≠ 已认证」、「能连 ≠ 已连」。状态一定要锚定权威来源。
- 裸 `except: return False` 是排查杀手，吞掉的正是你要找的那条线索。
- 每改一处，尽量留一个能自动跑的最小验证（`python -c` 调一下、真实 exe 跑一遍 `daemon` → `quit`）。「看着改了其实崩了」这种事只有冒烟测试能当场抓住。
