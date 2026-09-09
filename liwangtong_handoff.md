# 🍐 梨网通 — Handoff 交接文档

## 项目概述
Dr.COM 校园网自动登录客户端，Python 3.8 + tkinter + PIL，PyInstaller onefile 打包，Inno Setup 安装。纯 Windows 专用（winreg/ctypes.windll/pystray/Credential Manager）。

## 关键路径
- 工作区：`D:\deep Workspace\campus_login_v2\`
- **主源码**：`campus_login.py`（~1904 行，含全部修复）
- **备份**：`BACKUP_campus_login.py`
- **历史备份**：`campus_login.bak_*.py`（各迭代版）
- **全局色常量**：`C_GREEN/C_BLUE/C_PINK/C_PEAR`（line ~48）
- **安装脚本**：`campus_installer.iss` → `output\LiWangTong-Setup-3.2.0.exe`
- **桌面安装包**：`C:\Users\33389\Desktop\LiWangTong-Setup-3.2.0.exe`
- **打包**：`C:\Program Files\python\Scripts\pyinstaller.exe`（产物在 `D:\deep Workspace\dist\campus_login.exe`）
- **Inno**：`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`
- **测试床**：`test_run\梨网通.exe`（部署测试）

## 当前状态（v4.0.2 onedir 已定稿，15:19 打包）
源码已包含**全部确认修复**，桌面安装包已更新。**从 onefile 改为 onedir 打包**（彻底规避 `_MEIxxxx` 临时目录被清/删不掉的问题），用户数据迁到 `%LOCALAPPDATA%\Liwangtong`。

### 🔄 popup 打开/激活统一（v4.0.2，修复"配置关闭后主界面打不开"关键）
- 新增 **`launch_and_activate_popup(timeout=5.0)`**（模块级）：已存在且 HWND 有效→直接激活；进程在跑但 HWND 未就绪→轮询 5s 等 HWND；进程不存在→Popen 启动 + 轮询 5s 等 HWND 激活。任一步失败都有日志。
- **`teardown()`（cmd_init）**：删除旧的 `is_popup_running()/else launch + time.sleep(1.2)`，改为**统一调用 `launch_and_activate_popup(timeout=5.0)`**（配置保存后可靠恢复主界面，不再"只等1.2s就退出"）。
- **`_tray_open_popup()`**：简化为直接调用 `launch_and_activate_popup(5.0)`，托盘左键/"打开状态"/配置保存后恢复**三处走同一套逻辑**。

### 🏗️ onedir + 数据目录改造（v4.0.1）
- **打包**：`.spec` 改 onedir（EXE 的 `exclude_binaries=True` + `COLLECT`），产物 `dist\campus_login\`（exe + `_internal`）。安装器装整个目录（`stage\梨网通\*` recurse）。
- **用户数据目录**：新增 `APP_DATA_DIR = %LOCALAPPDATA%\Liwangtong`，`config.json`/`campus_login.log`/`runtime.json`/`popup.hwnd`/`cacert.pem` 都放这里（避免装到 Program Files 无写权限）；`DIR` 仍指向安装目录（`梨网通.exe`/`.ico`/资源）。
- **迁移**：`_migrate_old_data()` 把旧安装目录的 `config.json`/`runtime.json` 迁到新数据目录。
- **说明**：密码存 Windows 凭据管理器（全局，不随目录移动）。onedir 后不再有 `_MEI` 临时目录 → "Failed to remove temporary directory"、certifi/codec 临时目录问题从根上消失。

### 🎯 首装后打不开主界面 + 配置时有黑色命令窗（v4.0 修复版）
- **① 命令窗**：PyInstaller 打包改 **`console=False`**（windowed），`init`/popup/daemon 都不再弹黑色命令行窗口（原 `[Run] init` 无 CREATE_NO_WINDOW 会带控制台）。
- **② 配置保存后主界面不出现**：`teardown()` 改为——**daemon 未在跑才拉起 daemon；popup 若未在跑就 `launch_windowed(['popup'])`，若在跑则 `activate_popup()` 置顶显示**（不重启已有 daemon，其自动热加载）。修正"daemon 在跑但 popup 已退出/隐藏 → 主界面打不开"。
- **③ 打开配置前 `is_init_running()` 守卫**：popup 和托盘"修改账号密码"都先判断配置窗是否已在，避免重复弹窗。

### 稳定化修复（v4.0，①②③④）
- **① `launch_windowed()` 返回 bool**：启动 init 失败会 `config_editing_write(False)`，不残留"编辑中"脏标记。
- **② popup.hwnd 延后写入**：geometry+canvas+update 之后才写 `write_popup_hwnd()`，托盘拿到的是完全初始化的窗口。
- **③ `get_status()` 不再"百度连通=已认证"**：优先 `chkstatus` result，只有 chkstatus 不可用才用 internet_ok 辅助（避免 VPN/热点误判）。
- **④ `config.json` 原子写**：tmp+os.replace，避免写一半崩溃损坏；`_cleanup_orphan_tmp` 一并清理 config.json.*.tmp。

### 轻量化优化（v62/v63，第一批）
- popup 隐藏降频到 3s 只查 app_quit、不重绘；缓存自启；daemon 热加载账号/密码/运营商并重建 DrcomClient；配置中托盘显示"修改设置"。

### ⚡ 轻量化优化（v62，按用户"第一批"优先级）
- **③ popup 隐藏时降频/不重绘**：`tick()` 用 `root.winfo_viewable()` 判断——窗口可见才每 200ms 重绘（PIL render）；被 × 隐藏后**降为 1.5s 一次且不做 PIL 重绘**，后台 CPU 大幅下降（只保留低频 app_quit 检查）。
- **⑤ 注销状态优先于自动登录**：daemon 状态机优先级固定为 `app_quit → daemon_enabled → manual_logout → 网络检查 → 登录`。`manual_logout` 提前到网络检测之前，即使网络检测返回已连接也**绝不自动重连**，直到手动点「连接」。
- **④ daemon 热加载账号/密码/运营商**：`current_suffix()` 每次都读 config，运营商/账号/密码变更自动生效；daemon 会打 `检测到账号配置变化` 日志。

### 🎯 修改账号密码窗口"只闪一下就没"——真根因（v61 修复）
- **现象**：点「修改账号密码」，窗口一闪而过。
- **根因（评审定位）**：`init` 是 **Tk GUI 程序**，但用 `CREATE_NEW_CONSOLE` 启动 → 人为创建一个控制台 → 又 `hide_console()` 隐藏 → **控制台一闪**；且 `teardown()` 保存后还**重启 popup+daemon**，与已运行 daemon 抢 `MUTEX_DAEMON` → 新实例瞬间退出，造成"闪烁 + 多余进程"。
- **v61 修复（四处）**：
  1. `launch_windowed()`：`CREATE_NEW_CONSOLE` → **`CREATE_NO_WINDOW`**（不创建控制台）。
  2. popup 的"修改账号密码" `Popen("init")`：同样改 `CREATE_NO_WINDOW`，并 `cwd=DIR`。
  3. `cmd_init()`：删除开头的 `hide_console()`（纯 GUI 子进程，无控制台可隐藏）。
  4. `teardown()`：**去掉"保存后重启 popup+daemon"**（避免 mutex 竞争 + 闪烁）。已运行 daemon 会自动热加载新账号密码；首次安装由 `cmd_daemon` 负责拉起 popup。
- **补充**：新增 `MUTEX_INIT` 单例（独立配置窗口只开一个）；daemon 热加载处加"账号/密码变更"日志。

### 🔧 评审补充修复（v60，按建议逐条落地）
- **① 托盘左键 popup 竞态**：`_tray_open_popup` 对"已启动但 HWND 未写入"做最多 3s 轮询激活（`activate_popup`），不再直接 `return` 装死。
- **② `activate_popup` 假激活**：改为 `ShowWindow(SW_SHOW)+SW_RESTORE+SetForegroundWindow+BringWindowToTop`，返回**真实 `IsWindowVisible`**，避免"窗口没显示却报成功"。
- **③ popup HWND**：去掉 `GetParent`，直接 `root.winfo_id()`（Tk 顶层句柄）。
- **④ HWND 写入时机**：popup `update_idletasks()+update()` 后再写 HWND。
- **⑤ popup 启动失败检测**：`Popen` 后最多 5s 轮询 `p.poll()` + `activate_popup`，启动后立即崩会被日志捕获。
- **⑧ 重启清除手动注销**：daemon 启动时 `manual_logout=False`，手动注销只在本会话生效，避免"重启后永不自动登录"。
- ~~⑥（已有 manual_logout 闭环）~~、~~⑦（daemon 每轮热加载 user/pw）~~ 已确认闭环。

### 🐛 修改账号密码窗口打不开（v59 修复）
- 现象：点「修改账号密码」弹出配置窗口，但窗口不显示（进程在跑、窗口 `IsWindowVisible=0` / 尺寸 0）。
- 根因：`cmd_init` 的 overrideredirect 窗口设了 `root.geometry(...)` 后**没有 `update_idletasks()`/`update()`**，窗口没真正应用几何尺寸并显示（popup 因在 HWND 注册处调了 `update_idletasks` 所以正常）。
- **v59 修复**：`cmd_init` 在 `c.pack()` 后加 `root.update_idletasks()` + `root.update()`，并加 `[INIT]` 日志。**实测窗口 `vis=1`、尺寸 420×460 正常显示**。

### 🔧 托盘生命周期重构（v58，按用户/评审建议的正式架构）
- **旧**：`tray_start()` 在 daemon 主线程 `icon.run_detached()`（pystray 自己线程） + daemon 主线程 `while not _shutdown` 循环 + runtime.json `popup_activate` IPC 激活窗口。复杂、易踩线程坑。
- **新**（最终架构）：
  - daemon **主线程**直接 `icon.run(setup=_tray_setup)` —— pystray 占用主线程跑 Windows 消息循环（左键/右键菜单都由它处理，官方推荐用法）。
  - `setup` 回调里启动 `_daemon_worker` 独立线程，专门跑网络检测/自动登录循环。
  - 删除 `tray_start()`、`popup_activate`、`run_detached`、runtime.json 激活 IPC（`_tray_open_popup` 直接用 Win32 `activate_popup()` 激活/拉起）。
  - `activate_popup()` 改为返回真实激活结果；`_tray_exit` 用 `icon.stop()`。
  - 保留 `×→withdraw()`（隐藏主界面，托盘/守护不退出）。
- **实测**：源码 daemon 启动日志 `守护启动 → pystray 图标已创建 → setup 开始 → 守护工作线程启动 → daemon 工作线程已启动`，进程常驻。✓

### 🎯 连不上（certifi SSL）——同一 onefile 临时目录根因（v57 修复）
- 用户连接报错：`...\_MEI549682\certifi\cacert.pem`（找不到）。
- 根因与 v56 相同：**PyInstaller onefile 临时目录在运行中被清理**，导致 `requests` 做 HTTPS 联网检测（internet_ok → https://www.baidu.com）时找不到 CA 证书 → SSL 校验失败 → 判定"未连接/登录失败"。
- **v57 修复**：启动时把 `certifi.where()` 的 `cacert.pem` 复制到**稳定的安装目录** `DIR\cacert.pem`，并设 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` 指向它（仅当临时目录不可用时兜底用 `certifi.where()`）。这样即使临时目录被清理，联网检测也能正常用 CA 证书。
- 说明：密码正确（`***`），连不上不是密码问题，而是 HTTPS 联网检测因 CA 证书路径失效而误判失败。

