# 架构说明

一个 exe，多种身份。`campus_login.py` 里所有功能都在同一个文件中，靠 `sys.argv[1]` 决定这次启动是干什么的：

```
梨网通.exe [login|daemon|popup|init|config|status|diagnostic|autostart|quit]
                     │
        ┌────────────┼─────────────┬───────────────┐
        ▼            ▼             ▼               ▼
     daemon       popup         init            login
   托盘+守护     主界面窗口    账号密码配置    单次登录（脚本用）
        │            │             │
        └──── runtime.json / popup.hwnd / popup.show ───┘
                    文件 + Windows 命名互斥锁
```

## 为什么拆成三个进程

一开始就是一个进程：pystray 的图标跑在子线程里，Tk 主循环跑在主线程里，看起来挺合理，实际全是坑。

- pystray 在 Windows 上要占住一个线程跑消息循环，`icon.run()` 是阻塞的；Tk 的 `mainloop()` 也一样。两个都想要主线程。
- 界面崩了会把守护一起带走，用户表现就是「托盘图标没了，校园网还在线」。
- 打包成 onefile 之后更糟：托盘左键一次 `Popen` 一个新 popup，每次都要重新解压临时目录，快速连点会挤出一堆实例互相抢单例。

所以现在是：**daemon 只管托盘和网络，popup 只管界面，init 只管改配置**，三者各自是独立进程，谁挂了都不影响别人。

`_tray_run()` 里 daemon 主线程直接跑 `icon.run(setup=_tray_setup)`，`setup` 回调里再开一个工作线程 `_daemon_worker` 跑网络检测/重连循环 —— 网络逻辑不能占托盘线程，托盘消息循环也不能挪到子线程去。

## 进程之间怎么说话

没有 socket，没有管道，就三个文件加三把命名互斥锁，都在 `%LOCALAPPDATA%\Liwangtong\` 下：

| 文件 | 作用 |
|------|------|
| `runtime.json` | 共享状态：`state` / `ip` / `daemon_enabled` / `app_quit` / `manual_logout` / `config_editing` / `theme` |
| `popup.hwnd` | 主界面窗口的 HWND，别处要置顶界面就读它 |
| `popup.show` | 一个「请把界面显示出来」的请求标记文件，存在就该弹窗显示自己 |

`runtime.json` 的写入一律走「写 `xxx.<pid>.tmp` 再 `os.replace`」，避免两个进程同时写把 JSON 写坏（早期真的出现过读半个 JSON 的情况）。`<pid>.tmp` 残留由启动时的 `_cleanup_orphan_tmp()` 收掉。

单实例用 `kernel32.CreateMutexW` / `OpenMutexW`：

```
Local\LiwangtongDaemon   → daemon
Local\CampusLoginPopup   → popup
Local\CampusLoginInit    → init
```

`is_daemon_running()` / `is_popup_running()` 就是去 `OpenMutexW` 试一下能不能打开（`MUTEX_ALL_ACCESS = 0x1F0001`，这里踩过一次掩码写错的坑）。拿不到锁的进程自己退出，保证同一时刻只有一个托盘。

### 显示界面这件事

托盘不去 `ShowWindow` 别人的 Tk 窗口 —— 跨进程操作别人的 UI 永远不稳。改成：

```
托盘左键 → 读 popup.hwnd，发现 popup 活着
        → 写 popup.show 文件
popup 的 tick() 定时器发现 popup.show 存在
        → root.deiconify() + lift() + 短暂 topmost → 删掉 popup.show
```

popup 的 `×` 也只是 `withdraw()`（藏起来），进程、单例锁、HWND 都留着，所以第二次从托盘唤出是瞬时的。真正销毁进程的是「退出」按钮和托盘退出菜单。

## 认证层

`DrcomClient` 封装 Dr.COM 的 HTTP 接口，基地址是常量 `BASE_URL`（校内网关，写死的）：

```
状态   GET  /drcom/chkstatus?callback=dr1002   → JSONP，result == "1" 才算已认证
登录   POST /a79.htm                            → 主通道，认 "login_ok" / "认证成功" / UID
       GET  /drcom/login                        → 兜底一
       POST /drcom/login                        → 兜底二
