# 梨网通 / LiWangTong

Windows 上的 Dr.COM 校园网自动登录客户端。开机自己认证、断线自己重连，平时缩在系统托盘里，不用再开浏览器输一遍账号密码。

我自己的学校（安徽理工大学，AUST）用的就是这套 Dr.COM 认证，联通 / 电信 / 移动三条线都实测过。写它的动机很简单：每天开浏览器点一遍登录太烦。认证网关地址是写死的，换学校要改代码，先看下面的「适用范围」。

最新安装包在 [Releases](https://github.com/98ds5/liwangtong/releases)，不想用安装包的可以从源码自己打（见 [docs/BUILD.md](docs/BUILD.md)）。

---

## 它做什么

- **自动登录**：走 Dr.COM 的 HTTP 认证接口，POST `/a79.htm` 为主，失败再退到 `/drcom/login` 的 GET / POST，三个通道轮着试。
- **断线重连**：后台守护线程定期查状态，掉了就重新认证（重连间隔默认 30 秒，刚登录成功后的 15 秒宽限期不判定为掉线）。
- **系统托盘**：图标颜色就是当前状态，右键菜单可以开关守护、开关开机自启、改账号密码、退出。
- **凭据不落明文**：账号密码写进 Windows 凭据管理器（通用凭据，目标名 `campus_login`），`config.json` 里只有学号和运营商后缀。
- **界面和后台分开**：主界面是一个独立进程，关掉窗口不影响后台连接，界面崩了也不会把守护带下去。
- **开机自启**：写 `HKCU\...\CurrentVersion\Run`，可以在托盘菜单里直接切。

## 托盘颜色对照

| 图标 | 含义 |
|------|------|
| 粉色 | 已连接 |
| 灰色 | 未连接 |
| 淡紫粉 | 连接中 / 正在处理 |

鼠标停在图标上会显示「🍐 网通｜已连接｜守护：开」这样的提示。守护开关只决定**掉线后要不要持续重连**；开机那一次认证是无条件执行的，不然关着守护就永远连不上。

## 安装

1. 下载 `LiWangTong-Setup-x.y.z.exe`，双击安装（不需要管理员权限，装在 `%LOCALAPPDATA%\PearTech\Liwangtong`）。
2. 安装完成勾选启动后会自动弹出配置窗口，填学号、密码，选运营商，保存。
3. 保存成功后主界面弹出来，托盘出现图标，之后就不用管了。

开机自启默认不开，装完在托盘右键里勾一下。

没做代码签名，第一次运行 SmartScreen 会提示「未知发布者」，点「更多信息 → 仍要运行」；个别杀软对 PyInstaller 产物误报，把安装目录加白名单就行。

## 命令行

打包出来的 exe 接受一个子命令，安装器和开机自启就是靠它：

| 命令 | 作用 |
|------|------|
| `梨网通.exe`（无参数） | 打开程序：确保守护在跑，并弹出主界面 |
| `梨网通.exe daemon` | 只起托盘和守护，不弹界面（开机自启用的就是这个） |
| `梨网通.exe popup` / `init` | 只开主界面 / 只开账号密码配置窗口 |
| `梨网通.exe login` | 登录一次 |
| `梨网通.exe quit` | 退出守护和界面（卸载前会自动调用它） |
| `梨网通.exe autostart on\|off` | 开机自启开关，不带参数只打印当前状态 |
| `梨网通.exe status` / `diagnostic` / `config` | 打印状态、自检报告、命令行改配置 |

最后那组是「往控制台打印」的命令，而打包版是无控制台程序：在中文 Windows 的终端里直接跑，会因为 GBK 编不出程序名里的 🍐 而报 `UnicodeEncodeError`（`config` 还要交互输入）。所以看状态请开主界面或者看日志；要跑完整诊断就从源码跑，先设编码：

```
set PYTHONIOENCODING=utf-8
python src\campus_login.py diagnostic
```

诊断报告会自动复制到剪贴板，提 Issue 的时候直接粘贴就行。

## 数据放在哪

```
%LOCALAPPDATA%\Liwangtong\
├── config.json          # 学号、运营商后缀、超时等参数
├── runtime.json         # 三个进程之间共享的状态
├── campus_login.log     # 运行日志，超过 512KB 只保留最后 200 行
├── cacert.pem           # 从 certifi 复制出来的 CA 包（HTTPS 探测用）
├── popup.hwnd           # 主界面窗口句柄
└── popup.show           # 「请显示界面」的请求标记，弹窗自己清
```

密码不在这个目录里，在凭据管理器（控制面板 → 凭据管理器 → Windows 凭据）能看到 `campus_login` 那一条。

卸载之后这个目录会留着（配置和日志还在，方便你重装），要彻底清掉就手工删了它。

## 适用范围

- 只支持 Windows，代码里直接用了 `winreg`、`ctypes.windll`、Windows 凭据管理器和 pystray 的 Windows 后端，别的平台跑不起来。
- 面向 Dr.COM 认证环境。`src/campus_login.py` 里的 `BASE_URL` 是写死的校内网关地址，不同学校地址、接口路径、参数都不一样，换学校至少要改这一行。
- 学校可能换认证系统、改接口参数、限制第三方客户端登录，这些都不是我这边能控制的。
- 用之前确认一下学校允许这样登录。因为自动重连而导致的账号异常、封顶、被断网，本程序概不负责。
- 本项目和 Dr.COM（城市热点）没有任何关系，不是官方客户端，也没有用它们的任何代码或授权。

## 项目结构

```
liwangtong/
├── src/
│   ├── campus_login.py         # 全部逻辑都在这一个文件里（约 2400 行）
│   ├── auto_login.vbs          # 早期直接跑 exe 时用的静默启动脚本，装了之后用不上
│   └── config.template.json    # 配置样例
├── assets/                     # 程序图标和通知图标
├── docs/                       # 架构、构建、排错、开发记录
├── installers/                 # Inno Setup 脚本
├── campus_login.spec           # PyInstaller 打包配置
├── campus_login.manifest       # PerMonitorV2 DPI / 通用控件声明
├── requirements.txt            # 四个第三方依赖
└── README.md
```

`config.json`、`runtime.json`、`cacert.pem` 都在 `.gitignore` 里，仓库中不会带上任何个人配置。

## 从源码跑

```bash
python -m pip install -r requirements.txt
python src/campus_login.py init      # 配置账号密码
python src/campus_login.py daemon    # 托盘 + 守护
```

需要 Python 3.8+，依赖只有 `requests` / `certifi` / `pillow` / `pystray`（`tkinter`、`ctypes`、`winreg` 都是标准库），只在 Windows 上能跑。打包成 exe、做安装包见 [docs/BUILD.md](docs/BUILD.md)。

## 更多文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 三进程模型、`runtime.json` 和命名互斥锁怎么配合，认证流程
- [docs/BUILD.md](docs/BUILD.md) —— PyInstaller onedir + Inno Setup 的完整构建步骤
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) —— 连不上、界面打不开、托盘没图标、卸载残留这些怎么查
- [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) —— 开发记录和踩过的坑（多进程那几个竞态真的挺坑）
- [CHANGELOG.md](CHANGELOG.md)

## 反馈

有问题先翻 [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) 和 `%LOCALAPPDATA%\Liwangtong\campus_login.log`，开 Issue 的时候把日志里的相关几行带上，不然很难判断。安全相关的漏洞别开公开 Issue，看 [SECURITY.md](SECURITY.md)。

## 许可

MIT，详见 [LICENSE](LICENSE)。