### 🎯 crash 遥测立功：定到"注销转圈/崩溃"根因（v56 修复）
- v55 的 crash 遥测抓到了真实报错：
  `[CRASH][thread] FileNotFoundError: ...\_MEI864962\base_library.zip`，位于 `net_worker → resolve_password → cred_read`。
- **根因**：PyInstaller onefile 从临时目录 zipimport；该 `_MEIxxxxxx` 临时目录在运行中被清理后，后台线程再懒加载 `utf-16-le` 编解码器（`blob.decode("utf-16-le")`）就会 FileNotFoundError。而 `net_worker` 里 `resolve_password` 在 `try` 之外 → 线程崩 → 没写 `last_net` → 注销界面永久转圈。
- **v56 修复**：
  1. 启动即预加载常用编码（`codecs.lookup` utf-8/utf-16/utf-16-le/utf-16-be/latin-1/ascii/gbk），避免后台线程依懒临时目录 zipimport。
  2. `net_worker` 全面加固：`resolve_password`/`cli`/`manual_logout_write`/`daemon_write_state` 全包进 try/except，`last_net` 一定写入 → busy 必清除，任何取密码/网络/写状态异常只会弹 toast，不再转圈卡死。

### ⚠️ crash 遥测（v55 新增）
- 加 `_setup_crash_logging()`：`sys.excepthook` / `threading.excepthook` / Tk `report_callback_exception` 全部接 `log()`，任何未捕获异常会以 `[CRASH]` / `[CRASH][thread]` / `[CRASH][tk]` 写入 `campus_login.log`。

