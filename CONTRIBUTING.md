# 贡献

先说清楚一件事：这个项目是我的校园网客户端，我自己每天在用，所以我改东西的优先级是「别把现在的登录搞坏」，而不是「支持更多学校」。这不是客套，很多 PR 的取向会在这里撞上。

## 从源码跑起来

```
python -m pip install -r requirements.txt
python src\campus_login.py init      # 填学号密码
python src\campus_login.py daemon    # 托盘 + 守护
```

改完想验证认证链路，直接 `python src\campus_login.py status`（源码运行不受打包版那个 emoji 编码问题影响）。要打包见 [docs/BUILD.md](docs/BUILD.md)，里面有一份发版前我会跑的冒烟清单，改多进程相关的代码请务必走完那 8 条。

## 代码上的约定

- **全都放在 `src/campus_login.py` 里**，不要拆模块。这个程序要能拷到一个绿色目录就跑起来，拆成包之后 PyInstaller 的数据文件和路径推导会麻烦很多。觉得单文件太长可以加 `# 段名` 注释分组，别开新文件。
- **不引入新的第三方依赖**。现在的依赖只有 `requests` / `certifi` / `pillow` / `pystray`，Windows 相关的东西一律走 `ctypes` + `winreg`。想加依赖先开 issue 说，打包体积和杀软误报都要考虑。
- **注释写中文**，因为读它的人（包括半年后的我）中文更快。日志前缀沿用 `[TRAY]` / `[DAEMON]` / `[POPUP]` / `[INIT]` / `[STATUS]` / `[CRASH]` 这套，别新开一套。
- **异常不要裸 `except: pass`**。这条是踩出来的：`is_popup_running()` 里一个 `AttributeError` 被吞掉，导致托盘左键每次都能拉起新进程，查了一整天。至少把异常打到日志里。
- **跨进程不要直接操作对方的窗口**。要弹窗就往 `popup.show` 写一个请求，让 popup 自己 `deiconify()`，原因写在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 里。
- 界面上的文案、颜色、尺寸改动我会比较谨慎，因为这个界面是手绘的（tkinter + PIL 画圆角），改一处经常要跟着改 DPI 换算。

## 关于「适配我的学校」

把 `BASE_URL` 改成你学校的网关、把 `OPERATORS` 改成你学校的后缀，这属于 fork 后自己改的部分，我不会往主线里加「多校园配置」这种抽象 —— 不同学校的 Dr.COM 版本、接口参数、返回文本判断都不一样，做成可配置项最后会变成一堆永远测不到的分支。如果你的学校改动确实有意义，欢迎开 issue 说明你改了哪几行、为什么，我会考虑在 README 里放一个链接指过去。

## 提 PR 之前

1. 先开 issue 说你要干什么，尤其是改认证逻辑、进程模型、凭据存储这三块的。
2. 一个 PR 只做一件事。
3. 描述里写清楚：改了什么、为什么、你测过哪些（把 BUILD.md 那 8 条对照着说）。
4. 别顺手格式化整个文件（行尾、缩进、引号风格），那会让 diff 完全没法看。

## 报 bug

模板里的几项填全，特别是 `diagnostic` 的输出和日志片段。「不能用」「登不上」这种信息不够的 issue 我大概率只能回一句「看下日志」。安全类的问题走 [SECURITY.md](SECURITY.md)。
