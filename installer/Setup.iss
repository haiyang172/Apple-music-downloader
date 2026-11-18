#ifndef AppVersion
  #define AppVersion "0.3.38"
#endif
#ifndef PayloadDir
  #define PayloadDir "dist\amdl"
#endif
#define StudioAppId "amdl.desktop"
#define StudioAppName "影音下载台"
#define StudioExecutable "MediaDownloadDesk.exe"

[Setup]
AppId={{74870839-527C-4C3D-A869-AB8A60913829}
AppName={#StudioAppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\amdl
DefaultGroupName={#StudioAppName}
DisableProgramGroupPage=yes
DisableStartupPrompt=yes
UninstallDisplayIcon={app}\{#StudioExecutable}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19041
OutputDir=dist
OutputBaseFilename=MediaDownloadDesk-Setup
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern dynamic
UsePreviousTasks=not IsExtractMode
UsePreviousAppDir=not IsExtractMode
UsePreviousGroup=not IsExtractMode
Uninstallable=not IsExtractMode
CreateUninstallRegKey=not IsExtractMode
#if FileExists(PayloadDir + "\assets\AMDLStudio.ico")
SetupIconFile={#PayloadDir}\assets\AMDLStudio.ico
#endif
#if FileExists(PayloadDir + "\assets\AMDLStudio.png")
WizardSmallImageFile={#PayloadDir}\assets\AMDLStudio.png
#endif

[Languages]
Name: "zhcn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{autoprograms}\amdl.lnk"; Check: not IsExtractMode
Type: files; Name: "{autodesktop}\amdl.lnk"; Check: not IsExtractMode
Type: files; Name: "{autoprograms}\AMDL Studio.lnk"; Check: not IsExtractMode
Type: files; Name: "{autodesktop}\AMDL Studio.lnk"; Check: not IsExtractMode
Type: files; Name: "{app}\AMDLStudio.exe"; Check: not IsExtractMode

[Icons]
Name: "{autoprograms}\{#StudioAppName}"; Filename: "{app}\{#StudioExecutable}"; WorkingDir: "{app}"; AppUserModelID: "{#StudioAppId}"; Check: not IsExtractMode
Name: "{autodesktop}\{#StudioAppName}"; Filename: "{app}\{#StudioExecutable}"; WorkingDir: "{app}"; AppUserModelID: "{#StudioAppId}"; Tasks: desktopicon; Check: not IsExtractMode

[Run]
Filename: "{app}\apple-web-login\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Parameters: "/silent /install"; Flags: runhidden waituntilterminated; Check: NeedsWebViewRuntime
Filename: "{app}\{#StudioExecutable}"; Description: "{cm:LaunchStudio}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Check: not IsExtractMode

[CustomMessages]
zhcn.DesktopIcon=创建桌面快捷方式
english.DesktopIcon=Create a desktop shortcut
zhcn.LaunchStudio=打开{#StudioAppName}
english.LaunchStudio=Open Media Download Desk

[Code]
function IsExtractMode: Boolean;
begin
  Result := ExpandConstant('{param:EXTRACTONLY|0}') = '1';
end;

function NeedsWebViewRuntime: Boolean;
var
  Version: String;
  Key: String;
begin
  Result := False;
  if IsExtractMode then
    Exit;
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  if RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0') then
    Exit;
  if RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0') then
    Exit;
  Result := True;
end;

function InitializeSetup: Boolean;
var
  Destination: String;
  Entry: TFindRec;
begin
  Result := True;
  if not IsExtractMode then
    Exit;
  Destination := ExpandConstant('{param:DIR|}');
  Result := WizardSilent and PathIsRooted(Destination) and DirExists(Destination);
  if Result and FindFirst(AddBackslash(Destination) + '*', Entry) then
  try
    repeat
      if (Entry.Name <> '.') and (Entry.Name <> '..') then
      begin
        Result := False;
        Break;
      end;
    until not FindNext(Entry);
  finally
    FindClose(Entry);
  end;
end;