注销   GET  /drcom/logout?callback=dr1004
```

登录三个通道顺序试，任一成功即返回。账号是 `学号 + 运营商后缀`（`@unicom` / `@aust` / `@cmcc`），后缀对用户隐藏，界面上选「联通/电信/移动」，配置里存的是后缀。

**状态判定是严格的**：只有 `chkstatus` 明确回 `result=1` 才叫已连接。早期版本有一条「百度能打开就算已连接」的兜底，开机自启时会造成「界面显示已连接、其实没认证」的误判（校园网没认证时外网会被劫持到网关，反向也一样骗人），后来彻底删掉了。

## 守护循环

`_daemon_worker()` 大致这样跑：

```
启动
 ├─ 等 2s（让网卡 / DHCP 就绪）
 ├─ 严格探测状态；未认证 → login_with_retry()（最多 3 次，退避 2^n）
 └─ 复核状态，写 runtime.json
循环（每 30s，smart_sleep 可被 app_quit 打断）
 ├─ 守护开关关着 → 只更新托盘显示，不重连
 ├─ 已认证 → 什么都不做
 ├─ 刚登录成功 15s 内（grace_period）→ 跳过，不误判掉线
 ├─ 未认证 且 用户手动注销过 → 不重连（manual_logout 标记）
 └─ 未认证 → 重新登录，成功后发系统通知
```

「开机连一次」和「持续自动重连」是两件事：前者无条件执行，后者才受守护开关控制。这两点在 4.0.5 / 4.0.6 之间来回改过一轮，见 [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md)。

## 凭据

密码只进 Windows 凭据管理器，用 `advapi32` 的 `CredWriteW` / `CredReadW` / `CredDeleteW`（ctypes 手写的 `CREDENTIALW` 结构体，没用 pywin32）。目标名固定 `campus_login`，类型 `CRED_TYPE_GENERIC`。

`resolve_password()` 优先读凭据；如果发现老版本 `config.json` 里残留了明文 `password` 字段，会自动迁进凭据管理器并把明文从配置里删掉。日志里用户名只打前三位（`2021***@unicom`）。

## 退出链路

这是整个程序最容易出事的部分，改过三次：

```
「退出」按钮 / 托盘退出菜单
   → request_app_quit()  写 runtime.json: app_quit = true
   → _daemon_worker 在下一次 smart_sleep 检查到 → _shutdown = True → _stop_tray()
   → icon.run() 返回 → cmd_daemon 收尾 → daemon 进程结束
   → popup 检查到 app_quit → popup_exit() → 进程退出
```

关键点：`icon.run()` 不返回，进程就不会结束，所以**必须显式 `icon.stop()`**，光写标记是不够的（4.0.9 之前就是这个 bug：popup 退了，托盘还挂着，之后再点托盘会拉起一个「看到 app_quit 立刻自杀」的 popup，表现为闪退）。

`app_quit` 的清除也只在**拿到 daemon 单例锁之后**做一次，避免退出过程中被二次启动把标记擦掉。

`quit` 子命令是给 Inno 卸载用的：写完标记后要轮询等到 daemon 和 popup 的进程真的消失（最多 15s）才返回，否则 exe 和 `_internal` 还锁着，卸载会遗留文件。

## 崩溃可观测性

`_setup_crash_logging()` 同时挂 `sys.excepthook`、`threading.excepthook` 和 `Tk.report_callback_exception`，任何线程/回调里没接住的异常都会落到 `campus_login.log` 前面加 `[CRASH]`。窗口类程序不接这些的话，异常是静默消失的，很多问题根本查不到。

日志写入带轮转（超 512KB 截到最后 200 行），`log()` 里的 `print` 也包了 try，避免 `console=False` 打包后 stdout 不可用导致日志函数自己抛异常。

## 已知取舍

- 全部代码在一个文件里，约 2400 行。拆模块对 PyInstaller 打包和「拷到别的机器上直接跑」没什么好处，就没拆。
- 认证网关地址写死，不支持多校区/多环境切换，改学校要改源码。
- 界面是 tkinter + PIL 手绘圆角，没有用任何 UI 框架。DPI 靠 manifest 的 PerMonitorV2 加代码里的手动缩放。
- 多处 `os._exit(0)` 会跳过清理（临时文件、托盘句柄），目前只在 popup 侧使用，影响可控。
- 没做代码签名，SmartScreen 会拦一下，属正常现象。
