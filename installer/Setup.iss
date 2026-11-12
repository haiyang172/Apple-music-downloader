#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "dist\AMDL-Studio"
#endif

[Setup]
AppId={{74870839-527C-4C3D-A869-AB8A60913829}
AppName=AMDL Studio
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\AMDL Studio
DefaultGroupName=AMDL Studio
DisableProgramGroupPage=yes
DisableStartupPrompt=yes
UninstallDisplayIcon={app}\AMDLStudio.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19041
OutputDir=dist
OutputBaseFilename=AMDL-Studio-Setup
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern dynamic
UsePreviousTasks=no

[Languages]
Name: "zhcn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked
Name: "enablewsl"; Description: "{cm:EnableWsl}"; Flags: unchecked; Check: CanOfferWsl

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AMDL Studio"; Filename: "{app}\AMDLStudio.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\AMDL Studio"; Filename: "{app}\AMDLStudio.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\AMDLStudio.exe"; Description: "{cm:LaunchStudio}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[CustomMessages]
zhcn.DesktopIcon=创建桌面快捷方式
english.DesktopIcon=Create a desktop shortcut
zhcn.EnableWsl=启用 Windows 的 WSL 1 组件（ALAC、Atmos 本地服务可选，需要管理员权限）
english.EnableWsl=Enable the Windows WSL 1 component for the optional ALAC and Atmos service (requires administrator permission)
zhcn.LaunchStudio=打开 AMDL Studio
english.LaunchStudio=Open AMDL Studio
zhcn.WslProgress=正在启用 Windows 的 WSL 1 组件…
english.WslProgress=Enabling the Windows WSL 1 component…
zhcn.WslDeclined=AMDL Studio 已安装。WSL 1 组件启用已取消，可在“启用或关闭 Windows 功能”中重试。
english.WslDeclined=AMDL Studio is installed. WSL 1 setup was cancelled. You can retry from Turn Windows features on or off.
zhcn.WslFailed=AMDL Studio 已安装。WSL 1 组件启用失败（退出代码 %1），可在“启用或关闭 Windows 功能”中重试。
english.WslFailed=AMDL Studio is installed. Enabling WSL 1 failed (exit code %1). You can retry from Turn Windows features on or off.
zhcn.WslRestart=WSL 1 组件已启用。请在方便时重启 Windows 以完成配置。
english.WslRestart=The WSL 1 component is enabled. Restart Windows when convenient to finish setup.

[Code]
const
  LxssKey = 'Software\Microsoft\Windows\CurrentVersion\Lxss';

function CanOfferWsl: Boolean;
var
  Entries: TArrayOfString;
  Index: Integer;
  Name: String;
begin
  Result := FileExists(ExpandConstant('{sys}\dism.exe'));
  if Result and RegGetSubkeyNames(HKCU, LxssKey, Entries) then
    for Index := 0 to GetArrayLength(Entries) - 1 do
      if RegQueryStringValue(HKCU, LxssKey + '\' + Entries[Index], 'DistributionName', Name) then
        if CompareText(Name, 'u22-amdl') = 0 then
        begin
          Result := False;
          Exit;
        end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
begin
  if (CurStep <> ssPostInstall) or not WizardIsTaskSelected('enablewsl') or WizardSilent then
    Exit;

  WizardForm.StatusLabel.Caption := CustomMessage('WslProgress');
  if not ShellExec('runas', ExpandConstant('{sys}\dism.exe'),
    '/online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart',
    '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    MsgBox(CustomMessage('WslDeclined'), mbInformation, MB_OK)
  else if ExitCode = 3010 then
    MsgBox(CustomMessage('WslRestart'), mbInformation, MB_OK)
  else if ExitCode <> 0 then
    MsgBox(FmtMessage(CustomMessage('WslFailed'), [IntToStr(ExitCode)]), mbInformation, MB_OK);
end;
