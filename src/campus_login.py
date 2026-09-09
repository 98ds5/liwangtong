# -*- coding: utf-8 -*-
"""
🍐 网通 - 校园网自动登录客户端 (Dr.COM)
架构分层：
  常量与配置 -> 凭据(Windows Credential) -> 日志 -> 主题 -> 共享状态(runtime.json)
  -> 系统通知 -> 托盘(pystray) -> DrCOM 协议 -> 命令 -> GUI(popup/init)
健壮性设计：
  - daemon 单实例互斥；popup/init 独立进程崩溃不影响守护
  - 凭据只存 Windows 凭据管理器，config.json 不落明文
  - runtime.json 进程间共享状态，原子写入
  - 登录 POST /a79.htm(学生) + GET/POST /drcom/login 多通道兜底
  - 默认亮色主题，运行时可切换并持久化
"""
from __future__ import annotations
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from getpass import getpass
from typing import Any, Optional
import requests
# PyInstaller onefile 会用临时目录 zipimport；若该目录在运行中被清理，后台线程再懒加载
# 编解码器（如 utf-16-le）就会 FileNotFoundError 崩溃。这里启动即预加载常用编码，避免依赖临时目录。
import codecs
for _codec in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "latin-1", "ascii", "gbk"):
    try:
        codecs.lookup(_codec)
    except LookupError:
        pass
# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════
BASE_URL = "http://10.255.0.19"
CM_TARGET = "campus_login"
LOG_ENCODING = "utf-8"
APP_NAME = "liwangtong"
APP_DISPLAY = "\U0001F350 \u7F51\u901A"   # 🍐 网通
APP_EXE = "梨网通.exe"
MUTEX_DAEMON = "Local\\LiwangtongDaemon"
MUTEX_POPUP = "Local\\CampusLoginPopup"
MUTEX_INIT = "Local\\CampusLoginInit"
# 运营商（suffix 对用户隐藏，内部映射）
OPERATORS = [
    {"id": "cmcc", "name": "移动", "suffix": "@cmcc"},
    {"id": "lt", "name": "联通", "suffix": "@unicom"},
    {"id": "aust", "name": "电信", "suffix": "@aust"},
]
DEFAULT_SUFFIX = "@unicom"
# ========== 豆沙低饱和配色（状态 / 连接 / 风险）==========
C_GREEN = (98, 140, 110)   # 状态、成功 豆沙绿 #628C6E
C_BLUE = (74, 115, 153)     # 连接主操作 豆沙蓝 #4A7399
C_PINK = (224, 108, 117)    # 注销/退出 豆沙粉 #E06C75
C_PEAR = (156, 196, 72)     # 🍐 梨子 黄绿

if getattr(sys, "frozen", False):
    DIR = os.path.dirname(sys.executable)
else:
    DIR = os.path.dirname(os.path.abspath(__file__))
# 用户数据（配置/日志/runtime/凭据辅助文件）放到 %LOCALAPPDATA%\Liwangtong，避免安装到 Program Files 时无写权限
APP_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", DIR), "Liwangtong")
try:
    os.makedirs(APP_DATA_DIR, exist_ok=True)
except Exception:
    pass
CONFIG_PATH = os.path.join(APP_DATA_DIR, "config.json")
LOG_PATH = os.path.join(APP_DATA_DIR, "campus_login.log")
RUNTIME_PATH = os.path.join(APP_DATA_DIR, "runtime.json")
# onefile 临时目录如在运行中被清理，certifi 的 cacert.pem 也会找不到，导致 HTTPS/联网检测失败。
# 启动时把 CA 证书复制到用户数据目录，并强制 requests 使用它。
try:
    import certifi, shutil
    _ca = os.path.join(APP_DATA_DIR, "cacert.pem")
    try:
        if not os.path.exists(_ca) or os.path.getsize(_ca) == 0:
            shutil.copyfile(certifi.where(), _ca)
    except Exception:
        pass
    if os.path.exists(_ca):
        os.environ["REQUESTS_CA_BUNDLE"] = _ca
        os.environ["SSL_CERT_FILE"] = _ca
    else:
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
except Exception:
    pass
# ═══════════════════════════════════════════════════════════════
# 高 DPI 感知（窗口创建前调用）
# ═══════════════════════════════════════════════════════════════
def enable_dpi_awareness() -> None:
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
def get_dpi_scale() -> float:
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi / 96.0 if dpi > 0 else 1.0
    except Exception:
        return 1.0
try:
    enable_dpi_awareness()
except Exception:
    pass
# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
DEFAULTS = {
    "timeout": 8,
    "retry_max": 3,
    "retry_backoff": 2,
    "daemon_interval": 30,
    "daemon_check_connected": 10,
    "grace_period": 15,
    "log_max_bytes": 512_000,
    "log_keep_lines": 200,
    "notify": True,
}
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
def save_config(cfg: dict) -> None:
    # 原子写：先写 tmp 再 os.replace，避免写入途中崩溃/杀进程留下损坏的 JSON
    tmp = "%s.%d.tmp" % (CONFIG_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
def get_cfg() -> dict:
    cfg = load_config()
    merged = dict(DEFAULTS)
    for k, v in cfg.items():
        if k in DEFAULTS or k in ("username", "suffix"):
            merged[k] = v
    return merged
def current_suffix() -> str:
    return get_cfg().get("suffix", DEFAULT_SUFFIX) or ""
def op_display_name() -> str:
    """当前运营商显示名（隐藏后缀）。"""
    sfx = current_suffix()
    for op in OPERATORS:
        if op["suffix"] == sfx:
            return op["name"]
    return "联通" if sfx else "（未知）"
def validate_account(username: str, password: str) -> str:
    """返回 '' 通过，否则错误信息。"""
    if not username:
        return "学号不能为空"
    if not username.isdigit():
        return "学号应为纯数字"
    if len(username) < 6:
        return f"学号长度不足（当前{len(username)}位，至少6位）"
    if not password:
        return "密码不能为空"
    if len(password) < 4:
        return "密码长度不足"
    return ""
# ═══════════════════════════════════════════════════════════════
# Windows 凭据管理器（ctypes）
# ═══════════════════════════════════════════════════════════════
class CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]
class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTE)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_advapi = ctypes.WinDLL("advapi32")
_CredWriteW = _advapi.CredWriteW
_CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
_CredWriteW.restype = wintypes.BOOL
_CredReadW = _advapi.CredReadW
_CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
_CredReadW.restype = wintypes.BOOL
_CredDeleteW = _advapi.CredDeleteW
_CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
_CredDeleteW.restype = wintypes.BOOL
_CredFree = _advapi.CredFree
_CredFree.argtypes = [ctypes.c_void_p]
_CredFree.restype = None
def cred_save(username: str, password: str) -> bool:
    blob = password.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob)
    cred = CREDENTIALW()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = CM_TARGET
    cred.UserName = username
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    return bool(_CredWriteW(ctypes.byref(cred), 0))
def cred_read() -> Optional[str]:
    pcred = ctypes.POINTER(CREDENTIALW)()
    if not _CredReadW(CM_TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        return None
    try:
        cred = pcred.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        _CredFree(pcred)
def cred_delete() -> bool:
    return bool(_CredDeleteW(CM_TARGET, _CRED_TYPE_GENERIC, 0))
def resolve_password(cfg: dict) -> Optional[str]:
    """取密码：凭据管理器优先，其次迁移 config 残留明文。"""
    pw = cred_read()
    if pw is not None:
        return pw
    old = cfg.pop("password", None)
    if old is not None:
        u = cfg.get("username", "")
        if cred_save(u + current_suffix(), old):
            save_config(cfg)
            log("[SECURITY] 明文密码已迁入凭据管理器并清除")
            return old
    return None
# ═══════════════════════════════════════════════════════════════
# 进程间共享状态 runtime.json
# ═══════════════════════════════════════════════════════════════
def read_runtime() -> dict:
    try:
        with open(RUNTIME_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
_runtime_lock = threading.Lock()   # 同进程内(主线程+net_worker线程)串行写 runtime.json，避免 temp 竞争
def write_runtime(data: dict) -> None:
    with _runtime_lock:
        try:
            tmp = "%s.%d.tmp" % (RUNTIME_PATH, os.getpid())   # 每进程独立tmp, 避免多进程竞争
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, RUNTIME_PATH)
        except OSError:
            pass
def daemon_state_read() -> bool:
    # 默认开启后台守护：开机自启即自动连接，用户手动关闭后 runtime 会记录 False 保持关闭
    return read_runtime().get("daemon_enabled", True)
def daemon_state_write(on: bool) -> None:
    rt = read_runtime()
    rt["daemon_enabled"] = on
    write_runtime(rt)
def theme_read() -> str:
    return read_runtime().get("theme", "light")
def theme_write(theme: str) -> None:
    rt = read_runtime()
    rt["theme"] = theme
    write_runtime(rt)
def daemon_write_state(state: str, ip: str) -> None:
    rt = read_runtime()
    rt["state"] = state
    rt["ip"] = ip
    write_runtime(rt)
def request_app_quit() -> None:
    """用户真正退出程序（退出按钮/托盘退出）才置位。重启/异机登录/daemon 普通退出不置位。"""
    rt = read_runtime()
    rt["app_quit"] = True
    write_runtime(rt)
def app_quit_requested() -> bool:
    return read_runtime().get("app_quit", False)
def manual_logout_read() -> bool:
    """用户是否手动注销过（手动注销后 daemon 不得自动重连）。"""
    return read_runtime().get("manual_logout", False)
def manual_logout_write(on: bool) -> None:
    rt = read_runtime()
    rt["manual_logout"] = on
    write_runtime(rt)
def config_editing_read() -> bool:
    """是否正在修改账号密码（打开配置窗口时 daemon 应暂停自动认证）。"""
    return read_runtime().get("config_editing", False)
def config_editing_write(on: bool) -> None:
    rt = read_runtime()
    rt["config_editing"] = on
    write_runtime(rt)
def is_init_running() -> bool:
    """当前是否有独立的配置窗口（init 单例 mutex）在运行。"""
    try:
        _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel.OpenMutexW.restype = ctypes.c_void_p
        _kernel.OpenMutexW.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.LPCWSTR]
        h = _kernel.OpenMutexW(0x1F0001, False, MUTEX_INIT)
        if h:
            _kernel.CloseHandle(ctypes.c_void_p(h))
            return True
        return False
    except Exception:
        return False
# ═══════════════════════════════════════════════════════════════
# 托盘左键 -> 主界面 HWND 激活（Win32，比 runtime.json IPC 稳）
# ═══════════════════════════════════════════════════════════════
POPUP_HWND_FILE = os.path.join(APP_DATA_DIR, "popup.hwnd")
POPUP_SHOW_FILE = os.path.join(APP_DATA_DIR, "popup.show")
def write_popup_hwnd(hwnd: int) -> None:
    try:
        tmp = "%s.%d.tmp" % (POPUP_HWND_FILE, os.getpid())
        with open(tmp, "w", encoding="ascii") as f:
            f.write(str(hwnd))
        os.replace(tmp, POPUP_HWND_FILE)
    except OSError as e:
        log(f"[POPUP] 写 HWND 失败: {e}")
def read_popup_hwnd() -> int:
    try:
        with open(POPUP_HWND_FILE, "r", encoding="ascii") as f:
            return int(f.read().strip())
    except Exception:
        return 0
def remove_popup_hwnd() -> None:
    try:
        os.remove(POPUP_HWND_FILE)
    except OSError:
        pass
def request_popup_show() -> bool:
    """请求已存在的 popup 进程自己恢复窗口（写 popup.show 标记，由 popup 检测后 deiconify）。
    原子写：先写 tmp 再 os.replace，避免多进程/异常中断留下半写标记。"""
    try:
        tmp = "%s.%d.tmp" % (POPUP_SHOW_FILE, os.getpid())
        with open(tmp, "w", encoding="ascii") as f:
            f.write("1")
        os.replace(tmp, POPUP_SHOW_FILE)
        return True
    except Exception as e:
        log(f"[POPUP] request show failed: {e}")
        return False
def popup_show_requested() -> bool:
    return os.path.exists(POPUP_SHOW_FILE)
def clear_popup_show_request() -> None:
    try:
        if os.path.exists(POPUP_SHOW_FILE):
            os.remove(POPUP_SHOW_FILE)
    except Exception:
        pass
def popup_window_valid() -> bool:
    """popup 的 HWND 是否已存在且有效（IsWindow）。仅作被动检查，不做外部激活。"""
    hwnd = read_popup_hwnd()
    if not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False
