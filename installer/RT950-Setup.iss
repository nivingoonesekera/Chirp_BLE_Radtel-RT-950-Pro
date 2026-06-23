; ============================================================================
;  RT-950 Pro BLE — one-click Windows installer
;
;  Builds RT950-Setup.exe. It bundles the CHIRP drivers + the BLE bridge/GUI,
;  and at install time sets up the Python they need. The user chooses how:
;    * "Use my system Python 3.10"  -> installs 3.10 only if it's missing,
;                                      then pip-installs bleak + pyserial.
;    * "Install a private Python"   -> a self-contained Python 3.10 inside the
;                                      app folder (with Tk + pip), so nothing
;                                      else on the PC is touched.
;
;  It does NOT install com0com. A virtual COM-port bridge is needed for the
;  CHIRP-via-bridge / CPS paths, but com0com v3 is unreliable on Windows 11, so
;  we let the user install the known-good v2.2.2 themselves (see NEXT-STEPS.txt).
;  CHIRP itself is also not installed (it can't be silenced reliably).
;
;  Build:  see installer/build.md  (winget install JRSoftware.InnoSetup,
;          then  ISCC.exe RT950-Setup.iss)
;  Requires Inno Setup 6.1+ (for the built-in download support).
; ============================================================================

#define AppName     "RT-950 Pro BLE"
#define AppVersion  "1.1.0"
#define AppPublisher "Nivin Goonesekera (VK3NWG)"
#define AppURL      "https://github.com/nivingoonesekera/Radtel-RT-950Pro-BLE-bridge-for-CHIRP-and-CPS"

; Downloaded dependency (verified canonical URL)
#define PyVer       "3.10.11"
#define PyExe       "python-" + PyVer + "-amd64.exe"
#define PyUrl       "https://www.python.org/ftp/python/" + PyVer + "/" + PyExe

[Setup]
AppId={{E1DF89C8-3E9F-4C89-9D43-AB3841830A96}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\RT-950 Pro BLE
DefaultGroupName=RT-950 Pro BLE
DisableProgramGroupPage=yes
OutputDir=..
OutputBaseFilename=RT950-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\icon.ico
LicenseFile=..\LICENSE
; Shown on the final page: how to finish the setup (com0com v2.2.2 + CHIRP).
InfoAfterFile=..\NEXT-STEPS.txt
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Program Files install dir + the all-users system-Python option both need admin.
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut for the Bridge GUI"; GroupDescription: "Shortcuts:"

[Files]
; Drivers (loaded into CHIRP via Load Module) + bridge/GUI + icons + docs.
; Sources are relative to this .iss (release/installer/) -> parent is release/.
Source: "..\radtel_rt950pro_BLE_int.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\radtel_rt950pro_BL.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\ble_bridge.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\bridge_gui.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico";                   DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.png";                   DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";                  DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
Source: "..\LICENSE";                    DestDir: "{app}"; Flags: ignoreversion
; NEXT-STEPS.txt is shown inline on the final wizard page (InfoAfterFile) only --
; not installed as a stray file the user has to go open.

[Icons]
; --- GUI launcher (system-Python mode): windowed py launcher pinned to 3.10, so
;     the GUI's sys.executable (used to spawn ble_bridge.py) is the 3.10 that has
;     bleak + pyserial. ---
Name: "{group}\RT-950 Pro Bridge"; Filename: "{win}\pyw.exe"; Parameters: "-3.10 ""{app}\bridge_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Check: UseSystemPython
Name: "{autodesktop}\RT-950 Pro Bridge"; Filename: "{win}\pyw.exe"; Parameters: "-3.10 ""{app}\bridge_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon; Check: UseSystemPython
; --- GUI launcher (private-Python mode): the bundled interpreter in {app}. ---
Name: "{group}\RT-950 Pro Bridge"; Filename: "{app}\python310\pythonw.exe"; Parameters: """{app}\bridge_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Check: UsePrivatePython
Name: "{autodesktop}\RT-950 Pro Bridge"; Filename: "{app}\python310\pythonw.exe"; Parameters: """{app}\bridge_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon; Check: UsePrivatePython
; --- Shared shortcuts ---
Name: "{group}\RT-950 Driver Files"; Filename: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall RT-950 Pro BLE"; Filename: "{uninstallexe}"

[Registry]
; Private-Python mode: tell the integrated CHIRP driver where bleak lives, since
; the bundled interpreter isn't reachable via the py -3.10 launcher. (Its ABI is
; 3.10, matching CHIRP's frozen chirpwx.exe, so the borrow is valid.)
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: string; ValueName: "RT950_BLEAK_SITE"; ValueData: "{app}\python310\Lib\site-packages"; \
  Flags: uninsdeletevalue; Check: UsePrivatePython

[Run]
; --- SYSTEM-PYTHON MODE -----------------------------------------------------
; 1) Python 3.10 — only if a working 3.10 isn't already present. Non-intrusive:
;    NO PrependPath (your PATH / default `python` is left alone), version-specific
;    so other Python versions are untouched. Reached via `py -3.10` thereafter.
Filename: "{tmp}\{#PyExe}"; \
  Parameters: "/quiet InstallAllUsers=1 Include_launcher=1 Include_pip=1"; \
  StatusMsg: "Installing Python {#PyVer} ..."; Flags: waituntilterminated; Check: NeedSystemPython
; 2) bleak + pyserial into that 3.10 (the py launcher always lands in {win}).
Filename: "{win}\py.exe"; Parameters: "-3.10 -m pip install bleak pyserial"; \
  StatusMsg: "Installing bleak + pyserial ..."; Flags: waituntilterminated runhidden; Check: UseSystemPython

