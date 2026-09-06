; TickerView 二期安装器(Inno Setup 6)
; 编译:  ISCC.exe installer\TickerView.iss   (或跑 repo 根的 build.ps1)
; 重要:  .iss 内的相对路径都相对本文件所在目录(installer\)解析,故 repo 根的东西要加 ..\
; 本地化:默认英文界面。要中文,从 Inno 官网下载 ChineseSimplified.isl 放到 compiler:\Languages\
;         并启用下面 [Languages] 里被注释的那行。

[Setup]
AppName=TickerView
AppVersion=0.2.0
AppVerName=TickerView 0.2.0
AppPublisher=TickerView
DefaultDirName={localappdata}\Programs\TickerView
DefaultGroupName=TickerView
DisableProgramGroupPage=yes
; 当前用户安装,免 UAC
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 64 位(x64 兼容所有 Inno 6)
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
; 输出(相对 installer\ → installer\Output)
OutputDir=Output
OutputBaseFilename=TickerView-Setup-0.2.0
SetupIconFile=..\alphaprism\planner\assets\tray_icon.ico
UninstallDisplayIcon={app}\TickerView.exe
UninstallDisplayName=TickerView
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional tasks:"

[Files]
; PyInstaller onedir 产物(repo 根的 dist\,相对 installer\ 要加 ..\)
Source: "..\dist\TickerView\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; WebView2 Evergreen 运行时兜底(可选):把官方 bootstrapper 放 installer\ 下即启用;缺文件也不报错
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

[Icons]
Name: "{group}\TickerView"; Filename: "{app}\TickerView.exe"; IconFilename: "{app}\TickerView.exe"
Name: "{group}\Uninstall TickerView"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TickerView"; Filename: "{app}\TickerView.exe"; IconFilename: "{app}\TickerView.exe"; Tasks: desktopicon

[Run]
; 仅当"缺 WebView2 且 bootstrapper 已随包"时才静默安装
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing WebView2 runtime..."; Check: NeedsWebView2Setup; Flags: waituntilterminated
Filename: "{app}\TickerView.exe"; Description: "Launch TickerView"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载只删程序目录;用户配置/数据(%APPDATA%\TickerView)刻意保留,便于重装即用。
; 如需"卸载即清空数据",取消下一行注释:
; Type: filesandordirs; Name: "{userappdata}\TickerView"

[Code]
function WebView2Installed: Boolean;
begin
  Result :=
    RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') or
    RegKeyExists(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
end;

// 需要装(缺运行时)且 bootstrapper 确实已复制到 {tmp} 时才执行
function NeedsWebView2Setup: Boolean;
begin
  Result := (not WebView2Installed) and FileExists(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'));
end;