### 🎯 真正的根因（v54 定论，全网走了一天终于定位）
**`is_popup_running()` / `is_daemon_running()` 永远返回 False —— 因为它们用了不存在的 `ctypes.DWORD`！**
- `OpenMutexW.argtypes = [ctypes.DWORD, ...]` 这行会抛 `AttributeError: module 'ctypes' has no attribute 'DWORD'`，被 `except: return False` 吞掉 → **每次调用恒返回 False**。
- 结果：托盘左键永远认为"popup 未运行" → 每次 `Popen` 一个新的 onefile 实例 → 实例互相争抢（有的解压慢/拿不到单例就退）→ 表现为"唤不醒/没反应/抖动"。这是 v49→v53 一直"修不好"的真正原因。
- 该 bug 源自最早的 `is_daemon_running()`（一直在用 `ctypes.DWORD`，从未真正工作）；我 v49 加的 `is_popup_running()` 原样复制了这个错。
- **修复**：全文 `ctypes.DWORD` → `wintypes.DWORD`（`ctypes` 无 DWORD，`wintypes.DWORD` 才存在）。这一步同时修好：凭据结构体/argtypes（此前被我误改坏，已恢复）、`is_popup_running`、`is_daemon_running`。
- **实测**：源码 `is_popup_running()` 对运行中的 popup 返回 **True** ✓。

