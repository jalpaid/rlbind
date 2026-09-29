; Inno Setup script for Rogue Keybind Remapper

#define MyAppName "Rogue Keybind Remapper"
#define MyAppVersion "1.0"
#define MyAppExeName "RogueKeybinds.exe"

[Setup]
AppId={{B7E4A2C1-5F3D-4A8B-9C2E-1D6F8A3B5C7D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Cole Blaney
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=RogueKeybinds-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Gate-UI detection uses Tesseract OCR; warn if it is missing.
function TesseractInstalled: Boolean;
begin
  Result := FileExists('C:\Program Files\Tesseract-OCR\tesseract.exe') or
            FileExists('C:\Program Files (x86)\Tesseract-OCR\tesseract.exe') or
            FileExists(ExpandConstant('{localappdata}\Programs\Tesseract-OCR\tesseract.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not TesseractInstalled then
      MsgBox('Gate-UI detection needs Tesseract-OCR, which was not found on this PC.' #13#10 #13#10
        + 'Install it from: https://github.com/UB-Mannheim/tesseract/wiki' #13#10
        + '(the app still runs without it, but auto-disabling for the Gate typing UI will not work).',
        mbInformation, MB_OK);
  end;
end;
