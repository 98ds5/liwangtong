# -*- mode: python ; coding: utf-8 -*-
# 打包配置：onedir（不要用 onefile，原因见 docs/BUILD.md）
# 在仓库根目录执行：pyinstaller campus_login.spec
import os
from PyInstaller.utils.hooks import collect_all

ROOT = SPECPATH

datas = []
binaries = []
hiddenimports = ['tkinter']

# pystray 和 PIL 都是动态 import，不 collect 全打不全
for pkg in ('pystray', 'PIL'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, 'src', 'campus_login.py')],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='梨网通',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 窗口程序，控制台一闪很丑
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'assets', 'app.ico'),
    manifest=os.path.join(ROOT, 'campus_login.manifest'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='梨网通',
)
