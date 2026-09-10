; Liwangtong installer script (Inno Setup 6)
; Build from project root: ISCC.exe installers\campus_installer.iss
; Paths below are relative to this script file

#define MyAppName "梨网通"
#define MyAppExe "梨网通.exe"
#define MyAppVersion "4.0.11"
#define MyAppPublisher "PearTech"
#define MyAppDir "{localappdata}\PearTech\Liwangtong"

[Setup]
AppId={{B3E5A7C9-8D41-4F1A-9E7C-5E21A6D1A901}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={#MyAppDir}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\output
OutputBaseFilename=LiWangTong-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter=梨网通.exe
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; onedir：安装整个程序目录（梨网通.exe + _internal 依赖 + 图标）
Source: "..\stage\梨网通\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\stage\campus_notify.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stage\campus_notify_err.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: ""; IconFilename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: ""; Tasks: desktopicon; IconFilename: "{app}\{#MyAppExe}"

[Run]
Filename: "{app}\{#MyAppExe}"; Parameters: "init"; Description: "配置并启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前先让运行中的 daemon/popup 退出，释放 exe 文件锁，避免遗留 exe
Filename: "{app}\{#MyAppExe}"; Parameters: "quit"; Flags: runhidden waituntilterminated; StatusMsg: "正在退出网通客户端…"; RunOnceId: "QuitLiwangtong"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
