# Find which process holds a lock on the SQLite journal file.
# Uses the RestartManager API which is built into Windows Vista+.
# Run from an elevated (Administrator) PowerShell.

$targetFile = "E:\Programacion\magit\data\magitrader.db-journal"

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class RestartManager {
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, string strSessionKey);
    [DllImport("rstrtmgr.dll")]
    static extern int RmEndSession(uint pSessionHandle);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, string[] rgsFilenames,
        uint nApplications, [In] RM_UNIQUE_APPLICATION[] rgApplications, uint nServices, string[] rgsServiceNames);
    [DllImport("rstrtmgr.dll")]
    static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo,
        [In, Out] RM_PROCESS_INFO[] rgAffectedApps, ref uint lpdwRebootReasons);

    [StructLayout(LayoutKind.Sequential)]
    public struct RM_UNIQUE_APPLICATION { public string strAppName; public RM_APP_TYPE ApplicationType; public uint AppStatus; public uint TSSessionId; [MarshalAs(UnmanagedType.Bool)] public bool bRestartable; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct RM_PROCESS_INFO { public RM_UNIQUE_PROCESS Process; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string strAppName; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string strServiceShortName; public RM_APP_TYPE ApplicationType; public uint AppStatus; public uint TSSessionId; [MarshalAs(UnmanagedType.Bool)] public bool bRestartable; }
    [StructLayout(LayoutKind.Sequential)]
    public struct RM_UNIQUE_PROCESS { public int dwProcessId; public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime; }
    public enum RM_APP_TYPE { RmUnknownApp = 0, RmMainWindow = 1, RmOtherWindow = 2, RmService = 3, RmExplorer = 4, RmConsole = 5, RmCritical = 1000 }

    public static List<int> GetLockingProcessIds(string path) {
        var result = new List<int>();
        uint handle; string key = Guid.NewGuid().ToString();
        if (RmStartSession(out handle, 0, key) != 0) return result;
        try {
            if (RmRegisterResources(handle, 1, new[] { path }, 0, null, 0, null) != 0) return result;
            uint needed = 0, count = 0; uint reasons = 0;
            RmGetList(handle, out needed, ref count, null, ref reasons);
            if (needed == 0) return result;
            var infos = new RM_PROCESS_INFO[needed]; count = needed;
            if (RmGetList(handle, out needed, ref count, infos, ref reasons) != 0) return result;
            for (int i = 0; i < count; i++) result.Add(infos[i].Process.dwProcessId);
        } finally { RmEndSession(handle); }
        return result;
    }
}
"@

$pids = [RestartManager]::GetLockingProcessIds($targetFile)
if ($pids.Count -eq 0) {
    Write-Host "No processes found locking the file (may need elevation)."
} else {
    foreach ($procId in $pids) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        Write-Host "PID $procId — $($proc.ProcessName) — $($proc.Path)"
        Write-Host "  Kill it? Run: Stop-Process -Id $procId -Force"
    }
}