def activate_popup() -> bool:
    """[保留 / 兼容] 用 Win32 把已打开的 popup 窗口显示/置顶/激活。窗口已不可用则清掉记录。
    注意：现在「托盘恢复窗口」不会再调用本函数，改由 popup 进程检测 popup.show 后自行
    root.deiconify()。本函数仅留作 HWND 检查或兼容旧逻辑。"""
    user32 = ctypes.windll.user32
    hwnd = read_popup_hwnd()
    if not hwnd:
        return False
    if not user32.IsWindow(hwnd):
        remove_popup_hwnd()
        return False
    try:
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.ShowWindow(hwnd, 5)   # SW_SHOW
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        # 短暂置顶，把窗口按到最前，再释放（解决其他窗口抢焦点/被遮挡）
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP = 0x0001 | 0x0002 | 0x0010   # NOSIZE|NOMOVE|NOACTIVATE
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP)
        visible = bool(user32.IsWindowVisible(hwnd))
        log(f"[TRAY] activate HWND={hwnd} visible={visible}")
        return visible
    except Exception as e:
        log(f"[TRAY] activate_popup 异常: {e}")
        return False
_mutex_handles = {}
def acquire_singleton(name):
    """尝试获取命名互斥。返回 True=成功获取（本实例接管）；False=已有实例。
    保存句柄防止被 GC 提前释放；遇到"已存在"时主动关闭临时句柄。"""
    try:
        _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel.CreateMutexW.restype = ctypes.c_void_p
        _kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                         wintypes.LPCWSTR]
        _kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.set_last_error(0)
        h = _kernel.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            log(f"[MUTEX] CreateMutexW 失败 for {name}: err={err}")
            return False
        if err == 183:  # ERROR_ALREADY_EXISTS：已有实例，关闭临时句柄
            _kernel.CloseHandle(ctypes.c_void_p(h))
            return False
        _mutex_handles[name] = h
        return True
    except Exception as e:
        log(f"[MUTEX] acquire_singleton 异常: {type(e).__name__}: {e}")
        return False
def is_daemon_running() -> bool:
    try:
        _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel.OpenMutexW.restype = ctypes.c_void_p
        _kernel.OpenMutexW.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.LPCWSTR]
        h = _kernel.OpenMutexW(0x1F0001, False, MUTEX_DAEMON)
        if h:
            _kernel.CloseHandle(ctypes.c_void_p(h))
            return True
        return False
    except Exception:
        return False
def is_popup_running() -> bool:
    try:
        _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel.OpenMutexW.restype = ctypes.c_void_p
        _kernel.OpenMutexW.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.LPCWSTR]
        h = _kernel.OpenMutexW(0x1F0001, False, MUTEX_POPUP)
        if h:
            _kernel.CloseHandle(ctypes.c_void_p(h))
            return True
        return False
    except Exception:
        return False
# ═══════════════════════════════════════════════════════════════
# 日志（UTF-8，自动轮转）
# ═══════════════════════════════════════════════════════════════
def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        _rotate_log_if_needed()
        with open(LOG_PATH, "a", encoding=LOG_ENCODING) as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
def _rotate_log_if_needed() -> None:
    try:
        if not os.path.exists(LOG_PATH):
            return
        if os.path.getsize(LOG_PATH) <= 512_000:
            return
        with open(LOG_PATH, "r", encoding=LOG_ENCODING, errors="replace") as f:
            lines = f.readlines()
        if len(lines) > 200:
            with open(LOG_PATH, "w", encoding=LOG_ENCODING) as f:
                f.writelines(lines[-200:])
    except OSError:
        pass
def log_login_attempt(username: str, ok: bool) -> None:
    masked = username[:3] + "***" if len(username) > 3 else "***"
    log("[%s] 登录 %s%s" % ("OK" if ok else "FAIL", masked, current_suffix()))
# ═══════════════════════════════════════════════════════════════
# 主题色板
# ═══════════════════════════════════════════════════════════════
def palette(theme: str) -> dict:
    if theme == "light":
        return {
            "card_bg": (245, 245, 247, 255),
            "card_out": (225, 225, 232, 255),
            "divider": (228, 228, 235, 255),
            "text_main": (29, 29, 31, 255),
            "text_sub": (120, 120, 130, 255),
            "btn_bg": (232, 232, 237, 255),
            "btn_out": (225, 225, 232, 255),
            "btn_text": (29, 29, 31, 255),
            "btn_hover": (220, 220, 227, 255),
            "btn_disabled_text": (184, 184, 184, 255),
            "input_bg": (255, 255, 255, 255),
            "close": (140, 140, 150, 255),
        }
    return {
        "card_bg": (42, 42, 48, 255),
        "card_out": (62, 62, 70, 255),
        "divider": (58, 58, 66, 255),
        "text_main": (240, 240, 245, 255),
        "text_sub": (170, 170, 178, 255),
        "btn_bg": (62, 62, 70, 255),
        "btn_out": (82, 82, 92, 255),
        "btn_text": (238, 238, 243, 255),
        "btn_hover": (72, 72, 82, 255),
        "btn_disabled_text": (120, 120, 128, 255),
        "input_bg": (28, 28, 34, 255),
        "close": (170, 170, 178, 255),
    }
# ═══════════════════════════════════════════════════════════════
# 系统通知（纯 ctypes Shell_NotifyIcon 气泡，零子进程）
# ═══════════════════════════════════════════════════════════════
def notify(title: str, message: str, kind: str = "ok") -> None:
    if not get_cfg().get("notify", True):
        return
    threading.Thread(target=_notify_worker, args=(title, message, kind),
                     daemon=True).start()
def _notify_worker(title: str, message: str, kind: str) -> None:
    try:
        from ctypes import wintypes as _w
        import ctypes as _c
        class _NID(_c.Structure):
            _fields_ = [
                ("cbSize", _w.DWORD), ("hWnd", _w.HWND), ("uID", _w.UINT),
                ("uFlags", _w.UINT), ("uCallbackMessage", _w.UINT),
                ("hIcon", _w.HICON), ("szTip", _w.WCHAR * 128),
                ("dwState", _w.DWORD), ("dwStateMask", _w.DWORD),
                ("szInfo", _w.WCHAR * 256), ("uVersion", _w.UINT),
                ("szInfoTitle", _w.WCHAR * 64), ("dwInfoFlags", _w.DWORD),
            ]
        _shell32 = _c.windll.shell32
        _user32 = _c.windll.user32
        icon_name = "campus_notify.ico" if kind == "ok" else "campus_notify_err.ico"
        ico = os.path.join(DIR, icon_name)
        hicon = (_user32.LoadImageW(None, ico, 1, 32, 32, 0x10)
                 if os.path.exists(ico) else _user32.LoadIconW(None, 32512))
        hwnd = _user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                       None, None, None, None)
        nid = _NID()
        nid.cbSize = _c.sizeof(_NID)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = 0x01 | 0x02 | 0x04 | 0x10
        nid.uCallbackMessage = 0x0400 + 1
        nid.hIcon = hicon
        nid.szTip = "梨网通"
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = 0x04
        _shell32.Shell_NotifyIconW(0, _c.byref(nid))
        time.sleep(4.6)
        _shell32.Shell_NotifyIconW(2, _c.byref(nid))
        if hwnd:
            _user32.DestroyWindow(hwnd)
    except Exception as e:
        try:
            log("[WARN] 系统通知失败: %s" % e)
        except Exception:
            pass
# ═══════════════════════════════════════════════════════════════
# 开机自启（注册表 Run）
# ═══════════════════════════════════════════════════════════════
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
def _exe_path() -> str:
    return os.path.join(DIR, APP_EXE) if getattr(sys, "frozen", False) \
        else os.path.abspath(__file__)
def set_autostart(enable: bool) -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                              '"%s" daemon' % _exe_path())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except OSError:
        return False
def is_autostart() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                             winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
# ═══════════════════════════════════════════════════════════════
# 进程启动 / 隐藏控制台
# ═══════════════════════════════════════════════════════════════
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
def launch_windowed(args: list) -> bool:
    """启动独立 GUI 子进程，不创建控制台（CREATE_NO_WINDOW）。返回是否成功。"""
    try:
        subprocess.Popen([_exe_path()] + args, cwd=DIR,
                         creationflags=CREATE_NO_WINDOW)
        log(f"[PROC] 已启动子命令: {args}")
        return True
    except Exception as e:
        log(f"[PROC][FAIL] 启动子命令失败 {args}: {type(e).__name__}: {e}")
        return False
