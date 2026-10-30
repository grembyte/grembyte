# Install a Start menu shortcut for the current user that starts Hermes Hook
# from the project's virtual environment. Pin it to the taskbar from the Start
# menu (right click, "Pin to taskbar"); Windows does not allow scripts to pin.
# Remove it again with: install-windows-shortcut.ps1 -Uninstall
#
# Run with: powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$VenvDir = if ($env:HERMES_VENV) { $env:HERMES_VENV } else { Join-Path $ProjectDir ".venv" }
$ShortcutFile = Join-Path ([Environment]::GetFolderPath("Programs")) "Hermes Hook.lnk"
$IconFile = Join-Path $env:LOCALAPPDATA "hermes-hook\hermes-hook.ico"
# Has to match WINDOWS_APP_ID in hermes/main.py, so that the pinned shortcut
# and the running window share one taskbar button.
$AppId = "hermes-hook.win"

if ($Uninstall) {
    Remove-Item -Force -ErrorAction SilentlyContinue $ShortcutFile, $IconFile
    Write-Output "Removed $ShortcutFile"
    exit 0
}

$Pythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    Write-Error "No virtual environment at $VenvDir (see readme.md, Installation)."
}

# Shortcuts cannot use the SVG icon, so render it to an .ico with Qt.
New-Item -ItemType Directory -Force (Split-Path $IconFile) | Out-Null
$env:QT_QPA_PLATFORM = "offscreen"
& (Join-Path $VenvDir "Scripts\python.exe") -c @"
import sys
from PyQt6.QtGui import QGuiApplication, QIcon
app = QGuiApplication(sys.argv)
image = QIcon(sys.argv[1]).pixmap(256, 256).toImage()
sys.exit(0 if image.save(sys.argv[2], 'ICO') else 1)
"@ (Join-Path $ProjectDir "hermes\ui\resources\favicon.svg") $IconFile
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Cannot create $IconFile, the shortcut uses the Python icon."
    $IconFile = $Pythonw
}

# The shortcut starts pythonw.exe directly instead of hermes-hook.cmd, so no
# console window flashes up.
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutFile)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "`"$(Join-Path $ProjectDir 'main.py')`""
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.IconLocation = "$IconFile,0"
$Shortcut.Description = "Memory scanner, hook engine and reverse engineering tool"
$Shortcut.Save()

# WScript.Shell cannot set the app ID of a shortcut, so write the property
# through IPropertyStore.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class ShortcutAppId
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink { }

    [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPersistFile
    {
        void GetClassID(out Guid classId);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string fileName, uint mode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string fileName, bool remember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string fileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string fileName);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PropertyKey
    {
        public Guid FormatId;
        public uint PropertyId;
    }

    [StructLayout(LayoutKind.Explicit)]
    struct PropVariant
    {
        [FieldOffset(0)] public ushort VarType;
        [FieldOffset(8)] public IntPtr Value;
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    public static void Set(string shortcutFile, string appId)
    {
        object link = new ShellLink();
        try
        {
            ((IPersistFile)link).Load(shortcutFile, 2);  // STGM_READWRITE
            // PKEY_AppUserModel_ID
            PropertyKey key = new PropertyKey
            {
                FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
                PropertyId = 5,
            };
            PropVariant value = new PropVariant
            {
                VarType = 31,  // VT_LPWSTR
                Value = Marshal.StringToCoTaskMemUni(appId),
            };
            try
            {
                IPropertyStore store = (IPropertyStore)link;
                store.SetValue(ref key, ref value);
                store.Commit();
            }
            finally
            {
                Marshal.FreeCoTaskMem(value.Value);
            }
            ((IPersistFile)link).Save(shortcutFile, true);
        }
        finally
        {
            Marshal.ReleaseComObject(link);
        }
    }
}
"@
[ShortcutAppId]::Set($ShortcutFile, $AppId)

Write-Output "Installed $ShortcutFile"
Write-Output "Pin it from the Start menu: right click 'Hermes Hook', 'Pin to taskbar'."
