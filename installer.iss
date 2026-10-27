; Inno Setup script for CherryShell. Build with build.bat.

#define AppName "CherryShell"
#define AppVersion "0.1"

[Setup]
AppId={{6C1E2A4F-8B3D-4E7A-9C51-3F2D8A6B1C07}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=CherryShell
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install by default (no admin needed); user can choose all-users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=CherryShell-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
UninstallDisplayIcon={app}\cherryshell.exe

[Files]
Source: "dist\cherryshell.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\cherryshell.exe"; WorkingDir: "{userdocs}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\cherryshell.exe"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "addtopath"; Description: "Add CherryShell to PATH (type 'cherryshell' in any terminal)"

[Run]
Filename: "{app}\cherryshell.exe"; Description: "Launch CherryShell"; Flags: nowait postinstall skipifsilent

[Code]
function EnvKeyRoot: Integer;
begin
  if IsAdminInstallMode then
    Result := HKEY_LOCAL_MACHINE
  else
    Result := HKEY_CURRENT_USER;
end;

function EnvKeyPath: String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

procedure AddToPath(Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(EnvKeyRoot, EnvKeyPath, 'Path', Paths) then
    Paths := '';
  if Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0 then
    exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then
    Paths := Paths + ';';
  RegWriteExpandStringValue(EnvKeyRoot, EnvKeyPath, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(Dir: String);
var
  Paths: String;
  P: Integer;
begin
  if not RegQueryStringValue(EnvKeyRoot, EnvKeyPath, 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if P = 0 then
    exit;
  Delete(Paths, P, Length(Dir) + 1);
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteExpandStringValue(EnvKeyRoot, EnvKeyPath, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