def launch_daemon_if_needed() -> bool:
    if is_daemon_running():
        return True
    try:
        subprocess.Popen([_exe_path(), "daemon"], creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False
def hide_console() -> None:
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass
# ═══════════════════════════════════════════════════════════════
# 退出控制
# ═══════════════════════════════════════════════════════════════
_shutdown = False
_INIT_EMBEDDED = False   # daemon 内嵌 init：保存后不退出，返回让 daemon 继续
def handle_signal(signum: int, frame: Any) -> None:
    global _shutdown
    _shutdown = True
def smart_sleep(seconds: float) -> None:
    global _shutdown
    end = time.monotonic() + seconds
    while time.monotonic() < end and not _shutdown:
        # 收到退出请求（卸载/托盘退出）时立即跳出，避免 daemon 在长 sleep 里迟迟不退出
        if app_quit_requested():
            _shutdown = True
            _stop_tray()          # 停掉托盘，daemon 主线程退出，进程真正结束
            break
        time.sleep(min(0.2, end - time.monotonic()))
# ═══════════════════════════════════════════════════════════════
# 系统托盘（pystray）
# ═══════════════════════════════════════════════════════════════
_tray_icon = None
_tray_state = "connecting"
_daemon_enabled = True
_daemon_thread = None
_daemon_cli = None
_daemon_cfg = None
_daemon_user = ""
_daemon_pw = None
def tray_make_icon(color, size=256):
    from PIL import Image, ImageDraw, ImageFont
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = S // 40
    d.ellipse([pad, pad, S - pad, S - pad], fill=color)
    d.ellipse([S // 10, S // 10, S - S // 10, S - S // 10],
              outline=(255, 255, 255, 80), width=max(3, S // 64))
    fs = int(S * 0.58)
    try:
        font = ImageFont.truetype("arialbd.ttf", fs)
    except Exception:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "G", font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((S - w) / 2 - bbox[0], (S - h) / 2 - bbox[1] - S * 0.04),
           "G", fill="white", font=font)
    return img
_tray_images = {}
def _tray_preload():
    if not _tray_images:
        _tray_images["connected"] = tray_make_icon(C_PINK)
        _tray_images["disconnected"] = tray_make_icon((158, 158, 158))
        _tray_images["connecting"] = tray_make_icon((205, 165, 172))
    return _tray_images
def tray_update(state: str) -> None:
    global _tray_state
    _tray_state = state
    if _tray_icon is None:
        return
    try:
        imgs = _tray_preload()
        _tray_icon.icon = imgs.get(state, imgs["disconnected"])
        st_map = {"connected": "已连接", "disconnected": "未连接",
                  "connecting": "连接中", "configuring": "修改设置"}
        guard = "开" if daemon_state_read() else "停"
        _tray_icon.title = f"{APP_DISPLAY}｜{st_map.get(state, state)}｜守护：{guard}"
    except Exception:
        pass
def _tray_toggle_daemon(icon, item):
    global _daemon_enabled
    _daemon_enabled = not _daemon_enabled
    daemon_state_write(_daemon_enabled)
    log(f"[INFO] 后台守护已{'开启' if _daemon_enabled else '暂停'}")
    try:
        tray_update(_tray_state)
        icon.update_menu()
    except Exception:
        pass
def _tray_toggle_autostart(icon, item):
    try:
        set_autostart(not is_autostart())
        log(f"[INFO] 开机自启已切换")
        icon.update_menu()
    except Exception:
        pass
def _tray_edit_config(icon, item):
    log("[TRAY] 打开修改账号密码")
    if is_init_running():
        log("[TRAY] 配置窗口已存在，忽略")
        return
    config_editing_write(True)     # 打开配置时暂停 daemon 自动认证
    if not launch_windowed(["init"]):
        config_editing_write(False)   # 启动失败则清除编辑状态，避免脏标记
def _tray_exit(icon=None, item=None):
    global _shutdown
    log("[TRAY] 用户点击退出")
    _shutdown = True
    request_app_quit()          # 用户真正退出：弹窗也会收到 app_quit 并退出
    _stop_tray()                # 统一停掉托盘，daemon 主线程退出，进程真正结束
def _stop_tray():
    """停止 pystray 托盘，让 icon.run() 返回，从而 daemon 主线程能退出（进程真正结束）。
    供 daemon 工作线程在检测到 app_quit（弹窗/卸载退出）时调用，避免 daemon 变成"幽灵托盘"。"""
    try:
        if _tray_icon is not None:
            _tray_icon.stop()
    except Exception as e:
        log(f"[TRAY] stop 失败: {type(e).__name__}: {e}")
def launch_and_activate_popup(timeout=5.0) -> bool:
    """确保 popup 进程存在并显示。
    - 已有 popup 进程：不强行外部 ShowWindow，而是写 popup.show 请求，让 popup 自己 deiconify。
    - 没有 popup：启动新进程，等待其 HWND 就绪（窗口由 popup 自身默认显示）。"""
    # 程序正在退出：拒绝再启动 popup（避免 app_quit 状态下弹窗一开就退/闪退）
    if app_quit_requested() or _shutdown:
        log("[POPUP] 程序正在退出，拒绝启动 popup")
        return False
    # 已经有 popup 进程，直接通知它自己恢复
    if is_popup_running():
        log("[POPUP] 进程已存在，发送显示请求")
        return request_popup_show()
    # 没有 popup，启动一个新的
    try:
        if not launch_windowed(["popup"]):
            log("[POPUP][FAIL] 启动 popup 进程失败")
            return False
    except Exception as e:
        log(f"[POPUP] launch failed: {e}")
        return False
    # 等待 popup 初始化完成（HWND 有效即视为窗口已显示）
    end = time.time() + timeout
    while time.time() < end:
        if popup_window_valid():
            log("[POPUP] popup 启动并显示成功")
            return True
        time.sleep(0.1)
    log("[POPUP] popup startup timeout")
    return False
def _tray_open_popup(icon=None, item=None):
    """托盘左键/右键"打开状态"：popup 进程存在则通知其自行恢复，否则启动并等待就绪。"""
    log("[TRAY] 打开主界面")
    # 程序正在退出：禁止再启动/恢复 popup（避免 app_quit 状态下闪退）
    if app_quit_requested() or _shutdown:
        log("[TRAY] 程序正在退出，忽略打开主界面请求")
        return
    if is_popup_running():
        log("[TRAY] popup 已在运行，发送显示请求")
        if request_popup_show():
            log("[TRAY] 已请求 popup 自行显示")
        else:
            log("[TRAY][FAIL] 显示请求写入失败")
        return
    if launch_and_activate_popup(timeout=5.0):
        log("[TRAY] 主界面已打开")
    else:
        log("[TRAY][FAIL] 主界面打开失败")
def _tray_setup(icon):
    """pystray `setup` 回调：图标准备好后，在单独线程启动 daemon 工作循环。"""
    global _daemon_thread
    log("[TRAY] setup 开始")
    try:
        icon.visible = True
        tray_update("connecting")
        _daemon_thread = threading.Thread(
            target=_daemon_worker,
            args=(_daemon_cli, _daemon_cfg, _daemon_user, _daemon_pw),
            name="DaemonWorker", daemon=True,
        )
        _daemon_thread.start()
        log(f"[TRAY] daemon 工作线程已启动 PID={os.getpid()}")
    except Exception as e:
        log(f"[TRAY][FATAL] setup 失败: {type(e).__name__}: {e}")
def _tray_run():
    global _tray_icon
    try:
        import pystray
        imgs = _tray_preload()
        menu = pystray.Menu(
            pystray.MenuItem("打开状态", _tray_open_popup, default=True),
            pystray.MenuItem("后台守护", _tray_toggle_daemon,
                             checked=lambda i: daemon_state_read()),
            pystray.MenuItem("开机自启动", _tray_toggle_autostart,
                             checked=lambda i: is_autostart()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("修改账号密码", _tray_edit_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _tray_exit),
        )
        icon = pystray.Icon(APP_NAME, imgs["connecting"],
                            f"{APP_DISPLAY}｜连接中", menu)
        _tray_icon = icon
        log("[TRAY] pystray 图标已创建")
        # 关键：daemon 主线程直接运行 pystray 的 Windows 消息循环（阻塞），
        # daemon 网络逻辑交给 setup 里启动的工作线程。
        icon.run(setup=_tray_setup)
        log("[TRAY] icon.run() 已退出")
    except Exception as e:
        log(f"[TRAY][FATAL] 托盘运行失败: {type(e).__name__}: {e}")
    finally:
        _tray_icon = None
        log("[TRAY] 托盘已退出")
# ═══════════════════════════════════════════════════════════════
# DrCOM 协议客户端
# ═══════════════════════════════════════════════════════════════
def parse_jsonp(text: str, callback: str) -> dict:
    m = re.search(re.escape(callback) + r"\((.*)\)\s*$", text.strip(), re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
class DrcomClient:
    """校园网认证客户端：状态探测 / 登录 / 注销。"""
    def __init__(self, cfg: dict):
        self.timeout = cfg.get("timeout", 8)
        self.retry_max = cfg.get("retry_max", 3)
        self.retry_backoff = cfg.get("retry_backoff", 2)
        self.grace_period = cfg.get("grace_period", 15)
        self._last_login_ok = 0.0
    # -- 连通性 --
    def internet_ok(self) -> bool:
        """外网是否可达：Dr.COM 未认证时会被劫持到认证服务器。"""
        try:
            r = requests.get("https://www.baidu.com", timeout=3,
                             allow_redirects=False)
            if r.status_code in (301, 302):
                return "10.255.0.19" not in r.headers.get("Location", "")
            return r.status_code == 200
        except requests.RequestException:
            return False
    def is_connected(self) -> bool:
        # 严格以 Dr.COM 认证状态为准，不再把"外网可达"当作已连接
        return self.get_status().get("connected", False)
    def status_info(self) -> dict:
        try:
            r = requests.get(f"{BASE_URL}/drcom/chkstatus?callback=dr1002",
                             timeout=self.timeout)
            d = parse_jsonp(r.text, "dr1002")
            return {"ip": d.get("v46ip") or d.get("v4ip", ""),
                    "uid": d.get("uid", ""), "result": d.get("result", "")}
        except requests.RequestException:
            return {}
    def get_status(self) -> dict:
        """严格使用 Dr.COM 状态判断校园网认证状态：仅 chkstatus 明确返回 result=1 才算已连接。
        外网可达（百度等）不能单独作为"校园网已认证"的依据，避免开机/VPN/热点时误判已连接。"""
        try:
            r = requests.get(f"{BASE_URL}/drcom/chkstatus?callback=dr1002",
                             timeout=self.timeout)
            d = parse_jsonp(r.text, "dr1002")
            result = str(d.get("result", ""))
            ip = d.get("v46ip") or d.get("v4ip", "")
            return {"connected": result == "1", "ip": ip}
        except requests.RequestException as e:
            log(f"[STATUS] chkstatus 请求失败: {e}")
            return {"connected": False, "ip": ""}
        except Exception as e:
            log(f"[STATUS] 状态解析失败: {type(e).__name__}: {e}")
            return {"connected": False, "ip": ""}
    # -- 登录 --
    def _login_post_a79(self, user: str, pw: str) -> bool:
        data = {"callback": "dr1003", "DDDDD": user + current_suffix(),
                "upass": pw, "0MKKey": "123456", "R1": "0", "R3": "0",
                "R6": "0", "para": "00", "v6ip": ""}
        r = requests.post(f"{BASE_URL}/a79.htm", data=data, timeout=5,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        t = r.text
        return ("login_ok" in t or ("UID=" in t and "ac0=" in t)
                or "认证成功" in t or "Dr.COMWebLoginID_3" in t)
    def _login_get(self, user: str, pw: str) -> bool:
        params = {"callback": "dr1003", "DDDDD": user + current_suffix(),
                  "upass": pw, "R1": "0", "R2": "1", "R3": "0", "R6": "0",
                  "para": "00", "0MKKey": "123456", "v6ip": ""}
        r = requests.get(f"{BASE_URL}/drcom/login", params=params, timeout=5)
        return parse_jsonp(r.text, "dr1003").get("result") == "1"
    def _login_post(self, user: str, pw: str) -> bool:
        data = {"DDDDD": user + current_suffix(), "upass": pw,
                "callback": "dr1003", "R1": "0", "R3": "0", "R6": "0",
                "para": "00", "0MKKey": "123456", "v6ip": ""}
        r = requests.post(f"{BASE_URL}/drcom/login", data=data, timeout=5)
        return parse_jsonp(r.text, "dr1003").get("result") == "1"
    def login(self, user: str, pw: str) -> bool:
        try:
            ok = self._login_post_a79(user, pw) or self._login_get(user, pw) \
                or self._login_post(user, pw)
        except requests.RequestException:
            ok = False
        if ok:
            self._last_login_ok = time.monotonic()
        return ok
    def login_with_retry(self, user: str, pw: str) -> bool:
        for attempt in range(self.retry_max):
            if _shutdown:
                return False
            if self.login(user, pw):
                for _ in range(3):
                    if self.internet_ok():
                        return True
                    time.sleep(1)
                continue
            if attempt < self.retry_max - 1:
                time.sleep(self.retry_backoff ** attempt)
        return False
    def logout(self) -> bool:
        try:
            requests.get(f"{BASE_URL}/drcom/logout?callback=dr1004",
                         timeout=5)
            return True
        except requests.RequestException:
            return False
    def in_grace_period(self) -> bool:
        return (self._last_login_ok > 0
                and time.monotonic() - self._last_login_ok < self.grace_period)
# ═══════════════════════════════════════════════════════════════
# 命令：login
# ═══════════════════════════════════════════════════════════════
def cmd_login() -> int:
    cfg = get_cfg()
    user = cfg.get("username", "")
    pw = resolve_password(cfg)
    if not user:
        log("[FAIL] 未配置账号")
        return 1
    if pw is None:
        log("[FAIL] 未找到密码")
        return 1
    cli = DrcomClient(cfg)
    try:
        if cli.is_connected():
            info = cli.status_info()
            log(f"[OK] 已连接 | {user}{current_suffix()}"
                + (f" | IP: {info['ip']}" if info.get('ip') else ""))
            return 0
        if cli.login_with_retry(user, pw):
            manual_logout_write(False)
            log_login_attempt(user, True)
            notify(APP_DISPLAY, f"登录成功 · {user}", "ok")
            return 0
        log_login_attempt(user, False)
        notify(APP_DISPLAY, "登录失败，请检查账号/密码/运营商", "err")
        return 1
    finally:
        pass
# ═══════════════════════════════════════════════════════════════
# 命令：daemon（后台守护 + 托盘）
# ═══════════════════════════════════════════════════════════════
def _daemon_worker(cli, cfg, user, pw):
    """daemon 网络工作循环：在独立线程运行，主线程专职 pystray 托盘消息循环。"""
    global _daemon_enabled, _shutdown
    interval = cfg.get("daemon_interval", 30)
    fast = cfg.get("daemon_check_connected", 10)
    was_conn = False
    log(f"[*] 守护工作线程启动 | 轮询 {interval}s | 账号 {user}")

    # ── 开机首次认证：给 DHCP / Wi-Fi / 网卡 2 秒初始化时间，再主动探测/登录一次 ──
    # 目的：开机自启后即使后台守护默认开启，也要先确认真实认证状态，
    # 避免"百度可达/外网通"被当作"校园网已连接"的假象。
    smart_sleep(2)
    # 开机首次认证：不依赖"后台守护"开关——只要账号已配置且未手动注销，开机即主动登录一次。
    # 守护开关只负责"掉线后是否持续自动重连"，避免用户刚开机却因旧的 daemon_enabled=false 而登不上。
    if not _shutdown and not manual_logout_read():
        log("[DAEMON] 执行开机首次认证")
        try:
            status = cli.get_status()
            if status.get("connected"):
                ip = status.get("ip", "")
                was_conn = True
                daemon_write_state("connected", ip)
                tray_update("connected")
                log(f"[DAEMON] 开机检测已认证 | IP={ip or '--'}")
            else:
                tray_update("connecting")
                daemon_write_state("connecting", "")
                if cli.login_with_retry(user, pw):
                    confirm = cli.get_status()
                    if confirm.get("connected"):
                        ip = confirm.get("ip", "")
                        was_conn = True
                        daemon_write_state("connected", ip)
                        tray_update("connected")
                        log_login_attempt(user, True)
                        notify(APP_DISPLAY, f"登录成功 · {user}", "ok")
                        log(f"[DAEMON] 开机首次登录成功 | IP={ip or '--'}")
                    else:
                        daemon_write_state("disconnected", "")
                        tray_update("disconnected")
                        log("[DAEMON] 开机登录后状态确认失败")
                else:
                    daemon_write_state("disconnected", "")
                    tray_update("disconnected")
                    log("[DAEMON] 开机首次登录失败")
        except Exception as e:
            log(f"[DAEMON] 开机首次认证异常: {type(e).__name__}: {e}")
            daemon_write_state("disconnected", "")
            tray_update("disconnected")

    while not _shutdown:
        try:
            _daemon_enabled = daemon_state_read()
            if app_quit_requested():
                log("[INFO] 收到退出请求")
                _shutdown = True
                _stop_tray()          # 停掉托盘，daemon 主线程退出，进程真正结束（避免残留/闪退）
                break
            if not _daemon_enabled:
                # 守护关闭时：如实反映开机首次认证后的真实状态，不再恒定显示"连接中"
                tray_update(read_runtime().get("state", "disconnected"))
                smart_sleep(1)
                continue
            if manual_logout_read():
                # 用户主动注销：优先级最高（除退出/守护关闭外），绝不自动重连，直到手动点「连接」
                was_conn = False
                tray_update("disconnected")
                daemon_write_state("disconnected", "")
                smart_sleep(fast)
                continue
            # 自动修复：配置窗口已不存在但标记残留，则清除（防 init 崩溃/Alt+F4/被结束导致脏状态）
            if config_editing_read() and not is_init_running():
                log("[DAEMON] 配置窗口已不存在，自动清除 config_editing")
                config_editing_write(False)
            # 正在修改账号密码（配置窗口打开且存活）：暂停自动认证，避免"旧账号自动登录 + 新账号保存"冲突
            if config_editing_read() and is_init_running():
                tray_update("configuring")
                smart_sleep(1)
                continue
            # 热加载配置（支持托盘改账号后生效）
            cfg_now = get_cfg()
            new_user = cfg_now.get("username", "")
            new_suffix = cfg_now.get("suffix", DEFAULT_SUFFIX)
            config_changed = False
            if new_user != user or new_suffix != cfg.get("suffix", DEFAULT_SUFFIX):
                log(f"[DAEMON] 检测到账号/运营商配置变化: {user}{cfg.get('suffix','')} -> {new_user}{new_suffix}")
                user = new_user
                cfg = cfg_now
                cli = DrcomClient(cfg)
                config_changed = True
            pw_new = resolve_password(cfg_now)
            if pw_new is not None and pw_new != pw:
                log("[DAEMON] 检测到密码变更，已刷新凭据")
                pw = pw_new
                config_changed = True
            if config_changed:
                # 配置已变化：先注销旧会话，再用新账号重新认证（即使校园网当前仍在线也要换账号）
                log("[DAEMON] 配置已变化，准备重新认证")
                was_conn = False
                try:
                    cli.logout()
                except Exception as e:
                    log(f"[DAEMON] 注销旧会话失败: {e}")
                daemon_write_state("connecting", "")
                tray_update("connecting")
                if cli.login_with_retry(user, pw):
                    log("[DAEMON] 新账号重新认证成功")
                    daemon_write_state("connected", "")
                    tray_update("connected")
                else:
                    log("[DAEMON] 新账号重新认证失败")
                    daemon_write_state("disconnected", "")
                    tray_update("disconnected")
                smart_sleep(fast)
                continue
            status = cli.get_status()
            conn = status["connected"]
            ip = status.get("ip", "")
            if conn:
                if not was_conn:
                    log(f"[OK] 已连接 | {user}{current_suffix()}")
                    notify(APP_DISPLAY, f"已连接 · {user}", "ok")
                was_conn = True
                tray_update("connected")
                daemon_write_state("connected", ip)
                smart_sleep(fast)
                continue
            if cli.in_grace_period():
                smart_sleep(fast)
                continue
            was_conn = False
            tray_update("connecting")
            daemon_write_state("connecting", ip)
            if cli.login_with_retry(user, pw):
                log_login_attempt(user, True)
                notify(APP_DISPLAY, f"登录成功 · {user}", "ok")
                daemon_write_state("connected", ip)
                tray_update("connected")
                smart_sleep(fast)
            else:
                log(f"[FAIL] 重试{cli.retry_max}次均失败")
                tray_update("disconnected")
                daemon_write_state("disconnected", "")
                smart_sleep(interval)
        except requests.RequestException as e:
            log(f"[WARN] 网络异常: {e}")
            was_conn = False
            tray_update("disconnected")
            daemon_write_state("disconnected", "")
            smart_sleep(interval)
        except Exception as e:
            log(f"[DAEMON][ERROR] {type(e).__name__}: {e}")
            smart_sleep(interval)
    log("[DAEMON] 守护工作线程退出")

def cmd_daemon() -> int:
    global _daemon_enabled, _shutdown, _daemon_cli, _daemon_cfg, _daemon_user, _daemon_pw
    if not acquire_singleton(MUTEX_DAEMON):
        return 0
    # 清除历史退出/注销标记
    rt = read_runtime()
    rt["app_quit"] = False
    rt["manual_logout"] = False      # 每次"本次运行"启动守护都允许自动登录；手动注销只在本会话生效
    rt.pop("quit", None)          # 清理旧的 quit 字段，统一用 app_quit
    write_runtime(rt)
    cfg = get_cfg()
    user = cfg.get("username", "")
    pw = resolve_password(cfg)
    if not user or pw is None:
        # 首次运行（安装后双击/自启）：内嵌初始化向导
        log("[INFO] 未配置账号，弹出初始化向导")
        hide_console()
        global _INIT_EMBEDDED, _saved_ok
        _INIT_EMBEDDED = True
        try:
            rc = cmd_init()
        finally:
            _INIT_EMBEDDED = False
        if rc != 0:
            log("[FAIL] 初始化未完成，守护退出")
            return 1
        # 初始化保存成功：打开主界面，继续守护（统一走 launch_and_activate_popup，保证单一 popup 启动入口）
        launch_and_activate_popup(timeout=5.0)
        cfg = get_cfg()
        user = cfg.get("username", "")
        pw = resolve_password(cfg)
        if not user or pw is None:
            log("[FAIL] 未配置账号或密码")
            return 1
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    _shutdown = False
    _daemon_enabled = daemon_state_read()
    # 供 _tray_setup 启动工作线程时使用
    _daemon_cfg = cfg
    _daemon_user = user
    _daemon_pw = pw
    _daemon_cli = DrcomClient(cfg)
    hide_console()
    log(f"[*] 守护启动 | 账号 {user}")
    # 主线程直接进入 pystray 的 Windows 消息循环（阻塞）；网络工作循环由 setup 线程启动
    _tray_run()
    # icon.run() 返回 = 托盘已退出
    _shutdown = True
    _stop_tray()   # 最后一道保险：确保托盘停止、进程真正结束
    if _daemon_thread is not None:
        try:
            _daemon_thread.join(timeout=2)
        except Exception:
            pass
    log("[INFO] 守护退出")
    return 0
# ═══════════════════════════════════════════════════════════════
# 命令：config / status / autostart / diagnostic
# ═══════════════════════════════════════════════════════════════
def cmd_config() -> int:
    """命令行配置（GUI 见 cmd_init）。"""
    user = input("校园网账号（学号）: ").strip()
    pw = input("校园网密码（明文）: ").strip()
    print("选择运营商:")
    for i, op in enumerate(OPERATORS, 1):
        print(f"  {i}. {op['name']}")
    sel = input("编号 [2-联通]: ").strip()
    try:
        op = OPERATORS[int(sel) - 1] if sel else OPERATORS[1]
    except Exception:
        op = OPERATORS[1]
    err = validate_account(user, pw)
    if err:
        print(f"[FAIL] {err}")
        return 1
    if cred_save(user + op["suffix"], pw):
        cfg = load_config()
        cfg["username"] = user
        cfg["suffix"] = op["suffix"]
        cfg.pop("password", None)
        save_config(cfg)
        print(f"[OK] 配置完成: {user}（{op['name']}）")
        return 0
    print("[FAIL] 凭据写入失败")
    return 1
def cmd_status() -> int:
    cfg = get_cfg()
    user = cfg.get("username", "")
    print(f"=== {APP_DISPLAY} 状态 ===")
    print(f"  账号:   {user}（{op_display_name()}）")
    print(f"  凭据:   {'已存储' if cred_read() else '未存储'}")
    print(f"  自启:   {'开启' if is_autostart() else '关闭'}")
    print(f"  守护:   {'运行中' if is_daemon_running() else '未运行'}")
    cli = DrcomClient(cfg)
    conn = cli.is_connected()
    print(f"  网络:   {'已连接' if conn else '未连接'}")
    if conn:
        info = cli.status_info()
        if info.get("ip"):
            print(f"  IP:     {info['ip']}")
    return 0
def cmd_autostart() -> int:
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if arg == "on":
        ok = set_autostart(True)
        print("[OK] 开机自启已开启" if ok else "[FAIL] 开机自启写入失败")
    elif arg == "off":
        ok = set_autostart(False)
        print("[OK] 开机自启已关闭" if ok else "[FAIL] 开机自启写入失败")
    else:
        print(f"当前: {'开启' if is_autostart() else '关闭'}")
        print("用法: campus_login.exe autostart [on|off]")
    return 0
def cmd_diagnostic() -> int:
    cfg = get_cfg()
    user = cfg.get("username", "")
    lines = [f"=== {APP_DISPLAY} 诊断 ==",
             f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"账号: {user}（{op_display_name()}）",
             f"密码: {'已存储' if cred_read() else '未存储'}",
             f"自启: {'开' if is_autostart() else '关'}",
             f"守护: {'运行中' if is_daemon_running() else '未运行'}"]
    try:
        cli = DrcomClient(cfg)
        lines.append("网络: " + ("已连接" if cli.is_connected() else "未连接"))
    except Exception:
        lines.append("网络: 检测失败")
    text = "\n".join(lines)
    print(text)
    try:
        subprocess.run(["clip"], input=text.encode("utf-16le"),
                       creationflags=CREATE_NO_WINDOW)
        print("\n（已复制到剪贴板）")
    except Exception:
        pass
    return 0
def cmd_quit() -> int:
    """退出后台守护/主界面：置 app_quit，让 daemon/popup 退出（供卸载/托盘退出前调用）。
    等待它们真正退出（释放 梨网通.exe / _internal 的文件锁），避免卸载残留。"""
    request_app_quit()
    end = time.time() + 15
    while time.time() < end:
        if not is_daemon_running() and not is_popup_running():
            break
        time.sleep(0.2)
    return 0
# ═══════════════════════════════════════════════════════════════
# GUI 共享绘图辅助
# ═══════════════════════════════════════════════════════════════
def _load_fonts(ss):
    """返回 (emoji, bold, title, txt, sub, btn, x) 字体组。"""
    from PIL import ImageFont
    def _f(path, size):
        try:
            return ImageFont.truetype(path, int(size * ss))
        except Exception:
            return ImageFont.load_default()
    return (
        _f(r"C:\Windows\Fonts\seguiemj.ttf", 19),
        _f(r"C:\Windows\Fonts\msyhbd.ttc", 12),
        _f(r"C:\Windows\Fonts\msyhbd.ttc", 15),
        _f(r"C:\Windows\Fonts\msyh.ttc", 10),
        _f(r"C:\Windows\Fonts\msyh.ttc", 9),
        _f(r"C:\Windows\Fonts\msyh.ttc", 10),
        _f(r"C:\Windows\Fonts\msyh.ttc", 10),
    )
def _round_rect(d, x1, y1, x2, y2, r, **kw):
    d.rounded_rectangle([x1, y1, x2, y2], radius=r, **kw)
def _text_c(d, cx, cy, s, f, fill):
    tw = d.textlength(s, font=f)
    bb = d.textbbox((0, 0), s, font=f)
    d.text((cx - tw / 2, cy - (bb[3] - bb[1]) / 2), s, font=f, fill=fill)
def _hex(c):
    return "#%02x%02x%02x" % tuple(c[:3])
# ═══════════════════════════════════════════════════════════════
# popup：主界面（托盘左键弹出）
# 整卡 PIL 渲染，含连接/注销状态机、主题切换、守护/自启开关
# ═══════════════════════════════════════════════════════════════



# ========== 豆沙系完整颜色常量（PIL 渲染用, 与全局一致） ==========
COLOR_BG_MAIN       = "#F5F5F7"
COLOR_TEXT_PRIMARY  = (29, 29, 31)        # #1D1D1F
COLOR_TEXT_SECOND   = (120, 120, 130)     # #787882
COLOR_TEXT_DISABLE  = (184, 184, 184)     # #B8B8B8
COLOR_STATUS_OK     = (98, 140, 110)      # 豆沙绿-已连接 C_GREEN
COLOR_STATUS_WARN   = (230, 180, 60)      # 连接中 黄色呼吸
COLOR_STATUS_OFF    = (150, 150, 150)     # 未连接灰
COLOR_BTN_PRIMARY   = (74, 115, 153)      # 豆沙蓝-连接 C_BLUE
COLOR_BTN_DANGER    = (224, 108, 117)     # 豆沙粉-注销/退出 C_PINK
COLOR_BTN_SECOND    = (232, 232, 237)     # 次要: 修改账号密码 #E8E8ED
COLOR_BTN_PRIMARY_HOVER = (62, 98, 130)   # #3E6282
COLOR_BTN_PRIMARY_PRESS = (51, 80, 107)   # #33506B
COLOR_BTN_DANGER_HOVER  = (200, 84, 92)   # #C8545C
COLOR_BTN_DANGER_PRESS  = (173, 67, 74)   # #AD434A
COLOR_SWITCH_ON     = (98, 140, 110)      # 开: 豆沙绿
COLOR_SWITCH_OFF    = (160, 160, 168)     # 关: 灰


def cmd_popup() -> int:
    """🍐 网通 主窗口 440x330：状态行 + 账号IP + 左右分栏(开关连接注销) + 底部修改密码退出。"""
    import tkinter as tk
    from PIL import Image, ImageDraw, ImageFont, ImageTk

    if not acquire_singleton(MUTEX_POPUP):
        return 0
    hide_console()
    clear_popup_show_request()   # 清理异常退出残留的 popup.show 请求，避免一启动就被误恢复

    cfg = get_cfg()
    username = cfg.get("username", "") or "未配置"

    # —— 布局参数（逻辑像素, 4 为基础间距 scale）——
    SCALE = 0.9                       # 全局等比缩放(小而美)
    W, H = int(400*SCALE), int(330*SCALE)      # 360x295
    TITLE_H = int(48*SCALE)
    PADDING = int(24*SCALE)
    LEFT_X = PADDING
    LEFT_START_Y = TITLE_H + int(16*SCALE)
    # 右列连接/注销/退出（竖排一列, 均匀等距, 退出底与底部按钮顶对齐）
    RIGHT_BTN_W, RIGHT_BTN_H = int(160*SCALE), int(40*SCALE)
    RIGHT_X = W - PADDING - RIGHT_BTN_W
    RIGHT_BTN_SPACING = int(44*SCALE)           # 与左栏开关行距一致的节奏
    # 底部按钮行
    BOT_H = int(38*SCALE)
    BOT_Y = H - PADDING - BOT_H       # 贴底

    dpi = get_dpi_scale()
    SS = max(2, int(round(2 * dpi)))
    pw, ph = int(W * dpi), int(H * dpi)

    root = tk.Tk()
    root.report_callback_exception = _tk_crash_exc
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    try:
        root.attributes("-transparentcolor", "#FF00FE")
    except Exception:
        pass
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{pw}x{ph}+{sw - pw - int(12*dpi)}+{sh - ph - int(60*dpi)}")
    c = tk.Canvas(root, width=pw, height=ph, bg="#FF00FE",
                  highlightthickness=0, bd=0)
    c.pack()
    # 窗口尺寸/UI 就绪后再写 HWND，避免托盘过早读到未完全初始化的窗口
    try:
        root.update_idletasks()
        root.update()
        hwnd = int(root.winfo_id())   # 直接用 Tk 顶层窗口句柄（不要 GetParent，避免拿到其它句柄）
        write_popup_hwnd(hwnd)
        log(f"[POPUP] HWND={hwnd}")
    except Exception as e:
        log(f"[POPUP] 获取/记录 HWND 失败: {type(e).__name__}: {e}")

    f_emoji = ImageFont.truetype(r"C:\Windows\Fonts\seguiemj.ttf", int(22*SCALE*SS))
    f_title = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", int(17*SCALE*SS))
    f_status = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", int(15*SCALE*SS))
    f_lbl = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", int(12*SCALE*SS))
    f_val = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", int(13*SCALE*SS))
    f_btn = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", int(13*SCALE*SS))
    f_sub = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", int(10*SCALE*SS))

    ui = {"busy": None, "confirm": None, "toast": None, "frame": 0,
          "flash": True, "ok_flash": None, "hover": None, "pressed": None,
          "autostart": is_autostart()}

    def show_toast(text, color=(235, 200, 90)):
        ui["toast"] = (text, time.monotonic() + 2.2, color)

    def _draw_pear(d, cx, cy, scale, size, color):
        """参考图风格梨：淡黄绿圆润梨身 + 右侧淡绿阴影 + 左上高光 + 绿色梗叶。cx,cy=中心(逻辑)。"""
        S = SS
        w = size * 0.66          # 梨肚半宽
        h = size * 0.74          # 半高
        neck_w = size * 0.30     # 颈窄
        # 分层配色(参考图: 主面淡黄绿, 右侧/底更深绿)
        base = (228, 238, 168)        # 淡黄绿底色
        side = (188, 214, 96)         # 右侧/底阴影绿
        hi = (255, 255, 235)          # 高光
        # 右侧/底: 阴影绿(比梨身大一点的椭圆, 偏右)
        d.ellipse([(cx-w*0.55)*S, (cy-h*1.0)*S, (cx+w*1.05)*S,
                   (cy+h*1.05)*S], fill=side)
        # 主梨身: 头小肚大(颈窄椭圆 + 肚椭 + 过渡)
        d.ellipse([(cx-neck_w)*S, (cy-h*1.0)*S, (cx+neck_w)*S,
                   (cy+h*0.1)*S], fill=base)
        d.ellipse([(cx-w)*S, (cy-h*0.35)*S, (cx+w)*S,
                   (cy+h*1.0)*S], fill=base)
        d.ellipse([(cx-w*0.7)*S, (cy-h*0.6)*S, (cx+w*0.7)*S,
                   (cy+h*0.5)*S], fill=base)
        # 左上高光(白色弧/椭圆)
        d.ellipse([(cx-w*0.5)*S, (cy-h*0.15)*S, (cx-w*0.1)*S,
                   (cy+h*0.25)*S], fill=hi)
        # 绿色梗(带弧度, 向右上)
        d.line([(cx-1*S, (cy-h*1.0)*S), (cx-1*S, (cy-h*1.2)*S)],
               fill=(124, 174, 62), width=max(2, int(2*S)))
        # 绿色叶子(梗右上, 参考图有叶)
        leaf = 6*S
        lx0 = (cx + neck_w*0.6)*S; ly0 = (cy-h*1.18)*S
        d.ellipse([lx0, ly0, lx0+leaf, ly0+leaf*0.55],
                  fill=(154, 200, 92))
        d.ellipse([lx0, ly0, lx0+leaf, ly0+leaf*0.7],
                  fill=(110, 170, 76))

    def render(state, on, ip, theme, connected, auto_on, username):
        P = palette(theme)
        S = SS
        im = Image.new("RGBA", (int(W*S), int(H*S)), (255, 0, 254, 255))
        d = ImageDraw.Draw(im)
        # 主卡片
        _round_rect(d, 2, 2, W*S-2, H*S-2, 12*S, fill=P["card_bg"],
                    outline=P["card_out"], width=max(1, S))

        # ① 标题栏：左 🍐网通 | 右 ☀ ×（梨子与文字垂直居中于标题栏中线）
        _draw_pear(d, PADDING+int(12*SCALE), TITLE_H//2, 1.0,
                   int(11*SCALE), C_PEAR)
        d.text(((PADDING+int(30*SCALE))*S, (TITLE_H//2)*S), "网通", font=f_title,
               fill=P["text_main"], anchor="lm")
        bx = W*S - int(16*SCALE)*S
        # 太阳 / 叉：两个等大圆形按钮区域, 垂直居中于标题栏, 间距协调(随SCALE缩小)
        ICON_R = int(20*SCALE)*S         # 圆形按钮半径
        cyc = (TITLE_H//2)*S             # 垂直中心
        close_cx = bx - ICON_R        # 叉圆心
        theme_cx = close_cx - (ICON_R*2 + int(16*SCALE)*S)   # 太阳圆心(随SCALE留间距)
        for cx, key in ((theme_cx, "theme"), (close_cx, "close")):
            hv = ui.get("hover") == key
            pr = ui.get("pressed") == key
            bg = P["btn_hover"] if hv else P["card_bg"]
            if pr:
                bg = tuple(max(0, x-15) for x in bg)
            d.ellipse([cx-ICON_R, cyc-ICON_R, cx+ICON_R, cyc+ICON_R],
                      fill=bg, outline=P["card_out"])
        # ☀ 卡通太阳 / 🌙 卡通月亮（随 SCALE 等比缩放）
        import math as _m
        k = SCALE
        if theme == "light":
            # 卡通太阳: 大黄圆脸 + 圆润短光芒(可弯曲) + 微笑
            sun_face = (250, 205, 60)
            sun_ray = (248, 180, 40)
            r_face = 8*k*S
            # 圆润光芒(12根, 粗短圆头)
            for a in range(0, 360, 30):
                ang = _m.radians(a)
                x0 = theme_cx + _m.cos(ang)*(r_face+1*k*S)
                y0 = cyc + _m.sin(ang)*(r_face+1*k*S)
                x1 = theme_cx + _m.cos(ang)*(r_face+7*k*S)
                y1 = cyc + _m.sin(ang)*(r_face+7*k*S)
                d.line([x0, y0, x1, y1], fill=sun_ray,
                       width=max(3, int(3*k*S)))
            d.ellipse([theme_cx-r_face, cyc-r_face, theme_cx+r_face,
                       cyc+r_face], fill=sun_face)
            # 眼睛(两点)
            d.ellipse([theme_cx-4*k*S, cyc-3*k*S, theme_cx-2*k*S, cyc-1*k*S],
                      fill=(120, 80, 20))
            d.ellipse([theme_cx+2*k*S, cyc-3*k*S, theme_cx+4*k*S, cyc-1*k*S],
                      fill=(120, 80, 20))
            # 微笑
            d.arc([theme_cx-4*k*S, cyc+1*k*S, theme_cx+4*k*S, cyc+5*k*S],
                  start=15, end=165, fill=(120, 80, 20),
                  width=max(1, int(1*S)))
        else:
            # 卡通月亮: 大黄新月(圆润) + 柔和
            moonc = (250, 225, 130)
            moon_hi = (255, 245, 190)
            mr = 12*k*S
            d.ellipse([theme_cx-mr, cyc-mr, theme_cx+mr, cyc+mr],
                      fill=moonc)
            # 新月缺口
            d.ellipse([theme_cx+mr*0.5, cyc-mr*1.0, theme_cx+mr*1.6,
                       cyc+mr*0.9], fill=P["card_bg"])
            # 上弦高光
            d.ellipse([theme_cx-mr*0.7, cyc-mr*0.7, theme_cx-mr*0.1,
                       cyc-mr*0.1], fill=moon_hi)
        # × 叉（灰色, 垂直水平居中）
        d.line([close_cx-8*S, cyc-8*S, close_cx+8*S, cyc+8*S],
               fill=P["text_sub"], width=int(1.6*S))
        d.line([close_cx-8*S, cyc+8*S, close_cx+8*S, cyc-8*S],
               fill=P["text_sub"], width=int(1.6*S))
        d.line([0, TITLE_H*S, W*S, TITLE_H*S], fill=P["divider"],
               width=max(1, S))

        # ② 左：状态行（单独一行）
        sy = LEFT_START_Y + int(8*SCALE)
        dot_c = {"connected": COLOR_STATUS_OK,
                 "disconnected": COLOR_STATUS_OFF,
                 "connecting": COLOR_STATUS_WARN}.get(state, COLOR_STATUS_OFF)
        hollow = state == "disconnected"
        rdot = int(9*SCALE)*S
        if hollow:
            d.ellipse([LEFT_X*S, sy*S-rdot, LEFT_X*S+2*rdot, sy*S+rdot],
                      outline=dot_c, width=max(2, S))
        else:
            d.ellipse([LEFT_X*S, sy*S-rdot, LEFT_X*S+2*rdot, sy*S+rdot],
                      fill=dot_c)
        if state == "connecting":
            if ui["flash"]:
                d.ellipse([(LEFT_X-4*SCALE)*S, (sy-4*SCALE)*S-rdot,
                           (LEFT_X+9*SCALE)*S, (sy+4*SCALE)*S+rdot],
                          outline=(230, 180, 60, 90), width=max(1, S))
        lbl = {"connected": "已连接", "disconnected": "未连接",
               "connecting": "连接中…"}.get(state, state)
        d.text(((LEFT_X+int(30*SCALE))*S, (sy-int(12*SCALE))*S), lbl,
               font=f_status,
               fill=COLOR_STATUS_OK if state == "connected"
               else P["text_main"])

        # ③ 左：账号 / IP（两行, 紧凑信息组, 行距28*SCALE）
        ay0 = sy + int(36*SCALE)
        rows = [("账号", username), ("IP", ip or "--")]
        for i, (k, v) in enumerate(rows):
            yy = (ay0 + i*int(28*SCALE))*S
            d.text((LEFT_X*S, yy), k, font=f_lbl, fill=COLOR_TEXT_SECOND)
            d.text(((LEFT_X+int(72*SCALE))*S, yy), v, font=f_val,
                   fill=P["text_main"])

        # ④ 左：两开关（行距44*SCALE, 与右栏按钮同节奏）
        sy0 = ay0 + 2*int(28*SCALE) + int(24*SCALE)
        sw_items = [("后台守护", on, "guard"), ("开机自启", auto_on, "autostart")]
        for i, (lb, val, kind) in enumerate(sw_items):
            yy = (sy0 + i*int(44*SCALE))*S
            d.text((LEFT_X*S, yy), lb, font=f_lbl, fill=P["text_main"])
            sw_w, sw_h = int(48*SCALE)*S, int(26*SCALE)*S
            sx1 = (LEFT_X + int(176*SCALE))*S
            sx0 = sx1 - sw_w
            col = COLOR_SWITCH_ON if val else COLOR_SWITCH_OFF
            hv = ui.get("hover") == kind
            pr = ui.get("pressed") == kind
            if hv:
                col = tuple(max(0, x-10) for x in col)
            if pr:
                col = tuple(max(0, x-20) for x in col)
            _round_rect(d, sx0, yy, sx1, yy+sw_h, int(13*SCALE)*S, fill=col,
                        outline=((255, 255, 255) if hv else col),
                        width=max(1, S))
            kx = sx1-int(13*SCALE)*S if val else sx0+int(13*SCALE)*S
            d.ellipse([kx-int(11*SCALE)*S, yy+int(2*SCALE)*S,
                       kx+int(11*SCALE)*S, yy+sw_h-int(2*SCALE)*S],
                      fill=(255, 255, 255))

        # ⑤ 右：竖排 连接/注销/退出（均匀等距, 退出底边与修改账号密码底边对齐）
        busy = ui["busy"] is not None
        okf = ui["ok_flash"]
        bot_bottom = BOT_Y + BOT_H          # 底部按钮底边(对齐线)
        btn_exit_y = bot_bottom - RIGHT_BTN_H
        btn_logout_y = btn_exit_y - RIGHT_BTN_H - RIGHT_BTN_SPACING
        btn_connect_y = btn_logout_y - RIGHT_BTN_H - RIGHT_BTN_SPACING
        for kind, by0, label, col, hv_c, pr_c in (
                ("login", btn_connect_y, "连接", COLOR_BTN_PRIMARY,
                 COLOR_BTN_PRIMARY_HOVER, COLOR_BTN_PRIMARY_PRESS),
                ("logout", btn_logout_y, "注销", COLOR_BTN_DANGER,
                 COLOR_BTN_DANGER_HOVER, COLOR_BTN_DANGER_PRESS),
                ("appquit", btn_exit_y, "退出", COLOR_BTN_DANGER,
                 COLOR_BTN_DANGER_HOVER, COLOR_BTN_DANGER_PRESS)):
            enabled = (not busy and
                       ((kind == "login" and not connected) or
                        (kind == "logout" and connected)))
            only_exit = kind == "appquit"
            is_busy = ui["busy"] == kind
            flashing = bool(okf and okf["kind"] == kind
                            and time.monotonic() < okf["until"])
            hv = ui.get("hover") == kind
            pr = ui.get("pressed") == kind
            if flashing:
                bg = COLOR_STATUS_OK
                text = "✓ 已连接" if kind == "login" else "✓ 已断开"
                tcol = (255, 255, 255)
            elif is_busy:
                bg = tuple(max(0, x-14) for x in col)
                text = label
                tcol = (255, 255, 255)
            elif enabled or only_exit:
                if pr:
                    bg = pr_c
                elif hv:
                    bg = hv_c
                else:
                    bg = col
                text = label
                tcol = (255, 255, 255)
            else:
                bg = (232, 232, 237)
                text = label
                tcol = (184, 184, 184)
            _round_rect(d, RIGHT_X*S, by0*S, (RIGHT_X+RIGHT_BTN_W)*S,
                        (by0+RIGHT_BTN_H)*S, 9*S, fill=bg, outline=bg)
            if is_busy:
                ang = ui["frame"] % 360
                rc = (RIGHT_X+22)*S, (by0+RIGHT_BTN_H//2)*S
                d.arc([rc[0]-7*S, rc[1]-7*S, rc[0]+7*S, rc[1]+7*S],
                      start=ang, end=ang+270, fill=(255, 255, 255),
                      width=max(1, S))
                _text_c(d, (RIGHT_X+46)*S, (by0+RIGHT_BTN_H//2)*S, text,
                        f_btn, tcol)
            else:
                _text_c(d, (RIGHT_X+RIGHT_BTN_W/2)*S,
                        (by0+RIGHT_BTN_H/2)*S, text, f_btn, tcol)

        # ⑥ 左栏底部：修改账号密码（与右栏「退出」同底对齐, 宽与左栏协调）
        cfg_w, cfg_h = int(172*SCALE), BOT_H
        bxx = LEFT_X
        bby = BOT_Y
        hv2 = ui.get("hover") == "config"
        pr2 = ui.get("pressed") == "config"
        bg = COLOR_BTN_SECOND
        if hv2:
            bg = (220, 220, 227)
        if pr2:
            bg = (201, 201, 209)
        _round_rect(d, bxx*S, bby*S, (bxx+cfg_w)*S, (bby+cfg_h)*S, 10*S,
                    fill=bg, outline=(225, 225, 232))
        _text_c(d, (bxx+cfg_w/2)*S, (bby+cfg_h/2)*S, "修改账号密码", f_btn,
                COLOR_TEXT_PRIMARY)

        # Toast（右下）
        if ui["toast"]:
            txt, until, col = ui["toast"]
            if time.monotonic() < until:
                tw = d.textlength(txt, font=f_sub)
                tx = W*S - 16*S - tw - 20*S
                _round_rect(d, tx, (BOT_Y-28)*S, W*S-16*S, (BOT_Y-8)*S, 8*S,
                            fill=(40, 42, 52, 235))
                d.text((tx+10*S, (BOT_Y-24)*S), txt, font=f_sub, fill=(255,
                                                                       255, 255))

        # 确认弹层
        if ui["confirm"]:
            ov = Image.new("RGBA", (int(W*S), int(H*S)), (8, 8, 14, 150))
            im.alpha_composite(ov)
            d = ImageDraw.Draw(im)
            bw, bh = 300*S, 132*S
            bx = (W*S-bw)/2
            by = (H*S-bh)/2
            _round_rect(d, bx, by, bx+bw, by+bh, 12*S, fill=P["card_bg"],
                        outline=P["card_out"])
            is_lg = ui["confirm"] == "logout"
            title = "断开校园网会话" if is_lg else "退出网通客户端"
            msg = ("注销将下线当前账号，软件继续运行。"
                   if is_lg else "退出软件不会自动注销校园网账号，会话将在线残留，建议先注销再退出。")
            d.text((bx+16*S, by+14*S), title, font=f_status,
                   fill=P["text_main"])
            d.text((bx+16*S, by+48*S), msg, font=f_lbl, fill=P["text_sub"])
            for cxx, ctt, ok2 in ((bx+16*S, "取消", False),
                                  (bx+bw-104*S,
                                   "确认断开" if is_lg else "确认退出", True)):
                hv = ui.get("hover") == ("cfm_yes" if ok2 else "cfm_no")
                bg = COLOR_BTN_DANGER if ok2 else COLOR_BTN_SECOND
                if hv:
                    bg = tuple(max(0, x-15) for x in bg)
                _round_rect(d, cxx, by+bh-42*S, cxx+88*S, by+bh-14*S, 8*S,
                            fill=bg,
                            outline=COLOR_BTN_DANGER if ok2
                            else P["btn_out"])
                _text_c(d, cxx+44*S, by+bh-28*S, ctt, f_btn,
                        (255, 255, 255) if ok2 else P["btn_text"])
        # 缩小到显示尺寸后，重新施加"卡片外=纯洋红"掩码，消除 LANCZOS 缩放造成的洋红毛边
        # （只影响主窗口弹出卡片；确认弹层遮罩也在卡片内，不会误抠）
        im = im.resize((pw, ph), Image.LANCZOS)
        _msk = Image.new("L", (int(W*S), int(H*S)), 0)
        _md = ImageDraw.Draw(_msk)
        _md.rounded_rectangle([2, 2, W*S-2, H*S-2], radius=12*S, fill=255)
        _msk = _msk.resize((pw, ph), Image.LANCZOS).point(lambda p: 255 if p >= 128 else 0)
        _bg = Image.new("RGBA", (pw, ph), (255, 0, 254, 255))
        im = Image.composite(im, _bg, _msk)
        return im

    # ---- 交互 ----
    ph_img = [None]
    img_id = [None]

    def snapshot():
        rt = read_runtime()
        cfg_now = get_cfg()
        return (rt.get("state", "connected"), daemon_state_read(),
                rt.get("ip", ""), rt.get("theme", "light"),
                cfg_now.get("username", "") or "未配置")

    def redraw():
        st, on, ip, theme, username_now = snapshot()
        aon = ui.get("autostart", False)   # 用缓存，避免每次刷新查注册表
        img = render(st, on, ip, theme, st == "connected", aon, username_now)
        ph_img[0] = ImageTk.PhotoImage(img)
        c.itemconfig(img_id[0], image=ph_img[0])

    def hit(x, y):
        x = int(x / dpi); y = int(y / dpi)
        if ui["confirm"]:
            bw, bh = 300, 132
            bx = (W-bw)/2; by = (H-bh)/2
            if by+bh-42 <= y <= by+bh-14:
                if bx+16 <= x <= bx+104:
                    return "cfm_no"
                if bx+bw-104 <= x <= bx+bw-16:
                    return "cfm_yes"
            return None
        # 顶栏右上: 主题|关闭 (圆心 close_cx=W-36, theme_cx=W-92, 半径20)
        if 0 <= y < TITLE_H:
            if W-92-20 <= x <= W-92+20:
                return "theme"
            if W-36-20 <= x <= W-36+20:
                return "close"
        # 左开关（sy0 = 账号区下方）
        sy = LEFT_START_Y + int(8*SCALE)
        ay0 = sy + int(36*SCALE)
        sy0 = ay0 + 2*int(28*SCALE) + int(24*SCALE)
        if LEFT_X <= x <= LEFT_X+int(176*SCALE):
            if sy0 <= y <= sy0+int(26*SCALE):
                return "guard"
            if sy0+int(44*SCALE) <= y <= sy0+int(44*SCALE)+int(26*SCALE):
                return "autostart"
        # 右竖排 连接/注销/退出
        if RIGHT_X <= x <= RIGHT_X+RIGHT_BTN_W:
            bot_bottom = BOT_Y + BOT_H
            bye = bot_bottom - RIGHT_BTN_H
            byl = bye - RIGHT_BTN_H - RIGHT_BTN_SPACING
            byc = byl - RIGHT_BTN_H - RIGHT_BTN_SPACING
            if byc <= y <= byc+RIGHT_BTN_H:
                return "login"
            if byl <= y <= byl+RIGHT_BTN_H:
                return "logout"
            if bye <= y <= bye+RIGHT_BTN_H:
                return "appquit"
        # 底部 修改账号密码
        cfg_w, cfg_h = int(172*SCALE), BOT_H
        if LEFT_X <= x <= LEFT_X+cfg_w and BOT_Y <= y <= BOT_Y+cfg_h:
            return "config"
        return None

    def popup_exit():
        """popup 统一退出：清理 HWND + 销毁窗口 + 退出进程。"""
        try:
            remove_popup_hwnd()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        os._exit(0)

    def do_quit_app():
        request_app_quit()      # 置 app_quit：daemon 收到后退出；弹窗自身退出
        popup_exit()

    def net_worker(action):
        """后台网络操作。保证任何时候都写入 last_net，避免 busy 转圈卡死。"""
        c2 = get_cfg()
        u = c2.get("username", "")
        ok, err = False, ""
        pw = None
        t0 = time.monotonic()
        try:
            pw = resolve_password(c2)
            cli = DrcomClient(c2)
            if action == "login":
                ok = bool(u and pw) and cli.login_with_retry(u, pw)
            else:
                ok = cli.logout()
        except Exception as e:
            err = str(e)
        if time.monotonic()-t0 > 10 and not ok and not err:
            err = "请求超时，请检查校园网链路"
        try:
            if action == "login":
                manual_logout_write(False)          # 用户点「连接」= 想保持在线，允许后台重连
                if ok:
                    try:
                        ip = DrcomClient(get_cfg()).status_info().get("ip", "")
                    except Exception:
                        ip = ""
                    daemon_write_state("connected", ip)
                else:
                    daemon_write_state("disconnected", "")
            else:
                if ok:
                    manual_logout_write(True)       # 用户主动注销 = 禁止 daemon 自动重连
                daemon_write_state("disconnected", "")
        except Exception as e:
            if not err:
                err = str(e)
        try:
            rt = read_runtime()
            rt["last_net"] = {"action": action, "ok": ok, "err": err,
                              "ts": time.monotonic()}
            write_runtime(rt)
        except Exception:
            pass

    def begin_busy(kind):
        ui["busy"] = kind
        ui["confirm"] = None
        ui["toast"] = None
        redraw()
        anim_on[0] = True          # 启动动画轮询, 否则 busy 永远无法清除
        threading.Thread(target=net_worker, args=(kind,), daemon=True).start()
        root.after(150, anim_tick) # 每150ms轮询 last_net, 检测注销完成

    anim_on = [False]

    def _breath():
        return (int(time.monotonic() * 1000) // 900) % 2 == 0

    def anim_tick():
        ui["frame"] += 18
        ui["flash"] = _breath()
        rt = read_runtime()
        res = rt.get("last_net")
        if ui["busy"] and res and time.monotonic()-res.get("ts", 0) < 0.5:
            if res.get("action") == ui["busy"]:
                ui["busy"] = None
                if res.get("ok"):
                    ui["ok_flash"] = {"kind": res["action"],
                                      "until": time.monotonic()+0.25}
                else:
                    show_toast(res.get("err") or "操作失败，请稍后重试",
                               COLOR_BTN_DANGER if res.get("err")
                               else (235, 200, 90))
                rt.pop("last_net", None)
                write_runtime(rt)
                redraw()
                if ui["ok_flash"]:
                    root.after(120, anim_tick)
                else:
                    anim_on[0] = False
                return
        if ui["busy"]:
            redraw()
            root.after(150, anim_tick)
        elif ui["ok_flash"] and time.monotonic() >= ui["ok_flash"]["until"]:
            ui["ok_flash"] = None
            anim_on[0] = False
            redraw()
        else:
            anim_on[0] = False

    def on_press(e):
        act = hit(e.x, e.y)
        if act:
            ui["pressed"] = act
            redraw()

    def on_release(e):
        ui["pressed"] = None
        redraw()

    def click(e):
        act = hit(e.x, e.y)
        # 确认弹层交互始终允许（即使后台 busy），避免 busy 中 UI 卡死
        if ui["confirm"]:
            if act == "cfm_no":
                ui["confirm"] = None
            elif act == "cfm_yes":
                kind = ui["confirm"]
                ui["confirm"] = None
                if kind == "quit":
                    do_quit_app()
                else:
                    begin_busy("logout")
            redraw()
            return
        if ui["busy"]:
            return
        if act == "config":
            if is_init_running():
                log("[POPUP] 配置窗口已存在，忽略")
                return
            try:
                config_editing_write(True)     # 打开配置时暂停 daemon 自动认证
                p = subprocess.Popen([_exe_path(), "init"], cwd=DIR,
                                     creationflags=CREATE_NO_WINDOW)
                log(f"[POPUP] 已启动修改账号密码窗口 PID={p.pid}")
            except Exception as e:
                config_editing_write(False)    # 启动失败则清除编辑状态
                log(f"[POPUP][FAIL] 启动修改账号密码失败: {type(e).__name__}: {e}")
            return
        elif act == "close":
            # 关闭界面但不退出进程：隐藏窗口，托盘仍在；下次左键托盘立即显示（免去 onefile 再解压的延迟）
            root.withdraw()
        elif act == "theme":
            theme_write("dark" if theme_read() == "light" else "light")
            redraw()
        elif act == "guard":
            daemon_state_write(not daemon_state_read()); redraw()
        elif act == "autostart":
            try:
                new_value = not ui.get("autostart", False)
                if set_autostart(new_value):
                    ui["autostart"] = new_value
                    log(f"[INFO] 开机自启已切换: {new_value}")
            except Exception:
                pass
            redraw()
        elif act in ("login", "logout"):
            st = snapshot()[0]
            if act == "login" and st == "connected":
                return
            if act == "logout" and st != "connected":
                return
            if act == "logout":
                ui["confirm"] = "logout"; redraw()
            else:
                begin_busy("login")
        elif act == "appquit":
            ui["confirm"] = "quit"; redraw()

    def hover_move(e):
        x, y = int(e.x / dpi), int(e.y / dpi)
        h = hit(x, y)
        if ui["confirm"]:
            h = None
        if h != ui["hover"]:
            ui["hover"] = h
            redraw()

    def tick():
        # popup 是独立进程，只响应「用户真正退出」(app_quit)。daemon 的正常退出/崩溃会
        # 终止守护但不再污染此标记，所以注销/连接等操作不会误杀弹窗。
        # 此外响应「托盘请求重新显示」(popup.show)，让 popup 自行 deiconify，而非外部强改窗口。
        try:
            # 程序真正退出
            if app_quit_requested():
                popup_exit()
                return

            # 托盘要求重新显示（× 隐藏后由托盘写 popup.show 标记）
            if popup_show_requested():
                clear_popup_show_request()
                log("[POPUP] 收到托盘显示请求，自行恢复窗口")
                root.deiconify()
                root.update_idletasks()
                root.lift()
                # 临时置顶，确保能抢到前台
                root.attributes("-topmost", True)
                def remove_topmost():
                    try:
                        root.attributes("-topmost", False)
                    except Exception:
                        pass
                root.after(150, remove_topmost)
                redraw()

            # 被 × 隐藏：只做低占用轮询，等待 app_quit / popup.show 请求
            if not root.winfo_viewable():
                root.after(200, tick)
                return

            rt = read_runtime()
            if rt.get("app_quit"):
                popup_exit()
                return
            ui["flash"] = _breath()
            if not anim_on[0]:
                redraw()
            root.after(200, tick)
        except Exception as e:
            log(f"[POPUP] tick error: {type(e).__name__}: {e}")
            root.after(200, tick)

    img_id[0] = c.create_image(0, 0, anchor="nw")
    # 只绑 <Button-1> 到 canvas（避免双重绑定触发两次）
    c.bind("<Button-1>", click)
    c.bind("<Motion>", hover_move)
    root.bind("<Escape>", lambda e: root.withdraw())
    redraw()
    root.after(200, tick)
    root.mainloop()
    popup_exit()
    return 0

def cmd_init() -> int:
    """设置窗口（整卡 PIL 渲染 + 原生输入框）。可由 daemon 内嵌调用。"""
    global _saved_ok, _INIT_EMBEDDED
    _saved_ok = False
    if not _INIT_EMBEDDED and not acquire_singleton(MUTEX_INIT):
        log("[INIT] 已有设置窗口在运行，忽略")
        return 0
    import tkinter as tk
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    cfg_now = load_config()
    is_edit = bool(cfg_now.get("username"))
    cur_user = cfg_now.get("username", "")
    cur_sfx = cfg_now.get("suffix", "")
    cur_op = "lt"
    for op in OPERATORS:
        if op["suffix"] == cur_sfx:
            cur_op = op["id"]
            break
    theme = theme_read()
    P = palette(theme)
    card = P["card_bg"] if theme == "light" else (38, 38, 44, 255)
    out_c = P["card_out"]
    txt = P["text_main"]
    sub = P["text_sub"]

    W, H = 420, 460
    X0, X1 = 24, W - 24
    IN_H = 38
    dpi = get_dpi_scale()
    SS = max(2, int(round(2 * dpi)))
    pw, ph = int(W * dpi), int(H * dpi)

    Y_LBL1, Y_IN1 = 76, 100
    Y_LBL2, Y_IN2 = 160, 184
    Y_HINT = 234
    Y_OP_LBL, Y_OP = 262, 300
    Y_BTN = 388
    EYE_CX = X1 - 22
    EYE_CY = Y_IN2 + IN_H // 2
    EYE_R = 8

    state = {"op": cur_op, "show_pw": False, "err": "", "status": "", "pressed": None}

    root = tk.Tk()
    root.report_callback_exception = _tk_crash_exc
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    try:
        root.attributes("-transparentcolor", "#FF00FE")
    except Exception:
        pass
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{pw}x{ph}+{max(0,(sw-pw)//2)}+{max(0,(sh-ph)//2)}")
    c = tk.Canvas(root, width=pw, height=ph, bg="#FF00FE",
                  highlightthickness=0, bd=0)
    c.pack()
    try:
        root.update_idletasks()   # 让 overrideredirect 窗口真正应用 geometry 并显示
        root.update()
    except Exception:
        pass
    log("[INIT] 配置窗口已创建")

    f_emoji, f_bold, f_title, f_txt, f_sub, f_btn, f_x = _load_fonts(SS)
    ph_img = [None]
    img_id = [None]

    def eye_icon(d, cx, cy, r, color):
        S = SS; w = max(1, S)
        d.arc([cx-r, cy-r*0.7, cx+r, cy+r*0.7], start=180, end=360, fill=color, width=w)
        d.arc([cx-r, cy-r*0.7, cx+r, cy+r*0.7], start=0, end=180, fill=color, width=w)
        pr = r * 0.35
        d.ellipse([cx-pr, cy-pr, cx+pr, cy+pr], fill=color)

    def draw():
        S = SS
        im = Image.new("RGBA", (int(W * S), int(H * S)), (255, 0, 254, 255))
        d = ImageDraw.Draw(im)
        _round_rect(d, 2, 2, W * S - 2, H * S - 2, 18 * S,
                    fill=card, outline=out_c, width=max(2, S))
        # 标题
        d.text((24 * S, 14 * S), "\U0001F350", font=f_emoji,
               embedded_color=True)
        d.text((52 * S, 14 * S), "网通", font=f_title, fill=txt)
        rt = "修改设置" if is_edit else "首次配置"
        tw = d.textlength(rt, font=f_sub)
        d.text(((W - 20) * S - tw, 22 * S), rt, font=f_sub,
               fill=(120, 120, 128, 255))
        # 输入框白底
        for yy in (Y_IN1, Y_IN2):
            _round_rect(d, X0 * S, yy * S, X1 * S, (yy + IN_H) * S, 6 * S,
                        fill=(255, 255, 255), outline=(211, 211, 216))
        # 标签
        d.text((X0 * S, (Y_LBL1 + 2) * S), "校园网账号", font=f_lbl0,
               fill=sub)
        d.text((X0 * S, (Y_LBL2 + 2) * S), "校园网密码", font=f_lbl0,
               fill=sub)
        # 眼睛
        eye_icon(d, EYE_CX * S, EYE_CY * S, EYE_R * S,
                 (100, 100, 108) if not state["show_pw"] else C_BLUE)
        if not state["show_pw"]:
            w = max(1, S)
            d.line([(EYE_CX - 5) * S, (EYE_CY + 5) * S,
                    (EYE_CX + 5) * S, (EYE_CY - 5) * S],
                   fill=C_PINK, width=w)
        hint = "点击图标临时查看密码，密码在本机加密存储，不会上传"
        d.text((X0 * S, Y_HINT * S), hint, font=f_sub,
               fill=(150, 150, 158))
        # 运营商标题 + 分段
        d.text((X0 * S, (Y_OP_LBL + 2) * S), "运营商", font=f_lbl0,
               fill=sub)
        n = len(OPERATORS)
        seg_w = ((X1 - X0) * S - 4 * S * (n - 1)) / n
        for i, op in enumerate(OPERATORS):
            x0 = X0 * S + i * (seg_w + 4 * S)
            sel = op["id"] == state["op"]
            hv = state.get("hover") == f"op_{i}"
            if sel:
                bg = C_BLUE
                if hv:
                    bg = tuple(max(0, x-15) for x in bg)
                _round_rect(d, x0, Y_OP * S, x0 + seg_w, (Y_OP + 38) * S,
                            6 * S, fill=bg, outline=bg)
                _text_c(d, x0 + seg_w / 2, (Y_OP + 19) * S, op["name"],
                        f_btn, (255, 255, 255))
            else:
                bg = (255, 255, 255)
                if hv:
                    bg = (245, 245, 247)
                _round_rect(d, x0, Y_OP * S, x0 + seg_w, (Y_OP + 38) * S,
                            6 * S, fill=bg,
                            outline=(210, 210, 216))
                _text_c(d, x0 + seg_w / 2, (Y_OP + 19) * S, op["name"],
                        f_btn, (70, 70, 76))
        # 消息
        msg = state["err"] or state["status"]
        if msg:
            d.text((X0 * S, 348 * S), msg, font=f_sub,
                   fill=C_PINK if state["err"] else C_GREEN)
        # 按钮：取消 | 保存
        can = bool(entry_user.get().strip()) and bool(entry_pass.get())
        btn_w = 110
        # 取消按钮
        hv_cancel = state.get("hover") == "cancel"
        pr_cancel = state.get("pressed") == "cancel"
        bg_cancel = P["btn_hover"] if hv_cancel else P["btn_bg"]
        if pr_cancel:
            bg_cancel = tuple(max(0, x-15) for x in bg_cancel)
        _round_rect(d, X0 * S, Y_BTN * S, (X0 + btn_w) * S,
                    (Y_BTN + 40) * S, 8 * S, fill=bg_cancel,
                    outline=P["btn_out"])
        _text_c(d, (X0 + btn_w / 2) * S, (Y_BTN + 20) * S, "取消",
                f_btn, P["btn_text"])
        # 保存按钮
        hv_save = state.get("hover") == "save"
        pr_save = state.get("pressed") == "save"
        if can:
            bg_save = C_BLUE
            if pr_save:
                bg_save = tuple(max(0, x-30) for x in bg_save)
            elif hv_save:
                bg_save = tuple(max(0, x-15) for x in bg_save)
        else:
            bg_save = (200, 208, 216)
        _round_rect(d, (X1 - btn_w) * S, Y_BTN * S, X1 * S,
                    (Y_BTN + 40) * S, 8 * S,
                    fill=bg_save, outline=bg_save)
        _text_c(d, (X1 - btn_w / 2) * S, (Y_BTN + 20) * S, "保存",
                f_btn, (255, 255, 255))

        im = im.resize((pw, ph), Image.LANCZOS)
        _msk = Image.new("L", (int(W * S), int(H * S)), 0)
        _md = ImageDraw.Draw(_msk)
        _md.rounded_rectangle([2, 2, W * S - 2, H * S - 2], radius=18 * S, fill=255)
        _msk = _msk.resize((pw, ph), Image.LANCZOS).point(lambda p: 255 if p >= 128 else 0)
        _bg = Image.new("RGBA", (pw, ph), (255, 0, 254, 255))
        im = Image.composite(im, _bg, _msk)
        ph_img[0] = ImageTk.PhotoImage(im)
        c.itemconfig(img_id[0], image=ph_img[0])
        c.coords(win_u[0], (X0 + X1) / 2 * dpi,
                 (Y_IN1 + IN_H / 2) * dpi)
        c.coords(win_p[0], (X0 + EYE_CX - 12) / 2 * dpi,
                 (Y_IN2 + IN_H / 2) * dpi)
        entry_user.configure(width=max(8, int((X1 - X0 - 12) / 11)))
        entry_pass.configure(width=max(8, int((EYE_CX - 12 - X0 - 12) / 11)))

    entry_user = tk.Entry(root, font=("Microsoft YaHei", 12), bd=0,
                          relief="flat", bg="#ffffff", fg="#1a1a1a",
                          insertbackground="#1a1a1a", highlightthickness=0)
    entry_pass = tk.Entry(root, font=("Microsoft YaHei", 12), bd=0,
                          relief="flat", bg="#ffffff", fg="#1a1a1a",
                          insertbackground="#1a1a1a", show="*",
                          highlightthickness=0)
    win_u = [c.create_window(0, 0, window=entry_user, anchor="center")]
    win_p = [c.create_window(0, 0, window=entry_pass, anchor="center")]
    img_id[0] = c.create_image(0, 0, anchor="nw")
    if cur_user:
        entry_user.insert(0, cur_user)
    f_lbl0 = f_txt

    def do_save():
        u = entry_user.get().strip()
        pw = entry_pass.get()
        err = validate_account(u, pw)
        if err:
            state["err"] = err; state["status"] = ""; draw(); return
        op_sfx = ""
        for op in OPERATORS:
            if op["id"] == state["op"]:
                op_sfx = op["suffix"]; break
        if not cred_save(u + op_sfx, pw):
            state["err"] = "密码写入失败"; state["status"] = ""; draw(); return
        cf = load_config()
        cf["username"] = u; cf["suffix"] = op_sfx
        cf.pop("password", None); save_config(cf)
        global _saved_ok
        _saved_ok = True
        state["err"] = ""
        state["status"] = "配置已保存 ✓"
        draw()
        root.after(800, teardown)

    def teardown(*_):
        saved = _saved_ok
        try:
            config_editing_write(False)   # 无论保存/取消/ESC 都清除"配置编辑中"，让 daemon 恢复
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        if saved:
            log("[INIT] 配置保存成功")
            if not _INIT_EMBEDDED:
                # 首次安装：没有 daemon 则拉起 daemon；popup 若不存在（可能曾退出）也重新拉起，
                # 保证主界面出现。不重启已有 daemon（其会自动热加载新配置）。
                if not is_daemon_running():
                    # 开机自启即自动连接：不再强制关闭后台守护（daemon_state_read 默认 True），
                    # 用户手动关闭后 runtime 会保留 False，此处不覆盖其选择。
                    launch_windowed(["daemon"])
                # 统一用 launch_and_activate_popup 确保主界面进程存在并显示（替代原"1.2s 等 popup"）
                launch_and_activate_popup(timeout=5.0)
        if not _INIT_EMBEDDED:
            log("[INIT] 独立配置窗口退出，保留现有 daemon/popup")
            os._exit(0)

    # 接管窗口关闭事件（Alt+F4/任务栏/系统关闭），确保走 teardown 清理 config_editing
    try:
        root.protocol("WM_DELETE_WINDOW", teardown)
    except Exception:
        pass

    def hit_init(x, y):
        if abs(x - EYE_CX) <= 16 and abs(y - EYE_CY) <= 17:
            return "eye"
        n = len(OPERATORS)
        seg_w = (X1 - X0) / n
        if Y_OP <= y <= Y_OP + 38 and X0 <= x <= X1:
            idx = int((x - X0) // seg_w)
            if 0 <= idx < n:
                return f"op_{idx}"
        if X0 <= x <= X0 + 110 and Y_BTN <= y <= Y_BTN + 40:
            return "cancel"
        if X1 - 110 <= x <= X1 and Y_BTN <= y <= Y_BTN + 40:
            return "save"
        return None

    def on_press_init(e):
        x, y = e.x / dpi, e.y / dpi
        act = hit_init(x, y)
        if act:
            state["pressed"] = act
            draw()

    def on_release_init(e):
        state["pressed"] = None
        draw()

    def click(e):
        x, y = e.x / dpi, e.y / dpi
        act = hit_init(x, y)
        if act == "eye":
            state["show_pw"] = not state["show_pw"]
            entry_pass.configure(show="" if state["show_pw"] else "*")
            draw()
            return
        if act and act.startswith("op_"):
            idx = int(act.split("_")[1])
            if 0 <= idx < len(OPERATORS):
                state["op"] = OPERATORS[idx]["id"]
                draw()
            return
        if act == "cancel":
            teardown()
            return
        if act == "save" and entry_user.get().strip() and entry_pass.get():
            do_save()

    def hover_init(e):
        x, y = e.x / dpi, e.y / dpi
        h = hit_init(x, y)
        if h != state.get("hover"):
            state["hover"] = h
            draw()

    def keys(*_):
        draw()

    entry_user.bind("<KeyRelease>", keys)
    entry_pass.bind("<KeyRelease>", keys)
    entry_pass.bind("<FocusOut>", lambda e: (state.update(show_pw=False),
                                             entry_pass.configure(show="*"),
                                             draw()))
    # 只绑 <Button-1> 到 canvas（避免 c+root 双重绑定导致 click 触发两次翻转回去）
    c.bind("<Button-1>", click)
    c.bind("<Motion>", hover_init)
    root.bind("<Return>", lambda e: do_save())
    root.bind("<Escape>", lambda e: teardown())
    draw()
    entry_user.focus_set()
    root.mainloop()
    if not _INIT_EMBEDDED:
        os._exit(0)
    return 0 if _saved_ok else 1
# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════
COMMANDS = {
    "login": cmd_login,
    "daemon": cmd_daemon,
    "config": cmd_config,
    "status": cmd_status,
    "autostart": cmd_autostart,
    "diagnostic": cmd_diagnostic,
    "popup": cmd_popup,
    "init": cmd_init,
    "quit": cmd_quit,
}
def _cleanup_orphan_tmp() -> None:
    """清理异常退出遗留的 runtime.json.*.tmp / config.json.*.tmp 孤儿文件。"""
    try:
        if not os.path.isdir(APP_DATA_DIR):
            return
        for f in os.listdir(APP_DATA_DIR):
            if (f.startswith("runtime.json.") or f.startswith("config.json.")) and f.endswith(".tmp"):
                try:
                    os.remove(os.path.join(APP_DATA_DIR, f))
                except OSError:
                    pass
    except Exception:
        pass

def _setup_crash_logging() -> None:
    """把未捕获异常写入 campus_login.log，便于定位崩溃（主线程/工作线程/Tk 回调）。"""
    import traceback
    def _hook(exc_type, exc, tb):
        try:
            log("[CRASH] %s: %s" % (exc_type.__name__, exc))
            log("".join(traceback.format_tb(tb)))
        except Exception:
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
    sys.excepthook = _hook
    try:
        def _thook(args):
            try:
                log("[CRASH][thread] %s: %s" % (args.exc_type.__name__, args.exc_value))
                log("".join(traceback.format_tb(args.exc_traceback)))
            except Exception:
                pass
        threading.excepthook = _thook
    except AttributeError:
        pass
def _tk_crash_exc(exc, val, tb):
    import traceback
    try:
        log("[CRASH][tk] %s: %s" % (exc.__name__, val))
        log("".join(traceback.format_tb(tb)))
    except Exception:
        pass

def _migrate_old_data() -> None:
    """老版本把配置/运行时数据写在安装目录；onedir 后迁到 %LOCALAPPDATA%\Liwangtong。"""
    try:
        import shutil
        for name in ("config.json", "runtime.json"):
            src = os.path.join(DIR, name)
            dst = os.path.join(APP_DATA_DIR, name)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                    log(f"[MIGRATE] 已迁移 {name} -> {dst}")
                except Exception:
                    pass
    except Exception:
        pass

def main() -> int:
    _setup_crash_logging()
    _cleanup_orphan_tmp()
    _migrate_old_data()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    # 未配置账号时 login 默认进初始化
    if cmd == "login" and not load_config().get("username"):
        cmd = "init"
    # 双击/默认启动 + 已配置账号：这就是"打开程序"——清掉可能残留的退出标记，
    # 确保 daemon(托盘) 在跑，并弹出主界面窗口，避免只静默登录而无界面。
    if cmd == "login" and load_config().get("username"):
        try:
            rt = read_runtime()
            if rt.get("app_quit"):
                rt["app_quit"] = False
                write_runtime(rt)
        except Exception:
            pass
        if not is_daemon_running():
            launch_windowed(["daemon"])
        launch_and_activate_popup(timeout=5.0)
        return 0
    if cmd in COMMANDS:
        return COMMANDS[cmd]()
    print(f"{APP_DISPLAY} - 校园网自动登录")
    print("用法: 梨网通.exe <command>")
    print("  login / daemon / config / status / autostart / init / popup")
    return 1
if __name__ == "__main__":
    sys.exit(main())
