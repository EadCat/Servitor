; Inno Setup script for the Windows installer.
;
; build\build.bat does everything below and finds the compiler on its own; this
; is only what it runs. From the repository root:
;     pyinstaller --noconfirm build\Servitor.spec --workpath .pyinstaller --distpath dist
;     "<Inno Setup>\ISCC.exe" build\Servitor.iss
; Inno Setup keeps its compiler off PATH, and an unelevated winget install puts
; it under %LOCALAPPDATA%\Programs rather than Program Files. The installer
; lands in dist\. Inno Setup 6.6 or newer is required.
;
; Installs per user, so no administrator rights are needed. Uninstalling offers
; to delete the configuration as well, because it holds SSH passwords in plain
; text and leaving it behind would be a surprise.

#define AppName "Servitor"
; One place holds the version: the VERSION file at the repository root. Read it
; here rather than repeating it, so ISCC run by hand still stamps the right one.
#define VersionFile FileOpen(SourcePath + "..\VERSION")
#define AppVersion Trim(FileRead(VersionFile))
#expr FileClose(VersionFile)
#if AppVersion == ""
  #error VERSION at the repository root is empty
#endif
#define AppPublisher "Dae-Young Song"
#define AppURL "https://github.com/EadCat/Servitor"
#define AppExe "Servitor.exe"

[Setup]
; Keep this GUID stable: it is how Windows recognises an upgrade of the same app.
AppId={{7A1C4E62-2B58-4E1D-9C3F-5F0B2D6A81E4}
AppName={#AppName}
AppVersion={#AppVersion}
; Left alone, both read "Servitor version 1.2.0": the wizard's headings and the
; name under Settings > Apps. The version still shows in its own column there.
AppVerName={#AppName}
UninstallDisplayName={#AppName}
AppPublisher={#AppPublisher}
; These three fill in the links shown under Settings > Apps > Servitor.
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
AppCopyright=Copyright (c) 2026 {#AppPublisher}. MIT License.
; Stamped into the setup executable's own file properties.
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer
VersionInfoCopyright=Copyright (c) 2026 {#AppPublisher}
; Shown for acceptance before installing, and copied next to the program.
LicenseFile=..\LICENSE
; The program is 64-bit, so default to the real Program Files rather than the
; 32-bit one {autopf} would otherwise resolve to, and refuse 32-bit Windows
; instead of installing something that cannot run.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DefaultDirName={autopf}\{#AppName}
; That default is only a starting point. The folder page stays on, so anyone
; can install elsewhere, and PrivilegesRequiredOverridesAllowed below asks
; whether to install for this user or for everyone.
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Servitor-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
; Has Explorer drop its icon cache once installed. Reinstalled at the same
; path, the shortcuts otherwise kept showing the previous version's icon.
ChangesAssociations=yes
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[CustomMessages]
english.CleanupTitle=Remove Servitor
korean.CleanupTitle=Servitor 제거
english.CleanupIntro=Servitor will be removed. Tick anything you also want deleted; leaving a box clear keeps that item.
korean.CleanupIntro=Servitor를 제거합니다. 함께 지울 항목을 선택하십시오. 선택하지 않은 항목은 그대로 남습니다.
english.CleanupConfig=Saved servers and passwords (%1)
korean.CleanupConfig=저장된 서버와 비밀번호 (%1)
english.KeptConfig=Your saved servers and passwords were kept.%n%nTo remove them later, delete this folder by hand:%n%1
korean.KeptConfig=저장된 서버와 비밀번호를 남겨 두었습니다.%n%n나중에 지우려면 이 폴더를 직접 삭제하십시오:%n%1

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The bundle carries LGPL and Apache components, so ship their notices with it.
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
; Inno removes only the folders it created itself, so an install folder left
; empty by an earlier install survived the uninstall. Empty only: anything a
; user put there keeps the folder.
Type: dirifempty; Name: "{app}"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// Windows needs no font cleanup: CustomTkinter registers its fonts with
// AddFontResourceEx(FR_PRIVATE), which lasts only as long as the process and
// copies nothing to disk. On Linux the same toolkit copies them into ~/.fonts,
// which is why "Servitor --remove --purge" there deletes them and this does not.
var
  RemoveConfig: Boolean;

function ConfigFolder: String;
begin
  Result := ExpandConstant('{userappdata}\servitor');
end;

// Asked before anything is removed, so the choice is made while it can still
// be cancelled. A checkbox left clear keeps the data; nothing is preselected.
function InitializeUninstall: Boolean;
var
  Form: TSetupForm;
  Intro: TNewStaticText;
  ConfigBox: TNewCheckBox;
  BtnOk, BtnCancel: TNewButton;
begin
  Result := True;
  RemoveConfig := False;
  if not DirExists(ConfigFolder) then
    Exit;

  // Inno Setup 6.6 moved the client size into CreateCustomForm and made the
  // properties read-only afterwards. The two False arguments are KeepSizeX and
  // KeepSizeY, which would let the form grow with WizardSizePercent.
  Form := CreateCustomForm(ScaleX(430), ScaleY(170), False, False);
  try
    Form.Caption := CustomMessage('CleanupTitle');
    Form.Position := poScreenCenter;

    Intro := TNewStaticText.Create(Form);
    Intro.Parent := Form;
    Intro.Left := ScaleX(16);
    Intro.Top := ScaleY(16);
    Intro.Width := Form.ClientWidth - ScaleX(32);
    Intro.Height := ScaleY(44);
    Intro.AutoSize := False;
    Intro.WordWrap := True;
    Intro.Caption := CustomMessage('CleanupIntro');

    ConfigBox := TNewCheckBox.Create(Form);
    ConfigBox.Parent := Form;
    ConfigBox.Left := ScaleX(16);
    ConfigBox.Top := ScaleY(72);
    ConfigBox.Width := Form.ClientWidth - ScaleX(32);
    ConfigBox.Height := ScaleY(38);
    ConfigBox.Checked := False;
    ConfigBox.Caption := FmtMessage(CustomMessage('CleanupConfig'), [ConfigFolder]);

    BtnOk := TNewButton.Create(Form);
    BtnOk.Parent := Form;
    BtnOk.Width := ScaleX(86);
    BtnOk.Height := ScaleY(26);
    BtnOk.Left := Form.ClientWidth - ScaleX(188);
    BtnOk.Top := Form.ClientHeight - ScaleY(40);
    BtnOk.Caption := SetupMessage(msgButtonOK);
    BtnOk.ModalResult := mrOk;
    BtnOk.Default := True;

    BtnCancel := TNewButton.Create(Form);
    BtnCancel.Parent := Form;
    BtnCancel.Width := ScaleX(86);
    BtnCancel.Height := ScaleY(26);
    BtnCancel.Left := Form.ClientWidth - ScaleX(96);
    BtnCancel.Top := BtnOk.Top;
    BtnCancel.Caption := SetupMessage(msgButtonCancel);
    BtnCancel.ModalResult := mrCancel;
    BtnCancel.Cancel := True;

    if Form.ShowModal = mrOk then
      RemoveConfig := ConfigBox.Checked
    else
      Result := False;
  finally
    Form.Free;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RemoveConfig then
    begin
      if DirExists(ConfigFolder) then
        DelTree(ConfigFolder, True, True, True);
    end
    else if DirExists(ConfigFolder) then
      // Keeping it is fine, but say where it is so it is not forgotten.
      MsgBox(FmtMessage(CustomMessage('KeptConfig'), [ConfigFolder]),
             mbInformation, MB_OK);
  end;
end;