### 已修复的关键 bug（全部实测）
1. `bot_y→BOT_Y` —— Toast 触发不再 NameError 崩溃
2. `begin_busy` 加 `anim_on[0]=True` + `root.after(150, anim_tick)` —— **注销/连接不再永久转圈卡死**
3. **quit 标记彻底分离（v46 核心，功能语义变更）**：
   - 旧 `quit` 字段**删除**，改用 `app_quit`（只由「退出按钮/托盘退出」置位，表示用户真正退出程序）
   - daemon 的 `finally` **不再写 quit 标记** —— 修掉「daemon 普通退出 → 污染共享 runtime → 弹窗被误判为退出」的根因
   - popup `tick()` **只监听 `app_quit_requested()`**，daemon 退出/崩溃不再误杀弹窗；注销/连接不退出程序
4. **新增 `manual_logout`** —— 用户主动注销后置 `True`，daemon **绝不自动重连**；点「连接」成功后置 `False` 恢复。修掉「注销后被 daemon 立即重连、看起来没效果」
5. **去除窗口边缘洋红毛边（v47→v48，经历了回退重写）**：
   - 现象：卡片边缘一圈洋红毛边。根因：2x 超采样 `SS=max(2,round(2*dpi))` 再 LANCZOS 缩小到显示尺寸时，洋红遮罩与卡片边混出「不上不下」的中间色，非纯 `#FF00FE`，透明抠不掉 → 露紫边
   - 第一次尝试把 `SS` 改 `dpi`(浮点)「不再缩小」——**翻车**：`*S` 坐标全变浮点，PIL `Draw.arc()` 只收整数（`TypeError: integer argument expected, got float`），配置窗眼睛图标/主窗 loading 圈直接崩溃 → 安装无配置窗
   - **最终正确方案**：`SS` 仍保持整数超采样（坐标恒整数，PIL 安全）；缩小后**再施加"卡片外=纯洋红"掩码**：用与卡片同几何的 rounded_rectangle 建 `L` 掩码，LANCZOS 缩小→`>128`二值化，再把卡片外像素强制置 `(255,0,254,255)`。这样包围卡片的毛边被纯洋红盖住→透明抠掉，**保留圆角透明、彻底无毛边**。两处窗口（popup render / init draw）都已加
