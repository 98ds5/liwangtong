# 构建与发布

## 环境

- Windows 10 / 11 x64
- Python 3.8 x64（我一直在 3.8.6 上打包，产物最小；换 3.11+ 也能打，但体积会大一截）
- 依赖：`python -m pip install -r requirements.txt`
- 打包工具：`pip install pyinstaller`（我用 6.21.0）、[Inno Setup 6](https://jrsoftware.org/isinfo.php)

## 1. 直接跑源码

```powershell
python src\campus_login.py init      # 第一次：填学号密码
python src\campus_login.py daemon    # 托盘 + 后台守护
python src\campus_login.py status    # 看当前状态
```

和打包版行为一致，只是没有单实例锁以外的差别（锁是一样的）。数据同样落在 `%LOCALAPPDATA%\Liwangtong\`。

## 2. 打 exe

```powershell
pyinstaller campus_login.spec
```

产物在 `dist\梨网通\`：`梨网通.exe` + `_internal\`。先自测两条，能起来、能退干净，再往下走：

```powershell
dist\梨网通\梨网通.exe            # 应该弹出主界面，托盘出现图标
dist\梨网通\梨网通.exe quit      # 应该干净退出，任务管理器里没有残留
```

`status` / `diagnostic` 这类往控制台打印的命令，在打包版（`console=False`）+ 中文终端下会因 GBK 编不出程序名里的 🍐 直接 `UnicodeEncodeError`，测它们请从源码跑（先 `set PYTHONIOENCODING=utf-8`）。

## 3. 组 stage 目录

Inno 脚本从仓库根的 `stage\` 取文件（这个目录已 gitignore，每次构建重新拼）：

```powershell
mkdir stage -Force
robocopy dist\梨网通 stage\梨网通 /MIR
copy assets\campus_notify.ico stage\
copy assets\campus_notify_err.ico stage\
```

两个 `.ico` 是系统通知用的图标，安装后放在 exe 同目录；缺了不影响功能，通知会用系统默认图标。

## 4. 打安装包

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installers\campus_installer.iss
```

产物：`output\LiWangTong-Setup-<版本>.exe`。

## 版本号在哪几处

| 位置 | 说明 |
|------|------|
| `installers/campus_installer.iss` 的 `MyAppVersion` | 唯一真正生效的，安装包文件名和「应用和功能」里显示的都是它 |
| git tag `vX.Y.Z` | 打在发版那次提交上 |
| `CHANGELOG.md` | 手写 |

exe 的文件属性里没有版本信息（PyInstaller 默认不写 `VS_VERSION_INFO`）。要加就在 spec 里给 `EXE()` 传 `version='...'`，顺便也能带版权字段。

## 5. 发布

```powershell
git tag v4.0.12
git push origin v4.0.12
gh release create v4.0.12 output\LiWangTong-Setup-4.0.12.exe --title "v4.0.12" --notes "..."
```

安装包只放 Release 的 assets，不要提交进仓库（`.gitignore` 里 `*.exe` 已经挡了）。

## 发版前冒烟测试

每次打包后我自己都跑一遍这几条，尤其是第 4、6、8 —— 这三个出问题都是之前真实踩过的：

1. 装：Setup 走完，勾「启动」，配置窗口弹出。
2. 填学号 / 密码 / 运营商，保存 → 主界面出现，托盘图标变粉（已连接）。
3. 控制面板 → 凭据管理器 → Windows 凭据，确认有 `campus_login` 一条；再看 `config.json`，里面没有 `password` 字段。
4. 托盘左键 → 界面出来；点 `×` → 界面没了但托盘还在；再左键 → 应该**立刻**出来（不是重新解压、不是闪一下退掉）。
5. 拔网线等 30 秒 → 图标变灰；插回 → 自动重连成功。
6. 托盘右键「退出」→ 任务管理器里**不能**有 `梨网通.exe` 残留。
7. 勾上开机自启 → 重启 → 只有托盘图标、不弹界面、网络是已连接状态。
8. 卸载 → 安装目录整个消失，不留 exe 和 `_internal`（`%LOCALAPPDATA%\Liwangtong` 的用户数据是故意保留的）。

## 打包相关的坑

- **必须 onedir，别退回 onefile。** onefile 解压到 `%TEMP%\_MEIxxxxx`，Windows 清理临时目录或者杀软实时扫描会把正在用的目录删掉。表现非常诡异：后台线程懒加载 `utf-16-le` 编解码器、`requests` 找 certifi 的 `cacert.pem`，全都 `FileNotFoundError`，注销界面永久转圈。4.0.1 改 onedir 之后这类问题一次没再出现。
- **`collect_all('pystray')` / `collect_all('PIL')`**：pystray 的后端模块是按平台动态 import 的，不 collect 出来的 exe 换台机器就 `No module named 'pystray._win32'`。
- **`console=False`**：True 的话每次启动黑窗一闪。代价是窗口程序没有 stdout 可打，异常靠 `_setup_crash_logging()` 兜到日志文件里。
- **一定要带 `campus_login.manifest`**：声明 PerMonitorV2 DPI。不带的话在 125% / 150% 缩放屏上界面糊、点击坐标全偏（代码里按 `get_dpi_scale()` 做逻辑坐标换算，依赖系统真的回报 Per-Monitor DPI）。
- **中文 exe 名**：`CloseApplicationsFilter` 也得写 `梨网通.exe`，写错英文名卸载前就不会提示关闭占用程序。
- **杀软误报**：PyInstaller 产物 + UPX 压缩误报率明显高，所以 spec 里 `upx=False`。发布前自己丢 VirusTotal 看一眼，别等用户来问。
- **没有代码签名**，SmartScreen 会拦「未知发布者」，用户要点「更多信息 → 仍要运行」。README 里说了这件事，别指望用户自己知道。
