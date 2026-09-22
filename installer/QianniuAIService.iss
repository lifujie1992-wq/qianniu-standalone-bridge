#define AppName "淘宝千牛 AI 客服助手"
#define AppVersion "1.5.4"
#define AppPublisher "Qianniu AI Service"
#define SourceDir "..\delivery\淘宝千牛AI客服-安装版-1.5.4"

[Setup]
AppId={{C31C8BA7-A990-4B80-99D2-E69EF3AD91CA}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\QianniuAIService
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\delivery
OutputBaseFilename=淘宝千牛AI客服安装包-1.5.4
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UninstallDisplayIcon={app}\QianniuAgent.exe
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} 安装程序

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "closeclients"; Description: "安装时自动关闭旧客服助手和千牛（推荐）"; GroupDescription: "安装准备："; Flags: checkedonce
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked
Name: "autostart"; Description: "登录 Windows 后自动启动客服助手"; GroupDescription: "自动启动："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\启动客服助手"; Filename: "{app}\QianniuAgent.exe"
Name: "{group}\配置中心大脑"; Filename: "{app}\QianniuAgent.exe"; Parameters: "--configure"
Name: "{group}\检查在线更新"; Filename: "{app}\QianniuAgent.exe"; Parameters: "--update"
Name: "{group}\查看运行状态"; Filename: "{app}\QianniuAgent.exe"; Parameters: "--status"
Name: "{group}\彻底退出客服助手"; Filename: "{app}\QianniuAgent.exe"; Parameters: "--stop"
Name: "{group}\卸载客服助手"; Filename: "{uninstallexe}"
Name: "{autodesktop}\千牛 AI 客服助手"; Filename: "{app}\QianniuAgent.exe"; Tasks: desktopicon
Name: "{userstartup}\千牛 AI 客服助手"; Filename: "{app}\QianniuAgent.exe"; Tasks: autostart

[Run]
Filename: "{app}\QianniuAgent.exe"; Description: "启动客服助手控制中心"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\QianniuAgent.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopQianniuAIService"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\state"
Type: files; Name: "{app}\config.json"
Type: filesandordirs; Name: "{app}"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\QianniuAgent.exe')) then
    Exec(ExpandConstant('{app}\QianniuAgent.exe'), '--stop', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
  if WizardIsTaskSelected('closeclients') then begin
    if FileExists(ExpandConstant('{app}\QianniuAgent.exe')) then
      Exec(ExpandConstant('{app}\QianniuAgent.exe'), '--stop', '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode);
  end;
end;
