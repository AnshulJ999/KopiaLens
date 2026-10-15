; KopiaLens Windows installer. Tauri fills the handlebars values at bundle time.
; Based on Tauri's default NSIS template (tauri-cli 2.11.4). Differences: "Just me / Everyone"
; without forcing admin (elevates only when Everyone is chosen), options page for the desktop
; shortcut and Start at logon, a clean replace of the server folder on upgrade, and an uninstall
; option that also removes the KopiaLens data folder.
Unicode true
ManifestDPIAware true
ManifestDPIAwareness PerMonitorV2

!if "{{compression}}" == "none"
  SetCompress off
!else
  SetCompressor /SOLID "{{compression}}"
!endif

{{#if signed_plugins_path}}
!addplugindir "{{signed_plugins_path}}"
{{/if}}

!include MUI2.nsh
!include FileFunc.nsh
!include x64.nsh
!include WordFunc.nsh
!include nsDialogs.nsh
!include "utils.nsh"
!include "Win\COM.nsh"
!include "Win\Propkey.nsh"

!define WEBVIEW2APPGUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

!define MANUFACTURER "{{manufacturer}}"
!define PRODUCTNAME "{{product_name}}"
!define VERSION "{{version}}"
!define VERSIONWITHBUILD "{{version_with_build}}"
!define INSTALLMODE "{{install_mode}}"
!define INSTALLERICON "{{installer_icon}}"
!define UNINSTALLERICON "{{uninstaller_icon}}"
!define MAINBINARYNAME "{{main_binary_name}}"
!define MAINBINARYSRCPATH "{{main_binary_path}}"
!define BUNDLEID "{{bundle_id}}"
!define COPYRIGHT "{{copyright}}"
!define OUTFILE "{{out_file}}"
!define ARCH "{{arch}}"
!define ADDITIONALPLUGINSPATH "{{additional_plugins_path}}"
!define INSTALLWEBVIEW2MODE "{{install_webview2_mode}}"
!define WEBVIEW2INSTALLERARGS "{{webview2_installer_args}}"
!define WEBVIEW2BOOTSTRAPPERPATH "{{webview2_bootstrapper_path}}"
!define WEBVIEW2INSTALLERPATH "{{webview2_installer_path}}"
!define ESTIMATEDSIZE "{{estimated_size}}"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCTNAME}"
!define MANUKEY "Software\${MANUFACTURER}"
!define MANUPRODUCTKEY "${MANUKEY}\${PRODUCTNAME}"
; Same value name and format the desktop app's Start at logon switch uses (tauri-plugin-autostart).
!define RUNKEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define STARTUPAPPROVEDKEY "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
!define SERVEREXE "kopialens-server.exe"

Var PassiveMode
Var UpdateMode
Var NoShortcutMode
Var AllUsers
Var PrevInstall
Var ModeFromCmdline
Var DirFromCmdline
Var WantDesktop
Var WantStartup
Var WantPath
Var ModeRadioAll
Var DesktopBox
Var StartupBox
Var PathBox
Var DeleteDataBox
Var DeleteDataState

Name "${PRODUCTNAME}"
BrandingText "${COPYRIGHT}"
OutFile "${OUTFILE}"
; A placeholder, so a folder given with /D= can be told apart from the default.
!define PLACEHOLDER_INSTALL_DIR "placeholder\${PRODUCTNAME}"
InstallDir "${PLACEHOLDER_INSTALL_DIR}"
; Elevation happens only when "Everyone" is chosen (the installer relaunches itself as admin).
RequestExecutionLevel user

VIProductVersion "${VERSIONWITHBUILD}"
VIAddVersionKey "ProductName" "${PRODUCTNAME}"
VIAddVersionKey "FileDescription" "${PRODUCTNAME} Setup"
VIAddVersionKey "LegalCopyright" "${COPYRIGHT}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

!addplugindir "${ADDITIONALPLUGINSPATH}"

!if "${INSTALLERICON}" != ""
  !define MUI_ICON "${INSTALLERICON}"
!endif
!if "${UNINSTALLERICON}" != ""
  !define MUI_UNICON "${UNINSTALLERICON}"
!endif

; ---------------------------------------------------------------- installer pages
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!insertmacro MUI_PAGE_WELCOME

Page custom PageMode PageModeLeave

!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipDirectory
!insertmacro MUI_PAGE_DIRECTORY

Page custom PageOptions PageOptionsLeave

!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "$(klRunNow)"
!define MUI_FINISHPAGE_RUN_FUNCTION RunMainBinary
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!insertmacro MUI_PAGE_FINISH

; ---------------------------------------------------------------- uninstaller pages
!define MUI_UNCONFIRMPAGE_TEXT_TOP "$(klUninstallTop)"
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.ConfirmShow
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE un.ConfirmLeave
!define MUI_PAGE_CUSTOMFUNCTION_PRE un.SkipIfPassive
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

{{#each languages}}
!insertmacro MUI_LANGUAGE "{{this}}"
{{/each}}
!insertmacro MUI_RESERVEFILE_LANGDLL
{{#each language_files}}
  !include "{{this}}"
{{/each}}

LangString klModeTitle ${LANG_ENGLISH} "Who should have KopiaLens?"
LangString klModeSubtitle ${LANG_ENGLISH} "Choose who can use KopiaLens on this computer."
LangString klModeMe ${LANG_ENGLISH} "Just me (no administrator rights needed)"
LangString klModeAll ${LANG_ENGLISH} "Everyone on this computer (needs administrator rights)"
LangString klNoAdmin ${LANG_ENGLISH} "KopiaLens didn't get administrator rights. Choose $\"Just me$\", or try again."
LangString klOptionsTitle ${LANG_ENGLISH} "Options"
LangString klOptionsSubtitle ${LANG_ENGLISH} "You can change these later."
LangString klDesktop ${LANG_ENGLISH} "Create a desktop shortcut"
LangString klStartup ${LANG_ENGLISH} "Start KopiaLens when I sign in to Windows"
LangString klStartupHint ${LANG_ENGLISH} "Recommended: scheduled indexing only runs while KopiaLens is running. You can change this later in the tray menu."
LangString klPath ${LANG_ENGLISH} "Add the klens command line to PATH"
LangString klPathHint ${LANG_ENGLISH} "Lets you run klens and kopialens-cli in any terminal, for scripts and AI agents."
LangString klPathFailed ${LANG_ENGLISH} "Couldn't add $INSTDIR\bin to PATH. Add it yourself to use klens."
LangString klStillRunning ${LANG_ENGLISH} "KopiaLens is still running. Close it from the tray, then click Retry."
LangString klStillRunningSilent ${LANG_ENGLISH} "KopiaLens is still running. Close it, then try again."
LangString klRunNow ${LANG_ENGLISH} "Open KopiaLens"
LangString klClosing ${LANG_ENGLISH} "Closing KopiaLens..."
LangString klUninstallTop ${LANG_ENGLISH} "KopiaLens will be removed from this computer. Your backups and kopia repositories are not touched."
LangString klDeleteData ${LANG_ENGLISH} "Also delete KopiaLens settings and index"
LangString klSilentNoAdmin ${LANG_ENGLISH} "Installing for everyone needs administrator rights.$\n"

; ---------------------------------------------------------------- helpers
!macro IsAdmin _out
  UserInfo::GetAccountType
  Pop ${_out}
!macroend

; Sets the shell context for the chosen mode, and the default folder when this is a new install.
Function ApplyMode
  ${If} $AllUsers = 1
    SetShellVarContext all
    ${If} $PrevInstall = 0
    ${AndIf} $DirFromCmdline = 0
      StrCpy $INSTDIR "$PROGRAMFILES64\${PRODUCTNAME}"
    ${EndIf}
  ${Else}
    SetShellVarContext current
    ${If} $PrevInstall = 0
    ${AndIf} $DirFromCmdline = 0
      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
    ${EndIf}
  ${EndIf}
FunctionEnd

Function SkipIfPassive
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
FunctionEnd

; Adds (_add 1) or removes (_add 0) <install>\bin on the PATH of the chosen mode, through klens.exe
; (needs bin\ in place). Never read PATH into an NSIS string: they stop at 1024 characters, and a
; longer PATH reads back empty.
!macro KlPathEdit _add
  ${If} $AllUsers = 1
    StrCpy $R1 "machine"
  ${Else}
    StrCpy $R1 "user"
  ${EndIf}
  ${If} ${_add} = 1
    nsExec::Exec '"$INSTDIR\bin\klens.exe" --installer-path add $R1 "$INSTDIR\bin"'
    Pop $R0
    ${If} $R0 != "0"
      DetailPrint "$(klPathFailed)"
    ${EndIf}
  ${Else}
    nsExec::Exec '"$INSTDIR\bin\klens.exe" --installer-path remove $R1 "$INSTDIR\bin"'
    Pop $R0
  ${EndIf}
!macroend

Function un.SkipIfPassive
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
FunctionEnd

; Upgrades go to the existing folder.
Function SkipDirectory
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
  ${IfThen} $PrevInstall = 1 ${|} Abort ${|}
FunctionEnd

Function RunMainBinary
  nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" ""
FunctionEnd

; Sets $0 to 1 when the file can't be opened for writing: Windows refuses that while the program
; runs. Checking this install's own files leaves other KopiaLens copies (same exe names) alone.
!macro KlFileInUse _path
  ${If} ${FileExists} "${_path}"
    ClearErrors
    FileOpen $1 "${_path}" a
    ${If} ${Errors}
      StrCpy $0 1
    ${Else}
      FileClose $1
    ${EndIf}
  ${EndIf}
!macroend

; Asks this install's KopiaLens to quit (it stops its server and index workers) and waits for its
; programs to end. Still running after that: asks the user, or in a silent install ends this user's
; copies as a last resort.
!macro CLOSE_RUNNING_APP UN
Function ${UN}InstallInUse
  StrCpy $0 0
  !insertmacro KlFileInUse "$INSTDIR\${MAINBINARYNAME}.exe"
  !insertmacro KlFileInUse "$INSTDIR\server\${SERVEREXE}"
  !insertmacro KlFileInUse "$INSTDIR\server\kopia\kopia.exe"
FunctionEnd

Function ${UN}CloseRunningApp
  Call ${UN}InstallInUse
  ${If} $0 = 0
    Return
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\${MAINBINARYNAME}.exe"
    DetailPrint "$(klClosing)"
    nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" "--quit"
  ${EndIf}
  ; The app gives an index worker up to 60 s to save; wait a little longer.
  StrCpy $2 0
  ${Do}
    Sleep 500
    Call ${UN}InstallInUse
    ${If} $0 = 0
      Return
    ${EndIf}
    IntOp $2 $2 + 1
  ${LoopUntil} $2 >= 150
  ${If} ${Silent}
    nsis_tauri_utils::KillProcessCurrentUser "${MAINBINARYNAME}.exe"
    Pop $1
    nsis_tauri_utils::KillProcessCurrentUser "${SERVEREXE}"
    Pop $1
    Sleep 2000
    Call ${UN}InstallInUse
    ${If} $0 <> 0
      Abort "$(klStillRunningSilent)"
    ${EndIf}
  ${Else}
    ${Do}
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(klStillRunning)" IDRETRY +2
      Abort
      Call ${UN}InstallInUse
    ${LoopWhile} $0 <> 0
  ${EndIf}
FunctionEnd
!macroend
!insertmacro CLOSE_RUNNING_APP ""
!insertmacro CLOSE_RUNNING_APP "un."

; ---------------------------------------------------------------- install mode page
Function PageMode
  ${If} $PassiveMode = 1
  ${OrIf} $PrevInstall = 1
  ${OrIf} $ModeFromCmdline = 1
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "$(klModeTitle)" "$(klModeSubtitle)"
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateRadioButton} 0 10u 100% 12u "$(klModeMe)"
  Pop $1
  ${NSD_CreateRadioButton} 0 30u 100% 12u "$(klModeAll)"
  Pop $ModeRadioAll
  ${If} $AllUsers = 1
    ${NSD_Check} $ModeRadioAll
  ${Else}
    ${NSD_Check} $1
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function PageModeLeave
  ${NSD_GetState} $ModeRadioAll $0
  ${If} $0 = ${BST_CHECKED}
    !insertmacro IsAdmin $1
    ${If} $1 != "Admin"
      ; Relaunch elevated; the new copy starts with "Everyone" already chosen.
      ClearErrors
      ExecShell "runas" "$EXEPATH" "/ALLUSERS"
      ${If} ${Errors}
        MessageBox MB_ICONEXCLAMATION "$(klNoAdmin)"
        Abort
      ${EndIf}
      Quit
    ${EndIf}
    StrCpy $AllUsers 1
  ${Else}
    StrCpy $AllUsers 0
  ${EndIf}
  Call ApplyMode
FunctionEnd

; ---------------------------------------------------------------- options page
Function PageOptions
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
  !insertmacro MUI_HEADER_TEXT "$(klOptionsTitle)" "$(klOptionsSubtitle)"
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateCheckbox} 0 10u 100% 12u "$(klDesktop)"
  Pop $DesktopBox
  ${If} $WantDesktop = 1
    ${NSD_Check} $DesktopBox
  ${EndIf}
  ${NSD_CreateCheckbox} 0 32u 100% 12u "$(klStartup)"
  Pop $StartupBox
  ${If} $WantStartup = 1
    ${NSD_Check} $StartupBox
  ${EndIf}
  ${NSD_CreateLabel} 12u 46u -12u 24u "$(klStartupHint)"
  Pop $0
  ${NSD_CreateCheckbox} 0 76u 100% 12u "$(klPath)"
  Pop $PathBox
  ${If} $WantPath = 1
    ${NSD_Check} $PathBox
  ${EndIf}
  ${NSD_CreateLabel} 12u 90u -12u 16u "$(klPathHint)"
  Pop $0
  nsDialogs::Show
FunctionEnd

Function PageOptionsLeave
  ${NSD_GetState} $DesktopBox $0
  ${If} $0 = ${BST_CHECKED}
    StrCpy $WantDesktop 1
  ${Else}
    StrCpy $WantDesktop 0
  ${EndIf}
  ${NSD_GetState} $StartupBox $0
  ${If} $0 = ${BST_CHECKED}
    StrCpy $WantStartup 1
  ${Else}
    StrCpy $WantStartup 0
  ${EndIf}
  ${NSD_GetState} $PathBox $0
  ${If} $0 = ${BST_CHECKED}
    StrCpy $WantPath 1
  ${Else}
    StrCpy $WantPath 0
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- init
Function .onInit
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}

  StrCpy $PassiveMode 0
  StrCpy $NoShortcutMode 0
  StrCpy $UpdateMode 0
  StrCpy $ModeFromCmdline 0
  StrCpy $AllUsers 0
  StrCpy $PrevInstall 0
  StrCpy $DirFromCmdline 0
  ${If} $INSTDIR != "${PLACEHOLDER_INSTALL_DIR}"
    StrCpy $DirFromCmdline 1
  ${EndIf}

  ${GetOptions} $CMDLINE "/P" $0
  ${IfNot} ${Errors}
    StrCpy $PassiveMode 1
  ${EndIf}
  ${GetOptions} $CMDLINE "/NS" $0
  ${IfNot} ${Errors}
    StrCpy $NoShortcutMode 1
  ${EndIf}
  ${GetOptions} $CMDLINE "/UPDATE" $0
  ${IfNot} ${Errors}
    StrCpy $UpdateMode 1
  ${EndIf}

  ; An existing install decides the mode and folder.
  ReadRegStr $0 HKLM "${UNINSTKEY}" "UninstallString"
  ${If} $0 != ""
    StrCpy $PrevInstall 1
    StrCpy $AllUsers 1
    ReadRegStr $INSTDIR HKLM "${MANUPRODUCTKEY}" ""
  ${Else}
    ReadRegStr $0 HKCU "${UNINSTKEY}" "UninstallString"
    ${If} $0 != ""
      StrCpy $PrevInstall 1
      ReadRegStr $INSTDIR HKCU "${MANUPRODUCTKEY}" ""
    ${EndIf}
  ${EndIf}
  ${If} $PrevInstall = 1
  ${AndIf} $INSTDIR == ""
    StrCpy $PrevInstall 0
  ${EndIf}

  ${If} $PrevInstall = 0
    ${GetOptions} $CMDLINE "/ALLUSERS" $0
    ${IfNot} ${Errors}
      StrCpy $AllUsers 1
      StrCpy $ModeFromCmdline 1
    ${EndIf}
    ${GetOptions} $CMDLINE "/CURRENTUSER" $0
    ${IfNot} ${Errors}
      StrCpy $AllUsers 0
      StrCpy $ModeFromCmdline 1
    ${EndIf}
  ${EndIf}

  ${If} $AllUsers = 1
    !insertmacro IsAdmin $0
    ${If} $0 != "Admin"
      ${If} ${Silent}
      ${OrIf} $PassiveMode = 1
      ${OrIf} $PrevInstall = 1
        ; Upgrading an install for everyone needs admin too: relaunch elevated.
        ${IfNot} ${Silent}
          ${GetParameters} $1
          ClearErrors
          ExecShell "runas" "$EXEPATH" "$1"
          ${IfNot} ${Errors}
            Quit
          ${EndIf}
        ${EndIf}
        MessageBox MB_ICONEXCLAMATION "$(klSilentNoAdmin)" /SD IDOK
        Abort
      ${EndIf}
    ${EndIf}
  ${EndIf}

  Call ApplyMode

  ; Defaults: on for a new install; an upgrade keeps what the user has now.
  StrCpy $WantDesktop 1
  StrCpy $WantStartup 1
  StrCpy $WantPath 1
  ${If} $PrevInstall = 1
    ClearErrors
    ReadRegDWORD $0 SHCTX "${UNINSTKEY}" "KlensOnPath"
    ${IfNot} ${Errors}
      StrCpy $WantPath $0
    ${EndIf}
    StrCpy $WantDesktop 0
    ${If} ${FileExists} "$DESKTOP\${PRODUCTNAME}.lnk"
      StrCpy $WantDesktop 1
    ${EndIf}
    ReadRegStr $0 HKCU "${RUNKEY}" "${PRODUCTNAME}"
    ${If} $0 == ""
      StrCpy $WantStartup 0
    ${EndIf}
  ${EndIf}
  ${GetOptions} $CMDLINE "/NOSTARTUP" $0
  ${IfNot} ${Errors}
    StrCpy $WantStartup 0
  ${EndIf}
  ${GetOptions} $CMDLINE "/NOPATH" $0
  ${IfNot} ${Errors}
    StrCpy $WantPath 0
  ${EndIf}
  ${If} $NoShortcutMode = 1
    StrCpy $WantDesktop 0
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- sections
Section WebView2
  ${If} ${RunningX64}
    ReadRegStr $4 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${Else}
    ReadRegStr $4 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${EndIf}
  ${If} $4 == ""
    ReadRegStr $4 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${EndIf}

  ${If} $4 == ""
  ${AndIf} $UpdateMode <> 1
    !if "${INSTALLWEBVIEW2MODE}" == "downloadBootstrapper"
      Delete "$TEMP\MicrosoftEdgeWebview2Setup.exe"
      DetailPrint "$(webview2Downloading)"
      NSISdl::download "https://go.microsoft.com/fwlink/p/?LinkId=2124703" "$TEMP\MicrosoftEdgeWebview2Setup.exe"
      Pop $0
      ${If} $0 == "success"
        DetailPrint "$(webview2DownloadSuccess)"
      ${Else}
        DetailPrint "$(webview2DownloadError)"
        Abort "$(webview2AbortError)"
      ${EndIf}
      StrCpy $6 "$TEMP\MicrosoftEdgeWebview2Setup.exe"
      Goto install_webview2
    !endif
    !if "${INSTALLWEBVIEW2MODE}" == "embedBootstrapper"
      Delete "$TEMP\MicrosoftEdgeWebview2Setup.exe"
      File "/oname=$TEMP\MicrosoftEdgeWebview2Setup.exe" "${WEBVIEW2BOOTSTRAPPERPATH}"
      StrCpy $6 "$TEMP\MicrosoftEdgeWebview2Setup.exe"
      Goto install_webview2
    !endif
    !if "${INSTALLWEBVIEW2MODE}" == "offlineInstaller"
      Delete "$TEMP\MicrosoftEdgeWebView2RuntimeInstaller.exe"
      File "/oname=$TEMP\MicrosoftEdgeWebView2RuntimeInstaller.exe" "${WEBVIEW2INSTALLERPATH}"
      StrCpy $6 "$TEMP\MicrosoftEdgeWebView2RuntimeInstaller.exe"
      Goto install_webview2
    !endif
    Goto webview2_done

    install_webview2:
      DetailPrint "$(installingWebview2)"
      ExecWait "$6 ${WEBVIEW2INSTALLERARGS} /install" $1
      ${If} $1 = 0
        DetailPrint "$(webview2InstallSuccess)"
      ${Else}
        DetailPrint "$(webview2InstallError)"
        Abort "$(webview2AbortError)"
      ${EndIf}
    webview2_done:
  ${EndIf}
SectionEnd

Section Install
  SetOutPath $INSTDIR
  Call CloseRunningApp

  ; The server folder belongs to KopiaLens alone; replace it whole so no file from an older
  ; build lingers.
  ${If} ${FileExists} "$INSTDIR\server\${SERVEREXE}"
    RMDir /r "$INSTDIR\server"
  ${EndIf}

  File "${MAINBINARYSRCPATH}"
  {{#each resources_dirs}}
    CreateDirectory "$INSTDIR\\{{this}}"
  {{/each}}
  {{#each resources}}
    File /a "/oname={{this.[1]}}" "{{no-escape @key}}"
  {{/each}}

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ${If} $WantPath = 1
    !insertmacro KlPathEdit 1
  ${Else}
    !insertmacro KlPathEdit 0
  ${EndIf}

  WriteRegStr SHCTX "${MANUPRODUCTKEY}" "" $INSTDIR
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayIcon" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\""
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr SHCTX "${UNINSTKEY}" "Publisher" "${MANUFACTURER}"
  WriteRegStr SHCTX "${UNINSTKEY}" "InstallLocation" "$\"$INSTDIR$\""
  WriteRegStr SHCTX "${UNINSTKEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr SHCTX "${UNINSTKEY}" "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S"
  WriteRegDWORD SHCTX "${UNINSTKEY}" "NoModify" "1"
  WriteRegDWORD SHCTX "${UNINSTKEY}" "NoRepair" "1"
  WriteRegDWORD SHCTX "${UNINSTKEY}" "KlensOnPath" $WantPath
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD SHCTX "${UNINSTKEY}" "EstimatedSize" "$0"

  ; Start menu shortcut, tagged with the app ID so notifications show as KopiaLens.
  ${If} $UpdateMode <> 1
    CreateShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "$SMPROGRAMS\${PRODUCTNAME}.lnk"
  ${EndIf}

  ${If} $WantDesktop = 1
    CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "$DESKTOP\${PRODUCTNAME}.lnk"
  ${ElseIf} $UpdateMode <> 1
    !insertmacro IsShortcutTarget "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${If} $0 = 1
      Delete "$DESKTOP\${PRODUCTNAME}.lnk"
    ${EndIf}
  ${EndIf}

  ${If} $WantStartup = 1
    WriteRegStr HKCU "${RUNKEY}" "${PRODUCTNAME}" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\" --hidden"
    WriteRegBin HKCU "${STARTUPAPPROVEDKEY}" "${PRODUCTNAME}" 020000000000000000000000
  ${ElseIf} $UpdateMode <> 1
    ; Only our own entry: another copy of KopiaLens may own the name.
    ReadRegStr $0 HKCU "${RUNKEY}" "${PRODUCTNAME}"
    ${WordFind} $0 "$INSTDIR\${MAINBINARYNAME}.exe" "E+1{" $1
    ${IfNot} ${Errors}
      DeleteRegValue HKCU "${RUNKEY}" "${PRODUCTNAME}"
    ${EndIf}
  ${EndIf}

  ${If} $PassiveMode = 1
    SetAutoClose true
  ${EndIf}
SectionEnd

Function .onInstSuccess
  ${If} $PassiveMode = 1
  ${OrIf} ${Silent}
    ${GetOptions} $CMDLINE "/R" $R0
    ${IfNot} ${Errors}
      nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" ""
    ${EndIf}
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- uninstall
Function un.ConfirmShow
  FindWindow $1 "#32770" "" $HWNDPARENT
  System::Call "user32::GetDpiForWindow(p r1) i .r2"
  IntOp $4 0 * $2
  IntOp $5 100 * $2
  IntOp $6 420 * $2
  IntOp $7 25 * $2
  IntOp $4 $4 / 96
  IntOp $5 $5 / 96
  IntOp $6 $6 / 96
  IntOp $7 $7 / 96
  System::Call 'user32::CreateWindowEx(i ${__NSD_CheckBox_EXSTYLE}, w "${__NSD_CheckBox_CLASS}", w "$(klDeleteData)", i ${__NSD_CheckBox_STYLE}, i r4, i r5, i r6, i r7, p r1, i0, i0, i0) i .s'
  Pop $DeleteDataBox
  SendMessage $HWNDPARENT ${WM_GETFONT} 0 0 $1
  SendMessage $DeleteDataBox ${WM_SETFONT} $1 1
FunctionEnd

Function un.ConfirmLeave
  SendMessage $DeleteDataBox ${BM_GETCHECK} 0 0 $DeleteDataState
FunctionEnd

Function un.onInit
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}
  StrCpy $PassiveMode 0
  StrCpy $UpdateMode 0
  StrCpy $DeleteDataState 0
  ${GetOptions} $CMDLINE "/P" $0
  ${IfNot} ${Errors}
    StrCpy $PassiveMode 1
  ${EndIf}
  ${GetOptions} $CMDLINE "/UPDATE" $0
  ${IfNot} ${Errors}
    StrCpy $UpdateMode 1
  ${EndIf}

  ; Installed for everyone when the machine-wide entry points at this folder.
  StrCpy $AllUsers 0
  ReadRegStr $0 HKLM "${MANUPRODUCTKEY}" ""
  ${If} $0 == $INSTDIR
    StrCpy $AllUsers 1
  ${EndIf}
  ${If} $AllUsers = 1
    SetShellVarContext all
    !insertmacro IsAdmin $0
    ${If} $0 != "Admin"
    ${AndIfNot} ${Silent}
      ClearErrors
      ExecShell "runas" "$INSTDIR\uninstall.exe" ""
      ${IfNot} ${Errors}
        Quit
      ${EndIf}
    ${EndIf}
  ${Else}
    SetShellVarContext current
  ${EndIf}
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

Section Uninstall
  Call un.CloseRunningApp

  ${If} $UpdateMode <> 1
    !insertmacro KlPathEdit 0
  ${EndIf}

  Delete "$INSTDIR\${MAINBINARYNAME}.exe"
  ${If} ${FileExists} "$INSTDIR\server\${SERVEREXE}"
    RMDir /r "$INSTDIR\server"
  ${EndIf}
  Delete "$INSTDIR\bin\klens.exe"
  Delete "$INSTDIR\bin\kopialens-cli.exe"
  RMDir "$INSTDIR\bin"
  Delete "$INSTDIR\icons\KopiaLens *.ico"
  RMDir "$INSTDIR\icons"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  ${If} $UpdateMode <> 1
    !insertmacro DeleteAppUserModelId
    !insertmacro IsShortcutTarget "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${If} $0 = 1
      !insertmacro UnpinShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk"
      Delete "$SMPROGRAMS\${PRODUCTNAME}.lnk"
    ${EndIf}
    !insertmacro IsShortcutTarget "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${If} $0 = 1
      !insertmacro UnpinShortcut "$DESKTOP\${PRODUCTNAME}.lnk"
      Delete "$DESKTOP\${PRODUCTNAME}.lnk"
    ${EndIf}

    ; Only our own Start at logon entry (another copy of KopiaLens may own the name).
    ReadRegStr $0 HKCU "${RUNKEY}" "${PRODUCTNAME}"
    ${WordFind} $0 "$INSTDIR\${MAINBINARYNAME}.exe" "E+1{" $1
    ${IfNot} ${Errors}
      DeleteRegValue HKCU "${RUNKEY}" "${PRODUCTNAME}"
      DeleteRegValue HKCU "${STARTUPAPPROVEDKEY}" "${PRODUCTNAME}"
    ${EndIf}

    DeleteRegKey SHCTX "${UNINSTKEY}"
    DeleteRegKey SHCTX "${MANUPRODUCTKEY}"
    DeleteRegKey /ifempty SHCTX "${MANUKEY}"
  ${EndIf}

  ${If} $DeleteDataState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    RMDir /r "$LOCALAPPDATA\${PRODUCTNAME}"
    RMDir /r "$APPDATA\${BUNDLEID}"
    RMDir /r "$LOCALAPPDATA\${BUNDLEID}"
  ${EndIf}

  ${If} $PassiveMode = 1
  ${OrIf} $UpdateMode = 1
    SetAutoClose true
  ${EndIf}
SectionEnd