6. **托盘左键无响应（v49 尝试 → v50 真修）**：
   - 根因：安装完成时 teardown 拉起一个 popup 占住 `MUTEX_POPUP` 单例；之后再左键托盘只是又起一个进程→单例拿不到→静默退出，看似"没反应"。
   - **v49 做了一半踩坑**：新增 `is_popup_running()` 用 `OpenMutexW` 探测，但访问掩码写错成 `0x1F001`（应为 `MUTEX_ALL_ACCESS=0x1F0001`），实测 `OpenMutexW(0x1F001)` 返回 None / `ERROR_ACCESS_DENIED(5)` → **恒等于检测不到运行中的 popup**，于是 v49 的"置顶已有窗口"逻辑从未生效，左键仍无效。
   - **v50 真修**：`is_popup_running()`/`is_daemon_running()` 的掩码改为 `0x1F0001`。现在左键：主界面在跑→置顶显示（写 `runtime["popup_activate"]`，popup `tick()` `lift()+topmost`）；没在跑→正常拉起。**实测验证过 OpenMutexW 掩码行为**。
   - **v51 加遥测 + 加固**：整条托盘链路原本用裸 `except: pass` 把所有启动失败吞成"没反应"。现在 `_tray_open_popup`/`_tray_run`/`tray_start` 全部带日志（`[TRAY]`），启动前校验 `os.path.isfile(exe)`、`cwd=DIR`、打印 PID；`acquire_singleton` 保存 mutex 句柄并记录 `[MUTEX]` 日志（遇 ERROR_ALREADY_EXISTS 主动 CloseHandle）。**下次左键后看 `campus_login.log` 里 `[TRAY]` 行就知道卡在哪一步**（pystray 是否触发 / Popen 是否成功 / popup 是否启动后崩）。
   - **v52 换 Win32 HWND 激活 + 菜单可见**：把"激活已有窗口"从 runtime.json IPC 换成**直接 Win32**：
     - `POPUP_HWND_FILE=popup.hwnd`：popup 创建后 `write_popup_hwnd(GetParent(winfo_id()))` 记录真实顶层句柄；退出时 `remove_popup_hwnd()`（× / Esc / 退出 / app_quit / mainloop 结束）。
     - `activate_popup()`：`IsWindow` 校验 + `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` + 短暂置顶 `SetWindowPos(TOPMOST→NOTOPMOST)`。**实测 `activate_popup()==True` 命中活窗口**。
     - `_tray_open_popup`：先 `activate_popup()`（命中即返回，不再走 runtime.json），失败再 `is_popup_running()` 兜底，最后 `Popen`。
     - **"打开状态"改为 `visible=True`**（右键菜单也能点）——既是可靠的打开入口，也是诊断：右键→打开状态能开、左键不行，就锁定 pystray 左键默认动作层；此时再考虑 WM_LBUTTONUP 直连。
   - **v53 关键：× 改为"隐藏"而非"退出"进程（修"叉叉后左键唤不醒"）**：
     - 实机日志显示：左键每次 `Popen` 一个 onefile 新 popup，但 onefile **每次都要重新解压（~3s）**，且快速连点会同时挤出多个实例、拉锯抖动；`is_popup_running` 在这段解压窗口内恒为"未运行"，于是下一键又 Popen → 用户感觉"唤不醒"。
     - 修复：右上角 × / Esc 由 `os._exit(0)` 改为 **`root.withdraw()`（隐藏，进程/单例/句柄保留）**；托盘左键发现 `is_popup_running()` 为真 → 写 `popup_activate`，隐藏的 popup 自己 `deiconify()+lift()+topmost` **瞬时显示**，不再每次解压/抖动。真正退出仍靠「退出按钮/托盘退出/app_quit(卸载)`os._exit`」。
     - 效果：首次拉起后，"× → 左键托盘"即刻重新出现，无需再等 onefile 解压。
   - **v54 补充**：
     - `tray_start()` 不再 `threading.Thread(...).start()`，改为在 daemon **主线程**直接 `_tray_run()`；`_tray_run()` 末尾用 **`icon.run_detached()`**（非阻塞，pystray 自行建事件线程），不再在子线程里 `icon.run()` —— 与 pystray Windows 后端线程模型一致（`run()` 阻塞主事件循环）。注意：`run_detached()` 立即返回，故删除原先会把 `_tray_icon` 置 None 的 `finally`。
     - `acquire_singleton` 异常/`CreateMutexW` 失败改为 **`return False`（fail-closed）**，避免"没拿到锁也当成功"导致多 daemon/popup 互踩。原 `return True` 是 fail-open。
     - 实机日志佐证：`[TRAY] 收到打开主界面事件` **每次左键都会打**（pystray 左键回调一直在跑），所以"左键没反应"不是 pystray 触发问题，而是 is_popup_running 恒 False 导致的"永远当未运行→反复 Popen"。修复 is_popup_running（ctypes.DWORD）后链路即通。