; --- PRIVATE-PYTHON MODE ----------------------------------------------------
; 3) A self-contained Python 3.10 inside the app folder (Tk + pip included so the
;    GUI runs and pip works). InstallAllUsers=0 / no launcher = nothing global is
;    touched; the whole thing lives under {app}\python310 and is removed on uninstall.
Filename: "{tmp}\{#PyExe}"; \
  Parameters: "/quiet InstallAllUsers=0 Include_launcher=0 Include_pip=1 Include_tcltk=1 TargetDir=""{app}\python310"""; \
  StatusMsg: "Installing a private Python {#PyVer} ..."; Flags: waituntilterminated; Check: UsePrivatePython
; 4) bleak + pyserial into the private interpreter.
Filename: "{app}\python310\python.exe"; Parameters: "-m pip install bleak pyserial"; \
  StatusMsg: "Installing bleak + pyserial ..."; Flags: waituntilterminated runhidden; Check: UsePrivatePython

[UninstallDelete]
; Remove the private interpreter (incl. the site-packages we pip-installed).
Type: filesandordirs; Name: "{app}\python310"

[Code]
var
  DownloadPage: TDownloadWizardPage;
  PyModePage: TInputOptionWizardPage;
  Python310Present: Boolean;

function UsePrivatePython: Boolean;
begin
  Result := PyModePage.SelectedValueIndex = 1;
end;

function UseSystemPython: Boolean;
begin
  Result := PyModePage.SelectedValueIndex = 0;
end;

// System mode only needs to install Python when a working 3.10 is absent.
function NeedSystemPython: Boolean;
begin
  Result := UseSystemPython and not Python310Present;
end;

// True whenever we must fetch the python.org installer (either mode that installs it).
function NeedPythonDownload: Boolean;
begin
  Result := UsePrivatePython or NeedSystemPython;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
                                     SetupMessage(msgPreparingDesc), nil);

  PyModePage := CreateInputOptionPage(wpSelectTasks,
    'Python setup', 'How should Python 3.10 be provided?',
    'The drivers and bridge need Python 3.10 with the bleak + pyserial packages.' + #13#10 +
    'Pick one (you can change nothing else about your system):',
    True, False);
  PyModePage.Add('Use my system Python 3.10 (recommended). Installs 3.10 only if it''s missing; never changes your PATH.');
  PyModePage.Add('Install a private Python 3.10 inside this app. Self-contained; touches nothing else on your PC.');
  PyModePage.SelectedValueIndex := 0;
end;

function InitializeSetup: Boolean;
var
  RC: Integer;
begin
  // If a working Python 3.10 is already on the machine, don't reinstall/modify
  // it -- ask the py launcher. (Exec fails cleanly if py.exe isn't there at all.)
  Python310Present :=
    Exec(ExpandConstant('{win}\py.exe'), '-3.10 -c "import sys"', '',
         SW_HIDE, ewWaitUntilTerminated, RC) and (RC = 0);
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then begin
    DownloadPage.Clear;
    if NeedPythonDownload then begin
      DownloadPage.Add('{#PyUrl}', '{#PyExe}', '');
      DownloadPage.Show;
      try
        try
          DownloadPage.Download;
          Result := True;
        except
          if DownloadPage.AbortedByUser then
            Log('Download aborted by user.')
          else
            SuppressibleMsgBox(AddPeriod(GetExceptionMessage), mbCriticalError, MB_OK, IDOK);
          Result := False;
        end;
      finally
        DownloadPage.Hide;
      end;
    end;
    // Nothing to fetch (system Python already present) -> proceed.
  end;
end;
