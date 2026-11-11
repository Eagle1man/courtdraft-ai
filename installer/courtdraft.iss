#define MyAppName "CourtDraft AI"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyAppPublisher "CourtDraft AI"
#define MyAppExeName "start-portable.cmd"

[Setup]
AppId={{7C3A1F2E-6B4D-4E8A-9C1D-2F5A8B7E9C10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\CourtDraft AI
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=CourtDraftAI-{#MyAppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\runtime\{#MyAppExeName}
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "service"; Description: "Служба Windows для всех пользователей (порт 8081, сеть)"; GroupDescription: "Режим установки:"; Flags: exclusive
Name: "portable"; Description: "Портативно для текущего пользователя (только этот компьютер, порт 8080)"; GroupDescription: "Режим установки:"; Flags: exclusive unchecked
Name: "firewall"; Description: "Открыть порт 8081 в брандмауэре для локальной сети"; GroupDescription: "Сеть:"; Flags: unchecked

[Files]
Source: "stage\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\runtime\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Настройка моделей"; Filename: "{app}\runtime\first-run.cmd"; WorkingDir: "{app}"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\runtime\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: portable

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\detect-hardware.ps1"" -InstallDir ""{app}"""; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install-service.ps1"" -InstallDir ""{app}"""; Flags: runhidden waituntilterminated; Tasks: service
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""CourtDraft AI 8081"" dir=in action=allow protocol=TCP localport=8081"; Flags: runhidden waituntilterminated; Tasks: service and firewall
Filename: "{app}\runtime\first-run.cmd"; Description: "Настроить модель сейчас"; Flags: postinstall nowait skipifsilent
Filename: "{app}\runtime\{#MyAppExeName}"; Description: "Запустить CourtDraft AI"; Flags: postinstall nowait skipifsilent unchecked; Tasks: portable

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\uninstall-service.ps1"" -InstallDir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "CourtDraftAI-service"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""CourtDraft AI 8081"""; Flags: runhidden waituntilterminated; RunOnceId: "CourtDraftAI-firewall"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  HardwareFile: String;
begin
  if CurStep = ssPostInstall then
  begin
    HardwareFile := ExpandConstant('{app}\hardware.json');
    if FileExists(HardwareFile) then
      MsgBox('Профиль железа определён. Рекомендации записаны в hardware.json.' + #13#10 +
             'Следующий шаг: «Настроить модель сейчас».', mbInformation, MB_OK)
    else
      MsgBox('Не удалось определить железо автоматически.' + #13#10 +
             'Модель можно выбрать вручную в «Настройка моделей».',
             mbWarning, MB_OK);
  end;
end;