7. 双击绑定点不了 —— 只留 `c.bind("<Button-1>")`，删 `ButtonPress-1`（Tk 里与 Button-1 同一事件会覆盖 click）
8. 眼睛点击、保存后开主界面 —— 同上绑定修复
9. 颜色常量去重（COLOR_TEXT_PRIMARY 5份→1份）
10. `nid.uTimeout` 删除 —— 系统通知失效修复（ctypes 结构体无此字段，赋值必 AttributeError）
11. runtime tmp PID 隔离 —— 多进程不互覆盖
12. `set_autostart` 返回 bool，cmd_autostart 显示 [OK]/[FAIL]
13. 通知失败记 `log()`（不再静默吞异常）
14. `_cleanup_orphan_tmp()` 启动时清理孤儿 tmp（main 里调用）

### 安装器（Inno）
- **安装位置可自选（v49）**：`campus_installer.iss` 的 `DisableDirPage=auto` 改 `no` —— 强制总是显示「选择目标位置」页，用户可改路径/浏览
- **卸载遗留 exe（v50）**：安装后台的 daemon/popup 进程会占用 `{app}\梨网通.exe`，卸载删除目录时被锁→遗留一个 exe。修复：
  - 新增 `quit` 命令（`cmd_quit`：置 `app_quit` 让 daemon/popup 退出，再 sleep 2s 释放文件锁）
  - `.iss` 加 `[UninstallRun]`：卸载前先 `梨网通.exe quit`（runhidden + waituntilterminated）
  - `.iss` `[Setup]` 加 `CloseApplicationsFilter=梨网通.exe` 兜底强制关闭

### 语义确认（用户明确）
- **连接/注销**：网络操作，**不退出程序**；注销后状态变"未连接"可见；注销后 daemon 不自动重连（manual_logout）
- **右上角 ×**：关闭 popup 界面，**不退出托盘**（popup 是独立进程，os._exit 只退 popup，daemon/托盘存活）
- **退出（底部粉色按钮）**：确认后退出**程序+托盘**（do_quit_app → request_app_quit → daemon 循环 break → 托盘 stop）
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

## suggested skills
- `diagnose` —— 若遇到运行期 bug/回归，纪律化定位
- `mimo-vision` —— 若需分析截图确认 UI 渲染（当前模型不直接支持读图）
- `review` —— 若需对照规范审查改动
