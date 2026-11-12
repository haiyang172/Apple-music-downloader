#ifndef AppVersion
  #define AppVersion "0.3.1"
#endif
#ifndef PayloadDir
  #define PayloadDir "dist\amdl"
#endif
#define StudioAppId "amdl.desktop"

[Setup]
AppId={{74870839-527C-4C3D-A869-AB8A60913829}
AppName=amdl
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\amdl
DefaultGroupName=amdl
DisableProgramGroupPage=yes
DisableStartupPrompt=yes
UninstallDisplayIcon={app}\amdl.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19041
OutputDir=dist
OutputBaseFilename=amdl-setup
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern dynamic
UsePreviousTasks=no
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
Type: files; Name: "{autoprograms}\AMDL Studio.lnk"; Check: not IsExtractMode
Type: files; Name: "{autodesktop}\AMDL Studio.lnk"; Check: not IsExtractMode
Type: files; Name: "{app}\AMDLStudio.exe"; Check: not IsExtractMode

[Icons]
Name: "{autoprograms}\amdl"; Filename: "{app}\amdl.exe"; WorkingDir: "{app}"; AppUserModelID: "{#StudioAppId}"; Check: not IsExtractMode
Name: "{autodesktop}\amdl"; Filename: "{app}\amdl.exe"; WorkingDir: "{app}"; AppUserModelID: "{#StudioAppId}"; Tasks: desktopicon; Check: not IsExtractMode

[Run]
Filename: "{app}\amdl.exe"; Description: "{cm:LaunchStudio}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Check: not IsExtractMode

[CustomMessages]
zhcn.DesktopIcon=创建桌面快捷方式
english.DesktopIcon=Create a desktop shortcut
zhcn.LaunchStudio=打开 amdl
english.LaunchStudio=Open amdl

[Code]
function IsExtractMode: Boolean;
begin
  Result := ExpandConstant('{param:EXTRACTONLY|0}') = '1';
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
