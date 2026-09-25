#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import plistlib
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

STDOUT_LOCK = threading.Lock()
JOBS_LOCK = threading.RLock()


def emit(event: str, **data: Any) -> None:
    payload = json.dumps({"event": event, "data": data}, separators=(",", ":"), ensure_ascii=False)
    with STDOUT_LOCK:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def reply(payload: dict[str, Any]) -> None:
    with STDOUT_LOCK:
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
        sys.stdout.flush()


def ok(message: str = "", **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **extra}


def unavailable(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "available": False, "message": message, **extra}


def powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


def adb_path() -> str | None:
    return shutil.which("adb")


def command_path(name: str) -> str | None:
    return shutil.which(name)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, operation: str, params: dict[str, Any]):
        self.id = uuid.uuid4().hex[:16]
        self.operation = operation
        self.params = params
        self.status = "queued"
        self.progress = 0.0
        self.message = "Queued"
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancel = threading.Event()
        self.process: subprocess.Popen[str] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "status": self.status,
            "progress": round(float(self.progress), 2),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobEngine:
    def __init__(self, workers: int = 2):
        self.jobs: dict[str, Job] = {}
        self.q: queue.Queue[Job | None] = queue.Queue()
        self.workers: list[threading.Thread] = []
        for i in range(max(1, workers)):
            t = threading.Thread(target=self._worker, name=f"insync-worker-{i+1}", daemon=True)
            t.start()
            self.workers.append(t)

    def submit(self, operation: str, params: dict[str, Any]) -> Job:
        if operation not in OPERATIONS:
            raise ValueError(f"unsupported operation: {operation}")
        job = Job(operation, params)
        with JOBS_LOCK:
            self.jobs[job.id] = job
        self.q.put(job)
        emit("job.queued", job=job.snapshot())
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        with JOBS_LOCK:
            job = self.jobs.get(job_id)
        if not job:
            return unavailable("Job not found", job_id=job_id)
        job.cancel.set()
        proc = job.process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        return ok("Cancellation requested", job=job.snapshot())

    def status(self, job_id: str) -> dict[str, Any]:
        with JOBS_LOCK:
            job = self.jobs.get(job_id)
        if not job:
            return unavailable("Job not found", job_id=job_id)
        return ok(job=job.snapshot())

    def list(self) -> dict[str, Any]:
        with JOBS_LOCK:
            items = [j.snapshot() for j in sorted(self.jobs.values(), key=lambda x: x.created_at, reverse=True)[:50]]
        return ok(jobs=items)

    def progress(self, job: Job, value: float, message: str) -> None:
        job.progress = max(0.0, min(100.0, float(value)))
        job.message = message
        emit("job.progress", job=job.snapshot())

    def _worker(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                return
            try:
                if job.cancel.is_set():
                    raise Cancelled()
                job.status = "running"
                job.started_at = time.time()
                self.progress(job, 0, "Starting")
                result = OPERATIONS[job.operation](job, self)
                if job.cancel.is_set():
                    raise Cancelled()
                job.result = result
                job.progress = 100.0
                job.status = "completed" if result.get("ok", False) else "failed"
                job.message = str(result.get("message") or ("Completed" if job.status == "completed" else "Failed"))
            except Cancelled:
                job.status = "cancelled"
                job.message = "Cancelled"
                job.error = None
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = str(exc)
            finally:
                job.finished_at = time.time()
                job.process = None
                emit("job.finished", job=job.snapshot())
                self.q.task_done()


ENGINE = JobEngine(workers=2)


def run_process(
    job: Job,
    cmd: list[str],
    *,
    timeout: float = 60,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        shell=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    job.process = proc
    started = time.time()
    while proc.poll() is None:
        if job.cancel.is_set():
            try:
                proc.terminate()
            finally:
                raise Cancelled()
        if time.time() - started > timeout:
            try:
                proc.kill()
            finally:
                raise TimeoutError(f"Command timed out after {timeout:g}s")
        time.sleep(0.1)
    out, err = proc.communicate()
    return proc.returncode, out.strip(), err.strip()


def run_quick(cmd: list[str], timeout: float = 20) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def ps_json(script: str, timeout: float = 20) -> dict[str, Any]:
    ps = powershell()
    if not ps:
        return unavailable("PowerShell is not available")
    rc, out, err = run_quick([ps, "-NoProfile", "-Command", script], timeout=timeout)
    if rc != 0:
        return unavailable(err or out or f"PowerShell exited {rc}")
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if not lines:
        return unavailable("PowerShell returned no data")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return unavailable("PowerShell returned invalid JSON", raw=out[-2000:])


SHARING_STATUS_PS = r'''
$ErrorActionPreference='SilentlyContinue'
$eth=Get-NetIPConfiguration -InterfaceAlias 'Ethernet'
$wifi=Get-NetIPConfiguration -InterfaceAlias 'Wi-Fi'
$svc=Get-Service SharedAccess
$flags=Get-CimInstance -Namespace 'root/Microsoft/HomeNet' -ClassName 'HNet_ConnectionProperties' |
  Where-Object {$_.IsIcsPublic -or $_.IsIcsPrivate} |
  ForEach-Object {
    $guid=''
    if([string]$_.Connection -match '\{[0-9A-Fa-f-]+\}'){$guid=$Matches[0]}
    [pscustomobject]@{Guid=$guid;Public=[bool]$_.IsIcsPublic;Private=[bool]$_.IsIcsPrivate}
  }
$adapters=Get-NetAdapter -IncludeHidden | Select-Object Name,InterfaceGuid,Status
$merged=@()
foreach($f in $flags){
  $needle=([string]$f.Guid).Trim('{','}')
  $a=$adapters | Where-Object {([string]$_.InterfaceGuid).Trim('{','}') -ieq $needle} | Select-Object -First 1
  $merged += [pscustomobject]@{Name=$a.Name;Guid=$f.Guid;Public=$f.Public;Private=$f.Private;Status=$a.Status}
}
[pscustomobject]@{
  ok=$true
  available=$true
  service=[string]$svc.Status
  ethernetIPv4=[string](($eth.IPv4Address.IPAddress)-join ',')
  ethernetStatus=[string](Get-NetAdapter -Name 'Ethernet').Status
  ethernetSpeed=[string](Get-NetAdapter -Name 'Ethernet').LinkSpeed
  wifiIPv4=[string](($wifi.IPv4Address.IPAddress)-join ',')
  wifiGateway=[string](($wifi.IPv4DefaultGateway.NextHop)-join ',')
  flags=$merged
} | ConvertTo-Json -Compress -Depth 6
'''


def sharing_status(_: dict[str, Any] | None = None) -> dict[str, Any]:
    if sys.platform != "win32":
        return unavailable("Windows Internet sharing is only available on Windows", platform=sys.platform)
    result = ps_json(SHARING_STATUS_PS, timeout=20)
    if not result.get("ok"):
        return result
    flags = result.get("flags") or []
    public = next((x for x in flags if x.get("Public")), None)
    private = next((x for x in flags if x.get("Private")), None)
    running = str(result.get("service", "")).lower() == "running"
    if running and public and private:
        connection = "sending"
        message = f"Sharing {public.get('Name') or 'uplink'} → {private.get('Name') or 'private adapter'}"
    else:
        connection = "disconnected"
        message = "Internet sharing is off"
    result.update({"connection": connection, "message": message})
    return result


def _local_state_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    p = base / "THETECHGUY Digital Solutions" / "iNSync" / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_sharing_script(enable: bool, public_name: str, private_name: str) -> tuple[Path, Path]:
    state_dir = _local_state_dir()
    token = uuid.uuid4().hex
    script_path = state_dir / f"sharing-{token}.ps1"
    result_path = state_dir / f"sharing-{token}.json"
    baseline_path = state_dir / "sharing-baseline.json"
    mode = "$true" if enable else "$false"
    # Native HNetCfg + HomeNet recovery. The UI worker waits; Electron remains responsive.
    script = rf'''$ErrorActionPreference='Stop'
$enable={mode}
$publicName={json.dumps(public_name)}
$privateName={json.dumps(private_name)}
$scope='192.168.250.1'
$resultPath={json.dumps(str(result_path))}
$baselinePath={json.dumps(str(baseline_path))}
function Save-Result($obj){{$obj|ConvertTo-Json -Compress -Depth 8|Set-Content -LiteralPath $resultPath -Encoding UTF8}}
try {{
  $id=[Security.Principal.WindowsIdentity]::GetCurrent()
  $principal=New-Object Security.Principal.WindowsPrincipal($id)
  if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){{throw 'Administrator token required'}}

  $mgr=New-Object -ComObject HNetCfg.HNetShare.1
  $connections=@{{}}
  foreach($c in $mgr.EnumEveryConnection){{
    $props=$mgr.NetConnectionProps($c)
    $cfg=$mgr.INetSharingConfigurationForINetConnection($c)
    $connections[$props.Name]=[pscustomobject]@{{Cfg=$cfg;Props=$props}}
  }}
  if(-not $connections.ContainsKey($publicName)){{throw "Missing public adapter: $publicName"}}
  if(-not $connections.ContainsKey($privateName)){{throw "Missing private adapter: $privateName"}}

  if($enable){{
    if(-not (Test-Path -LiteralPath $baselinePath)){{
      $if=Get-NetIPInterface -InterfaceAlias $privateName -AddressFamily IPv4
      $ip=Get-NetIPConfiguration -InterfaceAlias $privateName
      $reg='HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters'
      $scopeObj=Get-ItemProperty -Path $reg
      [pscustomobject]@{{
        privateName=$privateName
        dhcp=[string]$if.Dhcp
        addresses=@($ip.IPv4Address | ForEach-Object {{[pscustomobject]@{{ip=$_.IPAddress;prefix=$_.PrefixLength}}}})
        gateway=[string](($ip.IPv4DefaultGateway.NextHop)-join ',')
        dns=@((Get-DnsClientServerAddress -InterfaceAlias $privateName -AddressFamily IPv4).ServerAddresses)
        scopeAddress=[string]$scopeObj.ScopeAddress
        scopeBackup=[string]$scopeObj.ScopeAddressBackup
      }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $baselinePath -Encoding UTF8
    }}

    # Clear stale flags first (Microsoft ICS recovery pattern).
    Get-WmiObject -Namespace 'root/Microsoft/HomeNet' -Class 'HNet_ConnectionProperties' | ForEach-Object {{
      if($_.IsIcsPublic -or $_.IsIcsPrivate){{
        $_.IsIcsPublic=$false; $_.IsIcsPrivate=$false; [void]$_.Put()
      }}
    }}
    foreach($name in $connections.Keys){{try{{$connections[$name].Cfg.DisableSharing()}}catch{{}}}}
    $reg='HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters'
    Set-ItemProperty -Path $reg -Name ScopeAddress -Type String -Value $scope
    Set-ItemProperty -Path $reg -Name ScopeAddressBackup -Type String -Value $scope
    Set-NetIPInterface -InterfaceAlias $privateName -AddressFamily IPv4 -Dhcp Disabled
    Get-NetIPAddress -InterfaceAlias $privateName -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object {{$_.IPAddress -ne $scope}} | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    if(-not (Get-NetIPAddress -InterfaceAlias $privateName -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {{$_.IPAddress -eq $scope}})){{
      New-NetIPAddress -InterfaceAlias $privateName -IPAddress $scope -PrefixLength 24 | Out-Null
    }}
    Stop-Service SharedAccess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 600
    Start-Service SharedAccess
    Start-Sleep -Seconds 1
    $mgr=$null; [gc]::Collect(); [gc]::WaitForPendingFinalizers()
    $mgr=New-Object -ComObject HNetCfg.HNetShare.1
    $connections=@{{}}
    foreach($c in $mgr.EnumEveryConnection){{
      $props=$mgr.NetConnectionProps($c); $cfg=$mgr.INetSharingConfigurationForINetConnection($c)
      $connections[$props.Name]=[pscustomobject]@{{Cfg=$cfg;Props=$props}}
    }}
    $connections[$publicName].Cfg.EnableSharing(0)
    Start-Sleep -Milliseconds 500
    $connections[$privateName].Cfg.EnableSharing(1)
    Start-Sleep -Seconds 2
  }} else {{
    Get-WmiObject -Namespace 'root/Microsoft/HomeNet' -Class 'HNet_ConnectionProperties' | ForEach-Object {{
      if($_.IsIcsPublic -or $_.IsIcsPrivate){{
        $_.IsIcsPublic=$false; $_.IsIcsPrivate=$false; [void]$_.Put()
      }}
    }}
    foreach($name in $connections.Keys){{try{{$connections[$name].Cfg.DisableSharing()}}catch{{}}}}
    Stop-Service SharedAccess -Force -ErrorAction SilentlyContinue
    if(Test-Path -LiteralPath $baselinePath){{
      $b=Get-Content -Raw -LiteralPath $baselinePath | ConvertFrom-Json
      Get-NetIPAddress -InterfaceAlias $privateName -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
      if([string]$b.dhcp -eq 'Enabled'){{
        Set-NetIPInterface -InterfaceAlias $privateName -AddressFamily IPv4 -Dhcp Enabled
        Set-DnsClientServerAddress -InterfaceAlias $privateName -ResetServerAddresses -ErrorAction SilentlyContinue
      }} else {{
        Set-NetIPInterface -InterfaceAlias $privateName -AddressFamily IPv4 -Dhcp Disabled
        foreach($a in @($b.addresses)){{
          if($a.ip -and $a.ip -notlike '169.254.*'){{
            New-NetIPAddress -InterfaceAlias $privateName -IPAddress $a.ip -PrefixLength ([int]$a.prefix) -ErrorAction SilentlyContinue | Out-Null
          }}
        }}
        if(@($b.dns).Count -gt 0){{Set-DnsClientServerAddress -InterfaceAlias $privateName -ServerAddresses @($b.dns) -ErrorAction SilentlyContinue}}
      }}
      $reg='HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters'
      if($b.scopeAddress){{Set-ItemProperty -Path $reg -Name ScopeAddress -Type String -Value ([string]$b.scopeAddress)}}
      if($b.scopeBackup){{Set-ItemProperty -Path $reg -Name ScopeAddressBackup -Type String -Value ([string]$b.scopeBackup)}}
      Remove-Item -LiteralPath $baselinePath -Force -ErrorAction SilentlyContinue
    }} else {{
      Set-NetIPInterface -InterfaceAlias $privateName -AddressFamily IPv4 -Dhcp Enabled
      Set-DnsClientServerAddress -InterfaceAlias $privateName -ResetServerAddresses -ErrorAction SilentlyContinue
    }}
  }}
  Save-Result ([pscustomobject]@{{ok=$true;enabled=$enable;message=if($enable){{"Internet sharing enabled"}}else{{"Normal networking restored"}}}})
  exit 0
}} catch {{
  Save-Result ([pscustomobject]@{{ok=$false;enabled=$enable;message=[string]$_;detail=($_|Out-String)}})
  exit 1
}}
'''
    script_path.write_text(script, encoding="utf-8-sig")
    return script_path, result_path


def sharing_toggle(job: Job, engine: JobEngine) -> dict[str, Any]:
    if sys.platform != "win32":
        return unavailable("Internet sharing toggle is Windows-only", platform=sys.platform)
    enable = bool(job.params.get("enable", True))
    public_name = str(job.params.get("public") or "Wi-Fi")
    private_name = str(job.params.get("private") or "Ethernet")
    ps = powershell()
    if not ps:
        return unavailable("PowerShell is unavailable")
    engine.progress(job, 5, "Preparing network transition")
    script_path, result_path = _write_sharing_script(enable, public_name, private_name)
    try:
        # Run an elevated child PowerShell. This may show the normal Windows UAC prompt.
        launch = (
            "$p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -Wait "
            f"-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',{json.dumps(str(script_path))}); "
            "exit $p.ExitCode"
        )
        engine.progress(job, 15, "Waiting for Windows approval")
        rc, out, err = run_process(job, [ps, "-NoProfile", "-Command", launch], timeout=180)
        engine.progress(job, 80, "Verifying sharing state")
        result: dict[str, Any]
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        else:
            result = unavailable(err or out or f"Sharing helper exited {rc}")
        status = sharing_status()
        result["status"] = status
        if status.get("connection"):
            emit("sharing.state", connection=status["connection"], status=status)
        return result
    finally:
        for p in (script_path, result_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def sharing_status_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    engine.progress(job, 20, "Reading Windows sharing state")
    result = sharing_status()
    if result.get("connection"):
        emit("sharing.state", connection=result["connection"], status=result)
    engine.progress(job, 90, result.get("message", "Sharing state read"))
    return result


def adb_devices_data(job: Job | None = None) -> dict[str, Any]:
    adb = adb_path()
    if not adb:
        return unavailable("ADB is not installed or not on PATH", devices=[])
    if job:
        rc, out, err = run_process(job, [adb, "devices", "-l"], timeout=20)
    else:
        rc, out, err = run_quick([adb, "devices", "-l"], timeout=20)
    devices = []
    if rc == 0:
        for line in out.splitlines()[1:]:
            if not line.strip():
                continue
            parts = line.split()
            devices.append({
                "serial": parts[0],
                "state": parts[1] if len(parts) > 1 else "unknown",
                "detail": " ".join(parts[2:]),
            })
    return {"ok": rc == 0, "available": True, "devices": devices, "message": f"{len(devices)} ADB device(s)", "error": err if rc else ""}


def adb_devices_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    engine.progress(job, 20, "Refreshing ADB devices")
    return adb_devices_data(job)


def _adb_prefix(serial: str) -> list[str]:
    adb = adb_path()
    if not adb:
        raise FileNotFoundError("ADB is not installed or not on PATH")
    return [adb] + (["-s", serial] if serial else [])


def adb_info_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    serial = str(job.params.get("serial") or "")
    engine.progress(job, 10, "Reading Android device information")
    prefix = _adb_prefix(serial)
    fields: dict[str, str] = {}
    for key, command in (
        ("model", ["shell", "getprop", "ro.product.model"]),
        ("manufacturer", ["shell", "getprop", "ro.product.manufacturer"]),
        ("android", ["shell", "getprop", "ro.build.version.release"]),
        ("sdk", ["shell", "getprop", "ro.build.version.sdk"]),
    ):
        rc, out, _ = run_process(job, prefix + command, timeout=20)
        if rc == 0:
            fields[key] = out.strip()
    rc, out, err = run_process(job, prefix + ["shell", "df", "-k", "/data"], timeout=20)
    storage = {}
    if rc == 0:
        lines = [line.split() for line in out.splitlines() if line.strip()]
        if len(lines) >= 2 and len(lines[-1]) >= 4:
            row = lines[-1]
            try:
                total = int(row[1]) * 1024
                used = int(row[2]) * 1024
                free = int(row[3]) * 1024
                storage = {"total": total, "used": used, "free": free}
            except ValueError:
                pass
    return ok(
        "Android device information refreshed",
        serial=serial,
        device=fields,
        storage=storage,
        error=err if rc else "",
    )


def _package_lines(out: str) -> list[str]:
    return sorted({
        line.split("package:", 1)[-1].strip()
        for line in out.splitlines()
        if line.strip().startswith("package:")
    })


def adb_apps_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    serial = str(job.params.get("serial") or "")
    prefix = _adb_prefix(serial)
    engine.progress(job, 10, "Reading user applications")
    rc_user, out_user, err_user = run_process(job, prefix + ["shell", "pm", "list", "packages", "-3"], timeout=35)
    engine.progress(job, 55, "Reading system applications")
    rc_sys, out_sys, err_sys = run_process(job, prefix + ["shell", "pm", "list", "packages", "-s"], timeout=35)
    user = _package_lines(out_user) if rc_user == 0 else []
    system = _package_lines(out_sys) if rc_sys == 0 else []
    apps = (
        [{"package": package, "kind": "user", "action": "uninstall"} for package in user]
        + [{"package": package, "kind": "system", "action": "disable"} for package in system]
    )
    return {
        "ok": rc_user == 0 or rc_sys == 0,
        "available": True,
        "apps": apps,
        "counts": {"user": len(user), "system": len(system)},
        "message": f"{len(user)} user / {len(system)} system app(s)",
        "error": "\n".join(x for x in (err_user if rc_user else "", err_sys if rc_sys else "") if x),
    }


def _expand_android_packages(paths: list[Path]) -> tuple[list[Path], tempfile.TemporaryDirectory[str] | None]:
    if len(paths) != 1 or paths[0].suffix.lower() not in {".apks", ".xapk"}:
        return paths, None
    archive = paths[0]
    temp = tempfile.TemporaryDirectory(prefix="insync-apk-")
    target = Path(temp.name)
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".apk") and not name.endswith("/"):
                zf.extract(name, target)
    expanded = sorted(target.rglob("*.apk"))
    if not expanded:
        temp.cleanup()
        raise ValueError(f"{archive.name} contains no APK files")
    return expanded, temp


def adb_install_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    raw = job.params.get("paths") or ([job.params.get("path")] if job.params.get("path") else [])
    paths = [Path(str(value)) for value in raw if value]
    if not paths or any(not path.is_file() for path in paths):
        return unavailable("Choose an APK, APKS/XAPK bundle, or split APK set before installing")
    serial = str(job.params.get("serial") or "")
    engine.progress(job, 8, "Preparing Android package")
    expanded, temp = _expand_android_packages(paths)
    try:
        command = _adb_prefix(serial)
        if len(expanded) == 1:
            command += ["install", "-r", str(expanded[0])]
        else:
            command += ["install-multiple", "-r", *[str(path) for path in expanded]]
        engine.progress(job, 22, f"Installing {len(expanded)} APK file(s) through ADB")
        rc, out, err = run_process(job, command, timeout=600)
        return {
            "ok": rc == 0,
            "available": True,
            "message": out or err or ("Installed" if rc == 0 else "Install failed"),
            "paths": [str(path) for path in paths],
            "apk_count": len(expanded),
        }
    finally:
        if temp is not None:
            temp.cleanup()


def adb_uninstall_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    package = str(job.params.get("package") or "").strip()
    if not package:
        return unavailable("Choose an app before uninstalling")
    serial = str(job.params.get("serial") or "")
    engine.progress(job, 20, f"Uninstalling {package}")
    rc, out, err = run_process(job, _adb_prefix(serial) + ["uninstall", package], timeout=90)
    return {"ok": rc == 0, "available": True, "message": out or err or ("Uninstalled" if rc == 0 else "Uninstall failed"), "package": package}


def adb_disable_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    package = str(job.params.get("package") or "").strip()
    if not package:
        return unavailable("Choose a system app before disabling")
    serial = str(job.params.get("serial") or "")
    engine.progress(job, 20, f"Disabling {package} for user 0")
    rc, out, err = run_process(
        job,
        _adb_prefix(serial) + ["shell", "pm", "disable-user", "--user", "0", package],
        timeout=90,
    )
    return {"ok": rc == 0, "available": True, "message": out or err or ("Disabled" if rc == 0 else "Disable failed"), "package": package}


def _collect_copy_plan(sources: list[Path], destination: Path) -> tuple[list[tuple[Path, Path, int]], int]:
    plan: list[tuple[Path, Path, int]] = []
    total = 0
    for src in sources:
        if not src.exists():
            raise FileNotFoundError(str(src))
        if src.is_file():
            size = src.stat().st_size
            target = destination / src.name
            plan.append((src, target, size))
            total += size
        else:
            root_target = destination / src.name
            for p in src.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(src)
                    size = p.stat().st_size
                    plan.append((p, root_target / rel, size))
                    total += size
    return plan, total


def files_copy_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    sources = [Path(str(x)) for x in (job.params.get("sources") or [])]
    destination = Path(str(job.params.get("destination") or ""))
    if not sources:
        return unavailable("Choose one or more files/folders")
    if not destination:
        return unavailable("Choose a destination folder")
    destination.mkdir(parents=True, exist_ok=True)
    engine.progress(job, 2, "Scanning files")
    plan, total = _collect_copy_plan(sources, destination)
    copied = 0
    last_emit = 0.0
    for src, dst, size in plan:
        if job.cancel.is_set():
            raise Cancelled()
        dst.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as rf, dst.open("wb") as wf:
            while True:
                if job.cancel.is_set():
                    raise Cancelled()
                chunk = rf.read(1024 * 1024)
                if not chunk:
                    break
                wf.write(chunk)
                copied += len(chunk)
                now = time.time()
                if total and now - last_emit >= 0.12:
                    engine.progress(job, (copied / total) * 100.0, f"Copying {src.name}")
                    last_emit = now
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass
    return ok(f"Transferred {len(plan)} file(s)", files=len(plan), bytes=copied, destination=str(destination))


def clipboard_send_text_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    text = str(job.params.get("text") or "")
    targets = list(job.params.get("targets") or [])
    if not text:
        return unavailable("Clipboard text is empty")
    if not targets:
        return unavailable("Choose at least one approved iNSync peer")
    return unavailable("Peer clipboard transport is not qualified yet", payload="text", targets=targets)


def clipboard_send_image_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    data_url = str(job.params.get("data_url") or "")
    targets = list(job.params.get("targets") or [])
    if not data_url:
        return unavailable("Clipboard image is empty")
    if not targets:
        return unavailable("Choose at least one approved iNSync peer")
    return unavailable("Peer clipboard transport is not qualified yet", payload="image", targets=targets)


PEER_PORT = 49549
PEER_MAGIC = b"INSYNC_DISCOVER_V1"


def _peer_config_path() -> Path:
    return _local_state_dir() / "peer-config.json"


def _load_peer_config() -> dict[str, Any]:
    path = _peer_config_path()
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    value = {
        "peer_id": uuid.uuid4().hex,
        "role": "idle",
        "scope": "internet-only",
        "shared_paths": [],
        "approved_ids": [],
    }
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return value


def _save_peer_config(value: dict[str, Any]) -> None:
    _peer_config_path().write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class PeerDiscovery:
    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._serve, name="insync-peer-discovery", daemon=True)
        self._thread.start()

    @staticmethod
    def _record(ip: str = "") -> dict[str, Any]:
        config = _load_peer_config()
        return {
            "id": config["peer_id"],
            "name": socket.gethostname(),
            "host": socket.gethostname(),
            "ip": ip,
            "role": config.get("role", "idle"),
            "scope": config.get("scope", "internet-only"),
            "shared_paths": config.get("shared_paths", []),
            "approved": True,
            "self": True,
            "port": PEER_PORT,
        }

    def _serve(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", PEER_PORT))
            while True:
                try:
                    data, address = sock.recvfrom(8192)
                except OSError:
                    time.sleep(0.5)
                    continue
                if data.strip() != PEER_MAGIC:
                    continue
                payload = json.dumps(self._record(address[0]), separators=(",", ":")).encode("utf-8")
                try:
                    sock.sendto(payload, address)
                except OSError:
                    pass
        except OSError:
            return
        finally:
            sock.close()

    def discover(self, timeout: float = 0.8) -> list[dict[str, Any]]:
        config = _load_peer_config()
        approved = set(str(x) for x in config.get("approved_ids", []))
        peers: dict[str, dict[str, Any]] = {}
        self_record = self._record("127.0.0.1")
        peers[self_record["id"]] = self_record
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", 0))
            sock.settimeout(0.12)
            for target in (("255.255.255.255", PEER_PORT), ("127.0.0.1", PEER_PORT)):
                try:
                    sock.sendto(PEER_MAGIC, target)
                except OSError:
                    pass
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, address = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    peer = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                peer_id = str(peer.get("id") or "")
                if not peer_id:
                    continue
                peer["ip"] = address[0]
                peer["self"] = peer_id == config["peer_id"]
                peer["approved"] = peer["self"] or peer_id in approved
                peers[peer_id] = peer
        finally:
            sock.close()
        return sorted(peers.values(), key=lambda item: (not item.get("self", False), str(item.get("name", ""))))


PEER_DISCOVERY = PeerDiscovery()


def peers_list_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    engine.progress(job, 15, "Discovering iNSync peers")
    peers = PEER_DISCOVERY.discover()
    remote = [peer for peer in peers if not peer.get("self")]
    return ok(f"{len(remote)} remote iNSync peer(s) found", peers=peers)


def peer_configure_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    role = str(job.params.get("role") or "idle").lower()
    scope = str(job.params.get("scope") or "internet-only").lower()
    if role not in {"provider", "receiver", "idle"}:
        return unavailable("Role must be provider, receiver or idle")
    if scope not in {"internet-only", "internet-folders", "selected-location", "whole-pc"}:
        return unavailable("Unknown PC share scope")
    shared_paths = [str(Path(str(value))) for value in (job.params.get("shared_paths") or []) if value]
    config = _load_peer_config()
    config.update({"role": role, "scope": scope, "shared_paths": shared_paths})
    _save_peer_config(config)
    emit("peer.state", peer=PEER_DISCOVERY._record("127.0.0.1"))
    return ok("PC share role applied", peer=PEER_DISCOVERY._record("127.0.0.1"))


def peer_approve_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    peer_id = str(job.params.get("peer_id") or "")
    approved = bool(job.params.get("approved", True))
    if not peer_id:
        return unavailable("Peer id required")
    config = _load_peer_config()
    values = set(str(x) for x in config.get("approved_ids", []))
    if approved:
        values.add(peer_id)
    else:
        values.discard(peer_id)
    config["approved_ids"] = sorted(values)
    _save_peer_config(config)
    return ok("Peer approval updated", peer_id=peer_id, approved=approved)


def _ipa_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        info_name = next(
            (name for name in names if name.startswith("Payload/") and name.endswith(".app/Info.plist")),
            None,
        )
        if not info_name:
            raise ValueError("IPA has no Payload/*.app/Info.plist")
        info = plistlib.loads(zf.read(info_name))
        app_root = info_name.rsplit("/", 1)[0] + "/"
        signed = any(name.startswith(app_root + "_CodeSignature/") for name in names)
        return {
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or path.stem,
            "bundle_id": info.get("CFBundleIdentifier") or "",
            "version": info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "",
            "minimum_os": info.get("MinimumOSVersion") or "",
            "signed": signed,
            "size": path.stat().st_size,
        }


def ios_validate_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    path = Path(str(job.params.get("path") or ""))
    if not path.is_file():
        return unavailable("Choose an IPA before validating")
    engine.progress(job, 20, "Reading IPA metadata")
    try:
        metadata = _ipa_metadata(path)
    except Exception as exc:
        return unavailable(f"IPA validation failed: {exc}")
    return ok(
        "IPA validated" if metadata.get("signed") else "IPA parsed; signing/provisioning still required",
        metadata=metadata,
        path=str(path),
    )


def _parse_key_value_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            values[key.strip()] = value.strip()
    return values


def ios_status_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    idevice_id = command_path("idevice_id")
    ideviceinfo = command_path("ideviceinfo")
    if not idevice_id:
        return unavailable("iOS device-service backend is not installed")
    rc, out, err = run_process(job, [idevice_id, "-l"], timeout=20)
    ids = [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []
    result: dict[str, Any] = {
        "ok": rc == 0,
        "available": True,
        "devices": ids,
        "message": f"{len(ids)} iOS device(s)",
        "error": err if rc else "",
    }
    if ids and ideviceinfo:
        engine.progress(job, 45, "Reading iPhone device status")
        rc2, info_out, info_err = run_process(job, [ideviceinfo, "-u", ids[0]], timeout=30)
        if rc2 == 0:
            info = _parse_key_value_lines(info_out)
            result["device"] = {
                "udid": ids[0],
                "name": info.get("DeviceName", ""),
                "product_type": info.get("ProductType", ""),
                "ios_version": info.get("ProductVersion", ""),
                "build": info.get("BuildVersion", ""),
            }
        elif info_err:
            result["detail"] = info_err
        rc3, disk_out, _ = run_process(job, [ideviceinfo, "-u", ids[0], "-q", "com.apple.disk_usage"], timeout=30)
        if rc3 == 0:
            disk = _parse_key_value_lines(disk_out)
            def number(key: str) -> int | None:
                try:
                    return int(disk.get(key, ""))
                except ValueError:
                    return None
            total = number("TotalDiskCapacity") or number("TotalDataCapacity")
            free = number("TotalDataAvailable")
            if total or free:
                result["storage"] = {"total": total, "free": free}
    return result


def ios_ipa_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    path = Path(str(job.params.get("path") or ""))
    if not path.is_file():
        return unavailable("Choose an IPA before installing")
    try:
        metadata = _ipa_metadata(path)
    except Exception as exc:
        return unavailable(f"IPA validation failed: {exc}")
    installer = command_path("ideviceinstaller")
    if not installer:
        return unavailable(
            "IPA is validated, but the install backend is not installed",
            metadata=metadata,
            path=str(path),
        )
    engine.progress(job, 20, "Installing IPA")
    rc, out, err = run_process(job, [installer, "-i", str(path)], timeout=600)
    return {
        "ok": rc == 0,
        "available": True,
        "message": out or err,
        "path": str(path),
        "metadata": metadata,
    }


def console_status_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    console = str(job.params.get("console") or "console")
    return unavailable(f"{console} adapter is not qualified yet", console=console)


def console_pkg_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    paths = [str(x) for x in (job.params.get("paths") or [])]
    console = str(job.params.get("console") or "PlayStation")
    if not paths:
        return unavailable("Choose one or more package files")
    return unavailable(f"{console} package backend is not qualified yet", console=console, packages=paths)


OPERATIONS: dict[str, Callable[[Job, JobEngine], dict[str, Any]]] = {
    "sharing.status": sharing_status_job,
    "sharing.toggle": sharing_toggle,
    "adb.devices": adb_devices_job,
    "adb.info": adb_info_job,
    "adb.apps": adb_apps_job,
    "adb.install": adb_install_job,
    "adb.uninstall": adb_uninstall_job,
    "adb.disable": adb_disable_job,
    "files.copy": files_copy_job,
    "peer.list": peers_list_job,
    "peer.configure": peer_configure_job,
    "peer.approve": peer_approve_job,
    "clipboard.text": clipboard_send_text_job,
    "clipboard.image": clipboard_send_image_job,
    "ios.status": ios_status_job,
    "ios.validate": ios_validate_job,
    "ios.ipa": ios_ipa_job,
    "console.status": console_status_job,
    "console.pkg": console_pkg_job,
}


def snapshot() -> dict[str, Any]:
    adb = adb_path()
    return ok(
        "iNSync engine ready",
        platform=sys.platform,
        engine={"workers": len(ENGINE.workers), "queued": ENGINE.q.qsize()},
        capabilities={
            "network_sharing": sys.platform == "win32",
            "file_copy": True,
            "adb": bool(adb),
            "ios": bool(command_path("idevice_id")),
            "ipa_validation": True,
            "peer_discovery": True,
            "peer_roles": True,
            "console_pkg": "adapter-pending",
            "clipboard_peer": "approval-transport-pending",
        },
    )


def direct_dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "app.snapshot":
        return snapshot()
    if method == "sharing.status":
        return sharing_status(params)
    if method == "adb.devices":
        return adb_devices_data()
    if method == "job.submit":
        operation = str(params.get("operation") or "")
        op_params = params.get("params") or {}
        if not isinstance(op_params, dict):
            raise ValueError("job params must be an object")
        job = ENGINE.submit(operation, op_params)
        return ok("Job queued", job=job.snapshot())
    if method == "job.cancel":
        return ENGINE.cancel(str(params.get("job_id") or ""))
    if method == "job.status":
        return ENGINE.status(str(params.get("job_id") or ""))
    if method == "job.list":
        return ENGINE.list()
    raise ValueError(f"unknown method: {method}")


def main() -> None:
    emit("engine.ready", snapshot=snapshot())
    for raw in sys.stdin:
        req: dict[str, Any] | None = None
        try:
            req = json.loads(raw)
            rid = req.get("id")
            method = str(req.get("method") or "")
            params = req.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            result = direct_dispatch(method, params)
            reply({"id": rid, "result": result})
        except Exception as exc:
            reply({
                "id": req.get("id") if isinstance(req, dict) else None,
                "error": f"{type(exc).__name__}: {exc}",
            })


if __name__ == "__main__":
    main()
