#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import concurrent.futures
import base64
import ctypes
from ftplib import FTP
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import os
import plistlib
import posixpath
import re
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
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlparse

STDOUT_LOCK = threading.Lock()
JOBS_LOCK = threading.RLock()


def emit(event: str, **data: Any) -> None:
    payload = json.dumps({"event": event, "data": data}, separators=(",", ":"), ensure_ascii=True)
    with STDOUT_LOCK:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def reply(payload: dict[str, Any]) -> None:
    with STDOUT_LOCK:
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n")
        sys.stdout.flush()


def ok(message: str = "", **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **extra}


def unavailable(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "available": False, "message": message, **extra}


def powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


ADB_MODERN_SERVER_PORT = int(os.environ.get("INSYNC_ADB_MODERN_PORT", "5041"))
ADB_MODERN_SERVER_LOCK = threading.RLock()


def _bundled_adb_path() -> str | None:
    candidates: list[Path] = []
    source_root = Path(__file__).resolve().parents[1]
    candidates.append(source_root / "resources" / "android-platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        candidates.append(exe.parent.parent / "resources" / "android-platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
        candidates.append(exe.parent / "android-platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def adb_modern_path() -> str | None:
    override = os.environ.get("INSYNC_MODERN_ADB_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    return _bundled_adb_path() or shutil.which("adb")


def adb_path() -> str | None:
    override = os.environ.get("INSYNC_ADB_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    # Use the bundled v41 client for classic USB ADB when available. The client
    # attaches to the ecosystem-owned server on 5037; it does not take server
    # ownership. PATH is fallback only because stale v40 adb.exe copies can
    # otherwise kill/restart a healthy v41 server during a version mismatch.
    return _bundled_adb_path() or shutil.which("adb")


def ffmpeg_path() -> str | None:
    override = os.environ.get("INSYNC_FFMPEG_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    try:
        import imageio_ffmpeg
        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    except Exception:
        pass
    return shutil.which("ffmpeg")




def _parse_adb_devices(output: str) -> list[dict[str, str]]:
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("*"):
            continue
        parts = line.split()
        devices.append({
            "serial": parts[0],
            "state": parts[1] if len(parts) > 1 else "unknown",
            "detail": " ".join(parts[2:]),
        })
    return devices


def _ensure_adb_modern_server(serial: str = "") -> str:
    adb = adb_modern_path()
    if not adb:
        raise FileNotFoundError("Modern ADB runtime is unavailable")
    with ADB_MODERN_SERVER_LOCK:
        run_quick([adb, "-P", str(ADB_MODERN_SERVER_PORT), "start-server"], timeout=20)
        rc, out, _err = run_quick([adb, "-P", str(ADB_MODERN_SERVER_PORT), "devices", "-l"], timeout=20)
        owned = _parse_adb_devices(out) if rc == 0 else []
        if (not serial and owned) or (serial and any(d.get("serial") == serial for d in owned)):
            return adb

        # A default 5037 server can hold USB ownership away from iNSync's
        # isolated v41 server. Re-home only when that server actually owns
        # a device iNSync is trying to use.
        rc0, out0, _err0 = run_quick([adb, "devices", "-l"], timeout=20)
        default_devices = _parse_adb_devices(out0) if rc0 == 0 else []
        should_rehome = bool(default_devices) and (
            not serial or any(d.get("serial") == serial for d in default_devices)
        )
        if should_rehome:
            run_quick([adb, "kill-server"], timeout=20)
            run_quick([adb, "-P", str(ADB_MODERN_SERVER_PORT), "start-server"], timeout=20)
            for _ in range(12):
                rc1, out1, _err1 = run_quick(
                    [adb, "-P", str(ADB_MODERN_SERVER_PORT), "devices", "-l"],
                    timeout=20,
                )
                moved = _parse_adb_devices(out1) if rc1 == 0 else []
                if (not serial and moved) or (serial and any(d.get("serial") == serial for d in moved)):
                    break
                time.sleep(0.25)
    return adb


def _adb_modern_base() -> list[str]:
    adb = adb_modern_path()
    if not adb:
        raise FileNotFoundError("Modern ADB runtime is unavailable")
    return [adb, "-P", str(ADB_MODERN_SERVER_PORT)]


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
    # Do not leave verbose child stdout/stderr in PIPEs while polling for
    # cancellation. On Windows, a full pipe can block the child before it exits
    # and make an otherwise-fast ADB command appear to time out. Temporary files
    # keep output draining at OS/file speed while retaining the explicit
    # cancellation and timeout loop.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            shell=False,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        job.process = proc
        started = time.time()
        while proc.poll() is None:
            if job.cancel.is_set():
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                finally:
                    raise Cancelled()
            if time.time() - started > timeout:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                finally:
                    raise TimeoutError(f"Command timed out after {timeout:g}s")
            time.sleep(0.05)
        stdout_file.seek(0)
        stderr_file.seek(0)
        out = stdout_file.read().decode("utf-8", errors="replace")
        err = stderr_file.read().decode("utf-8", errors="replace")
        return proc.returncode, out.strip(), err.strip()


def run_process_bytes(
    job: Job,
    cmd: list[str],
    *,
    timeout: float = 45,
    max_bytes: int = 10 * 1024 * 1024,
) -> tuple[int, bytes, str]:
    # Use a temporary file for binary stdout so a large image cannot fill a
    # PIPE buffer and stall the child while the cancellation loop is polling.
    with tempfile.TemporaryFile() as binary_out, tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            cmd,
            shell=False,
            stdout=binary_out,
            stderr=stderr_file,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        job.process = proc
        started = time.time()
        while proc.poll() is None:
            if job.cancel.is_set():
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                finally:
                    raise Cancelled()
            if time.time() - started > timeout:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                finally:
                    raise TimeoutError(f"Command timed out after {timeout:g}s")
            time.sleep(0.05)
        binary_out.seek(0, os.SEEK_END)
        size = binary_out.tell()
        if size > max_bytes:
            return 2, b"", f"Preview is larger than {max_bytes // (1024 * 1024)} MB"
        binary_out.seek(0)
        stderr_file.seek(0)
        out = binary_out.read()
        err = stderr_file.read()
    return proc.returncode, out, err.decode("utf-8", errors="replace").strip()


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
    uplink_baseline_path = state_dir / "sharing-uplink-baseline.json"
    mode = "$true" if enable else "$false"
    # Native HNetCfg + HomeNet recovery. The UI worker waits; Electron remains responsive.
    script = rf'''$ErrorActionPreference='Stop'
$enable={mode}
$publicName={json.dumps(public_name)}
$privateName={json.dumps(private_name)}
$scope='192.168.250.1'
$resultPath={json.dumps(str(result_path))}
$baselinePath={json.dumps(str(baseline_path))}
$uplinkBaselinePath={json.dumps(str(uplink_baseline_path))}
function Save-Result($obj){{$obj|ConvertTo-Json -Compress -Depth 8|Set-Content -LiteralPath $resultPath -Encoding UTF8}}
function Restore-UplinkMetric($path){{
  if(-not (Test-Path -LiteralPath $path)){{return}}
  try{{
    $u=Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
    if($u.publicName){{
      if([string]$u.automaticMetric -eq 'Enabled'){{
        Set-NetIPInterface -InterfaceAlias ([string]$u.publicName) -AddressFamily IPv4 -AutomaticMetric Enabled -ErrorAction SilentlyContinue
      }} else {{
        Set-NetIPInterface -InterfaceAlias ([string]$u.publicName) -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric ([int]$u.interfaceMetric) -ErrorAction SilentlyContinue
      }}
    }}
  }} finally {{
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }}
}}
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
    if(Test-Path -LiteralPath $uplinkBaselinePath){{
      $existing=Get-Content -Raw -LiteralPath $uplinkBaselinePath | ConvertFrom-Json
      if([string]$existing.publicName -ne [string]$publicName){{Restore-UplinkMetric $uplinkBaselinePath}}
    }}
    if(-not (Test-Path -LiteralPath $uplinkBaselinePath)){{
      $pubIf=Get-NetIPInterface -InterfaceAlias $publicName -AddressFamily IPv4
      [pscustomobject]@{{
        publicName=$publicName
        automaticMetric=[string]$pubIf.AutomaticMetric
        interfaceMetric=[int]$pubIf.InterfaceMetric
      }} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $uplinkBaselinePath -Encoding UTF8
    }}
    Set-NetIPInterface -InterfaceAlias $publicName -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 5
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
    Restore-UplinkMetric $uplinkBaselinePath
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


def _run_elevated_hidden(
    job: Job,
    executable: str,
    args: list[str],
    *,
    timeout: float = 180,
) -> tuple[int, str]:
    if sys.platform != "win32":
        raise RuntimeError("elevated Windows launch requested on non-Windows platform")

    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    ERROR_CANCELLED = 1223

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    params = subprocess.list2cmdline(args)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = params
    info.lpDirectory = str(Path(executable).parent) if Path(executable).is_absolute() else None
    info.nShow = SW_HIDE

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            return 1223, "Windows administrator approval was cancelled"
        return error or 1, ctypes.FormatError(error) if error else "Could not start elevated helper"

    process = info.hProcess
    started = time.time()
    try:
        while True:
            wait = kernel32.WaitForSingleObject(process, 100)
            if wait == WAIT_OBJECT_0:
                break
            if wait != WAIT_TIMEOUT:
                return 1, f"WaitForSingleObject failed: {wait}"
            if job.cancel.is_set():
                kernel32.TerminateProcess(process, 1)
                raise Cancelled()
            if time.time() - started > timeout:
                kernel32.TerminateProcess(process, 1)
                raise TimeoutError(f"Elevated helper timed out after {timeout:g}s")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            error = ctypes.get_last_error()
            return error or 1, ctypes.FormatError(error) if error else "Could not read elevated helper exit code"
        return int(exit_code.value), ""
    finally:
        kernel32.CloseHandle(process)


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
        # Elevate the helper directly through ShellExecuteEx with SW_HIDE.
        # Windows can still show its UAC consent UI, but no PowerShell console
        # window is created or flashed when the user clicks Sending/Disconnected.
        engine.progress(job, 15, "Waiting for Windows approval")
        rc, launch_error = _run_elevated_hidden(
            job,
            ps,
            ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            timeout=180,
        )
        engine.progress(job, 80, "Verifying sharing state")
        result: dict[str, Any]
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        else:
            result = unavailable(launch_error or f"Sharing helper exited {rc}")
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


def _parse_adb_devices(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        parts = line.split()
        devices.append({
            "serial": parts[0],
            "state": parts[1] if len(parts) > 1 else "unknown",
            "detail": " ".join(parts[2:]),
        })
    return devices


def adb_devices_data(job: Job | None = None) -> dict[str, Any]:
    modern = adb_modern_path()
    if modern:
        _ensure_adb_modern_server()
        cmd = _adb_modern_base() + ["devices", "-l"]
        if job:
            rc, out, err = run_process(job, cmd, timeout=20)
        else:
            rc, out, err = run_quick(cmd, timeout=20)
        devices = _parse_adb_devices(out) if rc == 0 else []
        return {
            "ok": rc == 0,
            "available": True,
            "devices": devices,
            "message": f"{len(devices)} ADB device(s)",
            "error": err if rc else "",
            "server_port": ADB_MODERN_SERVER_PORT,
        }

    adb = adb_path()
    if not adb:
        return unavailable("ADB runtime is unavailable", devices=[])
    if job:
        rc, out, err = run_process(job, [adb, "devices", "-l"], timeout=20)
    else:
        rc, out, err = run_quick([adb, "devices", "-l"], timeout=20)
    devices = _parse_adb_devices(out) if rc == 0 else []
    return {
        "ok": rc == 0,
        "available": True,
        "devices": devices,
        "message": f"{len(devices)} ADB device(s)",
        "error": err if rc else "",
        "server_port": 5037,
    }


def adb_devices_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    engine.progress(job, 20, "Refreshing ADB devices")
    return adb_devices_data(job)


def _adb_prefix(serial: str) -> list[str]:
    modern = adb_modern_path()
    if modern:
        _ensure_adb_modern_server(serial)
        return _adb_modern_base() + (["-s", serial] if serial else [])
    adb = adb_path()
    if not adb:
        raise FileNotFoundError("ADB runtime is unavailable")
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
        + [{"package": package, "kind": "system", "action": "uninstall"} for package in system]
    )
    return {
        "ok": rc_user == 0 or rc_sys == 0,
        "available": True,
        "apps": apps,
        "counts": {"user": len(user), "system": len(system)},
        "message": f"{len(user)} user / {len(system)} system app(s)",
        "error": "\n".join(x for x in (err_user if rc_user else "", err_sys if rc_sys else "") if x),
    }



APP_ICON_CACHE: dict[str, str] = {}


def _apk_icon_candidate_score(name: str, size: int) -> int:
    value = name.replace("\\", "/").lower()
    suffix = Path(value).suffix
    if suffix not in {".png", ".webp", ".jpg", ".jpeg"}:
        return -1
    if "/res/" not in "/" + value:
        return -1
    base = Path(value).stem.lower()
    score = 0
    if "mipmap" in value:
        score += 80
    elif "drawable" in value:
        score += 45
    if any(token in base for token in ("ic_launcher", "launcher", "app_icon", "application_icon")):
        score += 180
    elif base in {"icon", "ic_icon"} or base.endswith("_icon"):
        score += 140
    elif "icon" in base:
        score += 80
    elif "logo" in base:
        score += 50
    else:
        return -1
    if "xxxhdpi" in value:
        score += 45
    elif "xxhdpi" in value:
        score += 35
    elif "xhdpi" in value:
        score += 25
    elif "hdpi" in value:
        score += 15
    if "foreground" in base:
        score -= 12
    if "background" in base:
        score -= 35
    score += min(40, max(0, int(size).bit_length() - 9))
    return score


def _apk_icon_data_url(apk_path: Path) -> str:
    with zipfile.ZipFile(apk_path) as zf:
        candidates: list[tuple[int, int, str]] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            score = _apk_icon_candidate_score(info.filename, info.file_size)
            if score >= 0:
                candidates.append((score, info.file_size, info.filename))
        if not candidates:
            return ""
        _, _, name = max(candidates)
        raw = zf.read(name)
        suffix = Path(name).suffix.lower()
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(suffix, "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def adb_app_icon_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    package = str(job.params.get("package") or "").strip()
    serial = str(job.params.get("serial") or "")
    if not package or not re.fullmatch(r"[A-Za-z0-9._]+", package):
        return unavailable("Choose a valid Android package", package=package)
    key = f"{serial}|{package}"
    if key in APP_ICON_CACHE:
        return ok("Android app icon ready", package=package, data_url=APP_ICON_CACHE[key], cached=True)

    engine.progress(job, 12, f"Reading icon for {package}")
    rc, out, err = run_process(
        job,
        _adb_prefix(serial) + ["shell", "pm", "path", package],
        timeout=25,
    )
    if rc != 0:
        return unavailable(err or out or "Could not read package path", package=package)
    paths = [
        line[len("package:"):].strip()
        for line in out.splitlines()
        if line.strip().startswith("package:")
    ]
    base_remote = next((value for value in paths if value.endswith("/base.apk")), paths[0] if paths else "")
    if not base_remote:
        return unavailable("Android returned no APK path", package=package)

    with tempfile.TemporaryDirectory(prefix="insync-app-icon-") as td:
        local_apk = Path(td) / "base.apk"
        rc2, out2, err2 = run_process(
            job,
            _adb_prefix(serial) + ["pull", base_remote, str(local_apk)],
            timeout=120,
        )
        if rc2 != 0 or not local_apk.is_file():
            return unavailable(err2 or out2 or "Could not read package APK", package=package)
        try:
            data_url = _apk_icon_data_url(local_apk)
        except Exception as exc:
            return unavailable(f"Could not extract app icon: {exc}", package=package)
    if not data_url:
        return unavailable("No raster app icon found in the package", package=package)
    APP_ICON_CACHE[key] = data_url
    return ok("Android app icon ready", package=package, data_url=data_url, cached=False)


def _adb_ready_serial(serial: str = "") -> str:
    data = adb_devices_data()
    devices = [row for row in data.get("devices", []) if row.get("state") == "device"]
    if serial:
        if any(row.get("serial") == serial for row in devices):
            return serial
        raise ValueError(f"ADB device is not ready: {serial}")
    usb = [row for row in devices if ":" not in str(row.get("serial") or "")]
    if len(usb) == 1:
        return str(usb[0]["serial"])
    if len(devices) == 1:
        return str(devices[0]["serial"])
    if not devices:
        raise ValueError("No ready ADB device")
    raise ValueError("More than one ADB device is ready; choose a device first")


def _adb_wlan_ipv4(job: Job, serial: str) -> str:
    rc, out, _ = run_process(
        job,
        _adb_prefix(serial) + ["shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
        timeout=20,
    )
    if rc != 0:
        return ""
    for line in out.splitlines():
        text = line.strip()
        if not text.startswith("inet "):
            continue
        value = text.split()[1].split("/", 1)[0].strip()
        if value.count(".") == 3:
            return value
    return ""


def _adb_endpoint(value: str, default_port: int = 5555, require_port: bool = False) -> str:
    endpoint = str(value or "").strip()
    if not endpoint:
        return ""
    if ":" in endpoint:
        return endpoint
    if require_port:
        raise ValueError("Wireless pairing endpoint must include IP:port")
    return f"{endpoint}:{default_port}"


def adb_wifi_status_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    serial = str(job.params.get("serial") or "").strip()
    devices = adb_devices_data(job)
    ready = [row for row in devices.get("devices", []) if row.get("state") == "device"]
    selected = serial if serial and any(row.get("serial") == serial for row in ready) else ""
    if not selected:
        usb = [row for row in ready if ":" not in str(row.get("serial") or "")]
        if len(usb) == 1:
            selected = str(usb[0]["serial"])
        elif len(ready) == 1:
            selected = str(ready[0]["serial"])
    wlan_ip = ""
    adb_enabled = ""
    tcp_port = ""
    if selected:
        engine.progress(job, 35, "Reading Android wireless ADB state")
        wlan_ip = _adb_wlan_ipv4(job, selected)
        for key, command in (
            ("adb_enabled", ["shell", "settings", "get", "global", "adb_enabled"]),
            ("tcp_port", ["shell", "getprop", "service.adb.tcp.port"]),
        ):
            rc, out, _ = run_process(job, _adb_prefix(selected) + command, timeout=20)
            if rc == 0:
                if key == "adb_enabled":
                    adb_enabled = out.strip()
                else:
                    tcp_port = out.strip()
    wireless = [row for row in ready if ":" in str(row.get("serial") or "")]
    return ok(
        "Wireless ADB status refreshed",
        selected_serial=selected,
        wlan_ip=wlan_ip,
        adb_enabled=adb_enabled,
        tcp_port=tcp_port,
        wireless_devices=wireless,
        devices=devices.get("devices", []),
    )


def adb_wifi_enable_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    serial = _adb_ready_serial(str(job.params.get("serial") or ""))
    engine.progress(job, 12, "Reading Android Wi-Fi address")
    wlan_ip = _adb_wlan_ipv4(job, serial)
    if not wlan_ip:
        return unavailable("The selected Android device has no wlan0 IPv4 address", serial=serial)
    engine.progress(job, 38, "Switching adbd to tcpip 5555")
    rc, out, err = run_process(job, _adb_prefix(serial) + ["tcpip", "5555"], timeout=25)
    if rc != 0:
        return unavailable(out or err or "Could not enable wireless ADB", serial=serial, wlan_ip=wlan_ip)
    endpoint = f"{wlan_ip}:5555"
    time.sleep(1.0)
    if not adb_modern_path():
        return unavailable("Modern ADB runtime is unavailable", serial=serial, wlan_ip=wlan_ip)
    engine.progress(job, 68, f"Connecting to {endpoint}")
    rc2, out2, err2 = run_process(job, _adb_modern_base() + ["connect", endpoint], timeout=25)
    message = out2 or err2 or out or "Wireless ADB command completed"
    success = rc2 == 0 and ("connected to" in message.lower() or "already connected" in message.lower())
    return {
        "ok": success,
        "available": True,
        "message": message,
        "serial": serial,
        "wlan_ip": wlan_ip,
        "endpoint": endpoint,
        "devices": adb_devices_data().get("devices", []),
    }


def adb_wifi_connect_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    endpoint = _adb_endpoint(str(job.params.get("endpoint") or job.params.get("ip") or ""))
    if not endpoint:
        return unavailable("Enter the Android Wi-Fi ADB IP or IP:port")
    if not adb_modern_path():
        return unavailable("Modern ADB runtime is unavailable")
    engine.progress(job, 25, f"Connecting to {endpoint}")
    rc, out, err = run_process(job, _adb_modern_base() + ["connect", endpoint], timeout=25)
    message = out or err or "adb connect completed"
    return {
        "ok": rc == 0 and ("connected to" in message.lower() or "already connected" in message.lower()),
        "available": True,
        "message": message,
        "endpoint": endpoint,
        "devices": adb_devices_data().get("devices", []),
    }


def adb_wifi_disconnect_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    raw = str(job.params.get("endpoint") or "").strip()
    serial = str(job.params.get("serial") or "").strip()
    endpoint = _adb_endpoint(raw or (serial if ":" in serial else ""))
    if not endpoint:
        return unavailable("Choose or enter a wireless ADB endpoint first")
    if not adb_modern_path():
        return unavailable("Modern ADB runtime is unavailable")
    engine.progress(job, 30, f"Disconnecting {endpoint}")
    rc, out, err = run_process(job, _adb_modern_base() + ["disconnect", endpoint], timeout=20)
    return {
        "ok": rc == 0,
        "available": True,
        "message": out or err or "Wireless ADB disconnected",
        "endpoint": endpoint,
        "devices": adb_devices_data().get("devices", []),
    }


def adb_wifi_usb_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    serial = _adb_ready_serial(str(job.params.get("serial") or ""))
    engine.progress(job, 30, "Returning adbd to USB mode")
    rc, out, err = run_process(job, _adb_prefix(serial) + ["usb"], timeout=25)
    return {
        "ok": rc == 0,
        "available": True,
        "message": out or err or "ADB USB mode requested",
        "serial": serial,
    }


def adb_wifi_pair_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    endpoint = _adb_endpoint(str(job.params.get("endpoint") or ""), require_port=True)
    code = str(job.params.get("code") or "").strip()
    if not endpoint or not code:
        return unavailable("Enter the Wireless debugging pairing IP:port and pairing code")
    if not adb_modern_path():
        return unavailable("Modern ADB runtime is unavailable")
    engine.progress(job, 30, f"Pairing with {endpoint}")
    rc, out, err = run_process(job, _adb_modern_base() + ["pair", endpoint, code], timeout=30)
    message = out or err or "adb pair completed"
    return {
        "ok": rc == 0 and "successfully paired" in message.lower(),
        "available": True,
        "message": message,
        "endpoint": endpoint,
    }


def adb_wifi_mdns_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    adb = adb_modern_path()
    if not adb:
        return unavailable("Modern ADB runtime is unavailable", services=[])
    engine.progress(job, 30, "Discovering Android Wireless debugging services")
    rc, out, err = run_process(job, _adb_modern_base() + ["mdns", "services"], timeout=20)
    if rc != 0:
        return unavailable(err or out or "ADB mDNS discovery is unavailable", services=[])
    services: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("list of discovered"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        service = next((part for part in parts if part.startswith("_adb-tls-")), "")
        endpoint = parts[-1] if ":" in parts[-1] else ""
        if not service or not endpoint:
            continue
        services.append({
            "name": parts[0],
            "service": service,
            "endpoint": endpoint,
            "kind": "pairing" if "pairing" in service else "connect",
        })
    return ok(
        f"{len(services)} Wireless debugging service(s)",
        services=services,
        adb=adb,
    )



ANDROID_MEDIA_ROOTS = {
    "photos": ["/sdcard/DCIM", "/sdcard/Pictures"],
    "videos": ["/sdcard/DCIM", "/sdcard/Movies"],
}
ANDROID_MEDIA_EXTS = {
    "photos": {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".dng"},
    "videos": {".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi", ".m4v"},
}


def _android_media_kind(kind: str) -> str:
    value = str(kind or "photos").lower()
    if value not in ANDROID_MEDIA_ROOTS:
        raise ValueError("Unknown Android media category")
    return value


def adb_media_list_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    kind = _android_media_kind(str(job.params.get("kind") or "photos"))
    serial = str(job.params.get("serial") or "")
    media_type = "images" if kind == "photos" else "video"
    uri = f"content://media/external/{media_type}/media"
    engine.progress(job, 12, f"Reading Android {kind}")
    rc, out, err = run_process(
        job,
        _adb_prefix(serial) + [
            "shell", "content", "query",
            "--uri", uri,
            "--projection", "_id:_display_name:_data:_size",
        ],
        timeout=30,
    )
    items: list[dict[str, Any]] = []
    if rc == 0:
        for line in out.splitlines():
            if not line.startswith("Row:"):
                continue
            values: dict[str, str] = {}
            payload = line.split(" ", 2)[-1]
            for part in payload.split(", "):
                if "=" in part:
                    key, value = part.split("=", 1)
                    values[key.strip()] = value.strip()
            remote = values.get("_data", "")
            if not remote:
                continue
            try:
                size = int(values.get("_size") or 0)
            except ValueError:
                size = 0
            items.append({
                "kind": kind,
                "id": values.get("_id", ""),
                "path": remote,
                "name": values.get("_display_name") or posixpath.basename(remote),
                "size": size,
            })
            if len(items) >= 300:
                break
    items.reverse()
    return {
        "ok": rc == 0,
        "available": True,
        "kind": kind,
        "items": items,
        "message": f"{len(items)} Android {kind} item(s)",
        "error": err if rc else "",
    }



def _video_frame_from_command(job: Job, source_cmd: list[str], timeout: float = 45) -> tuple[bytes, str]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return b"", "FFmpeg video preview runtime is unavailable"
    source_err = tempfile.TemporaryFile()
    frame_out = tempfile.TemporaryFile()
    frame_err = tempfile.TemporaryFile()
    source = None
    decoder = None
    try:
        source = subprocess.Popen(
            source_cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=source_err,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        if source.stdout is None:
            return b"", "Could not open media stream"
        decoder = subprocess.Popen(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-ss", "0.20",
                "-frames:v", "1",
                "-vf", "scale=480:-2:force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            shell=False,
            stdin=source.stdout,
            stdout=frame_out,
            stderr=frame_err,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        source.stdout.close()
        started = time.time()
        while decoder.poll() is None:
            if job.cancel.is_set():
                for proc in (decoder, source):
                    if proc and proc.poll() is None:
                        proc.kill()
                raise Cancelled()
            if time.time() - started > timeout:
                for proc in (decoder, source):
                    if proc and proc.poll() is None:
                        proc.kill()
                return b"", f"Video preview timed out after {timeout:g}s"
            time.sleep(0.05)
        if source.poll() is None:
            source.terminate()
            try:
                source.wait(timeout=2)
            except subprocess.TimeoutExpired:
                source.kill()
        frame_out.seek(0)
        frame_err.seek(0)
        data = frame_out.read()
        error = frame_err.read().decode("utf-8", errors="replace").strip()
        if decoder.returncode != 0 or not data:
            source_err.seek(0)
            source_error = source_err.read().decode("utf-8", errors="replace").strip()
            return b"", error or source_error or "Could not decode video frame"
        return data, ""
    finally:
        for proc in (decoder, source):
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        source_err.close()
        frame_out.close()
        frame_err.close()


def _video_frame_from_file(job: Job, source: Path, timeout: float = 45) -> tuple[bytes, str]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return b"", "FFmpeg video preview runtime is unavailable"
    rc, raw, err = run_process_bytes(
        job,
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-ss", "0.20",
            "-frames:v", "1",
            "-vf", "scale=480:-2:force_original_aspect_ratio=decrease",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ],
        timeout=timeout,
        max_bytes=3 * 1024 * 1024,
    )
    return (raw, "") if rc == 0 and raw else (b"", err or "Could not decode video frame")


def _android_preview_mime(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".dng": "image/x-adobe-dng",
    }.get(suffix, "")


def adb_media_preview_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    remote = str(job.params.get("remote_path") or "").strip()
    serial = str(job.params.get("serial") or "")
    kind = str(job.params.get("kind") or "photos").lower()
    if not remote:
        return unavailable("Choose Android media before previewing it", kind=kind)
    if kind == "videos":
        engine.progress(job, 18, "Reading Android video frame")
        raw, err = _video_frame_from_command(
            job,
            _adb_prefix(serial) + ["exec-out", "cat", remote],
            timeout=50,
        )
        if not raw:
            return unavailable(err or "Could not read Android video frame", kind=kind, remote_path=remote)
        return ok(
            "Android video frame ready",
            kind=kind,
            remote_path=remote,
            data_url=f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}",
        )

    mime = _android_preview_mime(remote)
    if not mime:
        return unavailable("Preview is unavailable for this Android image", kind=kind, remote_path=remote)
    engine.progress(job, 18, "Reading Android image preview")
    rc, raw, err = run_process_bytes(
        job,
        _adb_prefix(serial) + ["exec-out", "cat", remote],
        timeout=45,
        max_bytes=8 * 1024 * 1024,
    )
    if rc != 0 or not raw:
        return unavailable(err or "Could not read Android image preview", kind=kind, remote_path=remote)
    return ok(
        "Android preview ready",
        kind=kind,
        remote_path=remote,
        data_url=f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
    )


def adb_media_pull_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    remote = str(job.params.get("remote_path") or "").strip()
    destination_raw = str(job.params.get("destination") or "").strip()
    serial = str(job.params.get("serial") or "")
    if not remote or not destination_raw:
        return unavailable("Choose Android content and a PC destination")
    destination = Path(destination_raw)
    destination.mkdir(parents=True, exist_ok=True)
    engine.progress(job, 15, "Sending Android content to PC")
    rc, out, err = run_process(job, _adb_prefix(serial) + ["pull", remote, str(destination)], timeout=600)
    return {
        "ok": rc == 0,
        "available": True,
        "kind": str(job.params.get("kind") or ""),
        "remote_path": remote,
        "destination": str(destination),
        "message": out or err or ("Saved to PC" if rc == 0 else "Transfer failed"),
    }


def adb_files_push_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    raw_sources = job.params.get("sources")
    if isinstance(raw_sources, list):
        sources = [Path(str(value)) for value in raw_sources if str(value or "").strip()]
    else:
        one = str(job.params.get("source") or "").strip()
        sources = [Path(one)] if one else []
    serial = str(job.params.get("serial") or "")
    destination = posixpath.normpath(str(job.params.get("destination") or "/sdcard/Download").strip())
    if not sources:
        return unavailable("Choose one or more PC files or folders before sending to Android")
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        return unavailable("One or more selected PC sources are unavailable", missing=missing)
    allowed_roots = ("/sdcard", "/storage/emulated/0")
    if not any(destination == root or destination.startswith(root + "/") for root in allowed_roots):
        return unavailable(
            "Android destination must be inside shared storage",
            destination=destination,
            allowed=list(allowed_roots),
        )

    engine.progress(job, 8, f"Preparing Android destination {destination}")
    rc, out, err = run_process(
        job,
        _adb_prefix(serial) + ["shell", "mkdir", "-p", destination],
        timeout=90,
    )
    if rc != 0:
        return unavailable(err or out or "Could not create Android destination", destination=destination)

    sent: list[dict[str, Any]] = []
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        pct = 12 + int(((index - 1) / max(1, total)) * 78)
        engine.progress(job, pct, f"Sending {source.name} to Android")
        rc, out, err = run_process(
            job,
            _adb_prefix(serial) + ["push", str(source), destination],
            timeout=1800,
        )
        if rc != 0:
            return unavailable(
                err or out or f"Could not send {source.name} to Android",
                destination=destination,
                sent=sent,
                failed=str(source),
            )
        remote_path = posixpath.join(destination, source.name)
        sent.append({
            "name": source.name,
            "local_path": str(source),
            "remote_path": remote_path,
            "directory": source.is_dir(),
        })
        if source.is_file() and destination.startswith((
            "/sdcard/DCIM",
            "/sdcard/Pictures",
            "/sdcard/Movies",
            "/sdcard/Music",
            "/storage/emulated/0/DCIM",
            "/storage/emulated/0/Pictures",
            "/storage/emulated/0/Movies",
            "/storage/emulated/0/Music",
        )):
            run_process(
                job,
                _adb_prefix(serial) + [
                    "shell", "am", "broadcast",
                    "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                    "-d", f"file://{remote_path}",
                ],
                timeout=45,
            )
    engine.progress(job, 95, "Android transfer complete")
    return ok(
        f"{len(sent)} item(s) sent to Android",
        destination=destination,
        items=sent,
        count=len(sent),
    )


def adb_media_delete_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    remote = str(job.params.get("remote_path") or "").strip()
    serial = str(job.params.get("serial") or "")
    kind = _android_media_kind(str(job.params.get("kind") or "photos"))
    if not remote:
        return unavailable("Choose Android content before deleting it", kind=kind)
    roots = ANDROID_MEDIA_ROOTS[kind]
    normalized = posixpath.normpath(remote)
    if not any(normalized == root or normalized.startswith(root + "/") for root in roots):
        return unavailable("Android media delete path is outside the selected media scope", kind=kind)
    engine.progress(job, 20, f"Deleting Android {kind[:-1] if kind.endswith('s') else kind}")
    rc, out, err = run_process(job, _adb_prefix(serial) + ["shell", "rm", "-f", remote], timeout=90)
    return {
        "ok": rc == 0,
        "available": True,
        "kind": kind,
        "remote_path": remote,
        "message": out or err or ("Deleted" if rc == 0 else "Delete failed"),
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
    engine.progress(job, 15, f"Uninstalling {package}")
    rc, out, err = run_process(job, _adb_prefix(serial) + ["uninstall", package], timeout=90)
    if rc == 0:
        return {
            "ok": True,
            "available": True,
            "message": out or "Uninstalled",
            "package": package,
            "method": "adb-uninstall",
        }
    engine.progress(job, 58, f"Removing {package} for Android user 0")
    rc2, out2, err2 = run_process(
        job,
        _adb_prefix(serial) + ["shell", "pm", "uninstall", "--user", "0", package],
        timeout=90,
    )
    return {
        "ok": rc2 == 0,
        "available": True,
        "message": out2 or err2 or err or out or ("Removed for user 0" if rc2 == 0 else "Uninstall failed"),
        "package": package,
        "method": "pm-uninstall-user-0" if rc2 == 0 else "failed",
        "direct_error": "" if rc == 0 else (err or out),
    }


def adb_app_export_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    package = str(job.params.get("package") or "").strip()
    destination_raw = str(job.params.get("destination") or "").strip()
    serial = str(job.params.get("serial") or "")
    if not package:
        return unavailable("Choose an installed Android app first")
    if not destination_raw:
        return unavailable("Choose a PC destination for the app")
    if not re.fullmatch(r"[A-Za-z0-9._]+", package):
        return unavailable("Android package name is invalid", package=package)

    engine.progress(job, 10, f"Reading APK paths for {package}")
    rc, out, err = run_process(
        job,
        _adb_prefix(serial) + ["shell", "pm", "path", package],
        timeout=30,
    )
    if rc != 0:
        return unavailable(err or out or "Could not read APK path", package=package)

    remote_paths: list[str] = []
    for line in out.splitlines():
        value = line.strip()
        if value.startswith("package:"):
            remote = value[len("package:"):].strip()
            if remote and remote not in remote_paths:
                remote_paths.append(remote)
    if not remote_paths:
        return unavailable("Android returned no APK path for this package", package=package)

    destination = Path(destination_raw).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    exported: list[str] = []

    if len(remote_paths) == 1:
        target = destination / f"{package}.apk"
        engine.progress(job, 35, f"Getting {package}")
        rc, out, err = run_process(
            job,
            _adb_prefix(serial) + ["pull", remote_paths[0], str(target)],
            timeout=300,
        )
        if rc != 0:
            return unavailable(err or out or "Could not export APK", package=package)
        exported.append(str(target))
    else:
        package_dir = destination / package
        package_dir.mkdir(parents=True, exist_ok=True)
        for index, remote in enumerate(remote_paths, start=1):
            name = posixpath.basename(remote) or f"split-{index}.apk"
            target = package_dir / name
            engine.progress(
                job,
                20 + (index - 1) / max(1, len(remote_paths)) * 75,
                f"Getting {name}",
            )
            rc, out, err = run_process(
                job,
                _adb_prefix(serial) + ["pull", remote, str(target)],
                timeout=300,
            )
            if rc != 0:
                return unavailable(
                    err or out or f"Could not export {name}",
                    package=package,
                    exported=exported,
                )
            exported.append(str(target))

    return ok(
        f"Got {package} to PC",
        package=package,
        files=exported,
        count=len(exported),
        destination=str(destination),
        split=len(remote_paths) > 1,
    )


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
    targets = [str(x) for x in (job.params.get("targets") or []) if x]
    if not text:
        return unavailable("Clipboard text is empty")
    if not targets:
        return unavailable("Choose at least one approved iNSync peer")
    peers, missing = _approved_targets(targets)
    results = []
    total = max(1, len(peers))
    for index, peer in enumerate(peers, 1):
        if job.cancel.is_set():
            raise Cancelled()
        engine.progress(job, 10 + (index - 1) * 80 / total, f"Sending text to {peer.get('name') or peer.get('ip')}")
        result = PEER_TRANSPORT.send(peer, "clipboard.text", {"text": text})
        results.append({"peer_id": peer.get("id"), "name": peer.get("name"), **result})
    failures = [item for item in results if not item.get("ok")]
    if missing:
        failures.extend({"peer_id": value, "ok": False, "message": "Peer not discovered"} for value in missing)
    return {
        "ok": bool(results) and not failures,
        "available": True,
        "message": f"Clipboard text sent to {len(results) - len([x for x in results if not x.get('ok')])} peer(s)" if results else "No approved peers were reachable",
        "results": results,
        "missing": missing,
    }


def clipboard_send_image_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    data_url = str(job.params.get("data_url") or "")
    targets = [str(x) for x in (job.params.get("targets") or []) if x]
    if not data_url:
        return unavailable("Clipboard image is empty")
    if not data_url.startswith("data:image/"):
        return unavailable("Clipboard image payload is invalid")
    if len(data_url.encode("utf-8")) > PEER_MAX_MESSAGE - 4096:
        return unavailable("Clipboard image is too large for peer transport")
    if not targets:
        return unavailable("Choose at least one approved iNSync peer")
    peers, missing = _approved_targets(targets)
    results = []
    total = max(1, len(peers))
    for index, peer in enumerate(peers, 1):
        if job.cancel.is_set():
            raise Cancelled()
        engine.progress(job, 10 + (index - 1) * 80 / total, f"Sending image to {peer.get('name') or peer.get('ip')}")
        result = PEER_TRANSPORT.send(peer, "clipboard.image", {"data_url": data_url})
        results.append({"peer_id": peer.get("id"), "name": peer.get("name"), **result})
    failures = [item for item in results if not item.get("ok")]
    if missing:
        failures.extend({"peer_id": value, "ok": False, "message": "Peer not discovered"} for value in missing)
    return {
        "ok": bool(results) and not failures,
        "available": True,
        "message": f"Clipboard image sent to {len(results) - len([x for x in results if not x.get('ok')])} peer(s)" if results else "No approved peers were reachable",
        "results": results,
        "missing": missing,
    }


PEER_PORT = 49549
PEER_DATA_PORT = 49550
PEER_MAGIC = b"INSYNC_DISCOVER_V1"
PEER_MAX_MESSAGE = 16 * 1024 * 1024


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
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
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
            "transfer_port": PEER_DATA_PORT,
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
        self.start()
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


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(min(65536, remaining))
        if not chunk:
            raise ConnectionError("peer disconnected")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)



PEER_FILE_CHUNK = 1024 * 1024


def _safe_peer_relative(value: str) -> Path:
    raw = str(value or "").replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("Invalid peer relative path")
    return Path(*pure.parts)


def _peer_receive_root(config: dict[str, Any]) -> Path:
    configured = [Path(str(value)).expanduser() for value in config.get("shared_paths", []) if value]
    root = configured[0] if configured else (_local_state_dir() / "received")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _peer_shared_roots(config: dict[str, Any]) -> list[Path]:
    scope = str(config.get("scope") or "internet-only")
    if scope == "internet-only":
        return []
    configured = [Path(str(value)).expanduser() for value in config.get("shared_paths", []) if value]
    roots: list[Path] = []
    for value in configured:
        try:
            resolved = value.resolve()
        except OSError:
            continue
        if resolved.exists() and resolved not in roots:
            roots.append(resolved)
    if scope == "whole-pc":
        if os.name == "nt":
            for code in range(ord("C"), ord("Z") + 1):
                drive = Path(f"{chr(code)}:\\")
                if drive.exists():
                    try:
                        resolved = drive.resolve()
                    except OSError:
                        continue
                    if resolved not in roots:
                        roots.append(resolved)
        else:
            root = Path("/").resolve()
            if root not in roots:
                roots.append(root)
    return roots


def _peer_resolve_shared(config: dict[str, Any], root_id: str, relative: str = "") -> tuple[Path, Path]:
    roots = _peer_shared_roots(config)
    try:
        index = int(str(root_id))
    except (TypeError, ValueError):
        raise ValueError("Invalid shared root")
    if index < 0 or index >= len(roots):
        raise ValueError("Shared root is unavailable")
    root = roots[index].resolve()
    if not relative:
        return root, root
    rel = _safe_peer_relative(relative)
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Requested path is outside the shared root")
    return root, target


def _peer_root_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for index, root in enumerate(_peer_shared_roots(config)):
        label = root.name or root.anchor or str(root)
        records.append({"id": str(index), "name": label, "path": str(root)})
    return records


def _peer_directory_records(config: dict[str, Any], root_id: str, relative: str) -> dict[str, Any]:
    root, target = _peer_resolve_shared(config, root_id, relative)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
    except PermissionError as exc:
        raise PermissionError(f"Cannot list {target}: {exc}") from exc
    for child in children[:500]:
        try:
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            continue
        rel = child.relative_to(root).as_posix()
        entries.append({
            "name": child.name,
            "path": rel,
            "type": "folder" if is_dir else "file",
            "size": size,
        })
    current = "" if target == root else target.relative_to(root).as_posix()
    return {"root_id": str(root_id), "path": current, "entries": entries}


def _collect_peer_file_plan(sources: list[Path]) -> tuple[list[tuple[Path, str, int]], int]:
    plan: list[tuple[Path, str, int]] = []
    total = 0
    for source in sources:
        if not source.exists():
            raise FileNotFoundError(str(source))
        if source.is_file():
            size = source.stat().st_size
            plan.append((source, source.name, size))
            total += size
            continue
        for child in source.rglob("*"):
            if not child.is_file():
                continue
            rel = (Path(source.name) / child.relative_to(source)).as_posix()
            size = child.stat().st_size
            plan.append((child, rel, size))
            total += size
    return plan, total


def _peer_send_json(sock: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > PEER_MAX_MESSAGE:
        raise ValueError("peer message is too large")
    sock.sendall(len(payload).to_bytes(4, "big") + payload)


def _peer_recv_json(sock: socket.socket) -> dict[str, Any]:
    raw_len = _recv_exact(sock, 4)
    length = int.from_bytes(raw_len, "big")
    if length <= 0 or length > PEER_MAX_MESSAGE:
        raise ValueError("invalid peer message size")
    value = json.loads(_recv_exact(sock, length).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("peer message must be an object")
    return value


class PeerTransport:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._serve, name="insync-peer-transport", daemon=True)
            self._thread.start()

    def _serve(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("", PEER_DATA_PORT))
            server.listen(8)
            server.settimeout(0.5)
            while True:
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_client,
                    args=(client, address),
                    name="insync-peer-client",
                    daemon=True,
                ).start()
        except OSError:
            return
        finally:
            server.close()

    def _handle_client(self, client: socket.socket, address: tuple[str, int]) -> None:
        try:
            client.settimeout(8)
            message = _peer_recv_json(client)
            config = _load_peer_config()
            sender_id = str(message.get("sender_id") or "")
            approved = set(str(x) for x in config.get("approved_ids", []))
            if not sender_id or sender_id not in approved:
                _peer_send_json(client, {"ok": False, "message": "Sender is not approved"})
                return
            action = str(message.get("action") or "")
            sender_name = str(message.get("sender_name") or address[0])
            if action == "clipboard.text":
                text = str(message.get("text") or "")
                if not text:
                    _peer_send_json(client, {"ok": False, "message": "Clipboard text is empty"})
                    return
                emit(
                    "peer.clipboard",
                    kind="text",
                    text=text,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_ip=address[0],
                )
                _peer_send_json(client, {"ok": True, "message": "Text clipboard received"})
                return
            if action == "clipboard.image":
                data_url = str(message.get("data_url") or "")
                if not data_url.startswith("data:image/"):
                    _peer_send_json(client, {"ok": False, "message": "Clipboard image payload is invalid"})
                    return
                emit(
                    "peer.clipboard",
                    kind="image",
                    data_url=data_url,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_ip=address[0],
                )
                _peer_send_json(client, {"ok": True, "message": "Image clipboard received"})
                return
            if action == "files.push":
                size = int(message.get("size") or -1)
                if size < 0:
                    _peer_send_json(client, {"ok": False, "message": "Invalid file size"})
                    return
                relative = _safe_peer_relative(str(message.get("relative_path") or message.get("name") or ""))
                receive_root = _peer_receive_root(config)
                target = (receive_root / relative).resolve()
                if target != receive_root and receive_root not in target.parents:
                    _peer_send_json(client, {"ok": False, "message": "Invalid receive path"})
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + f".insync-part-{uuid.uuid4().hex[:8]}")
                _peer_send_json(client, {"ok": True, "ready": True, "destination": str(target)})
                client.settimeout(600)
                digest = hashlib.sha256()
                received = 0
                try:
                    with temp.open("wb") as handle:
                        remaining = size
                        while remaining:
                            chunk = client.recv(min(PEER_FILE_CHUNK, remaining))
                            if not chunk:
                                raise ConnectionError("peer disconnected during file transfer")
                            handle.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                            remaining -= len(chunk)
                    os.replace(temp, target)
                finally:
                    if temp.exists():
                        try:
                            temp.unlink()
                        except OSError:
                            pass
                emit(
                    "peer.file.received",
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_ip=address[0],
                    path=str(target),
                    bytes=received,
                )
                _peer_send_json(
                    client,
                    {
                        "ok": True,
                        "message": "File received",
                        "path": str(target),
                        "bytes": received,
                        "sha256": digest.hexdigest(),
                    },
                )
                return
            if action == "files.roots":
                _peer_send_json(client, {"ok": True, "roots": _peer_root_records(config)})
                return
            if action == "files.list":
                listing = _peer_directory_records(
                    config,
                    str(message.get("root_id") or ""),
                    str(message.get("path") or ""),
                )
                _peer_send_json(client, {"ok": True, **listing})
                return
            if action == "files.pull":
                root, target = _peer_resolve_shared(
                    config,
                    str(message.get("root_id") or ""),
                    str(message.get("path") or ""),
                )
                if not target.is_file():
                    _peer_send_json(client, {"ok": False, "message": "Requested item is not a file"})
                    return
                size = target.stat().st_size
                _peer_send_json(
                    client,
                    {
                        "ok": True,
                        "ready": True,
                        "name": target.name,
                        "size": size,
                        "path": target.relative_to(root).as_posix(),
                    },
                )
                client.settimeout(600)
                digest = hashlib.sha256()
                with target.open("rb") as handle:
                    while True:
                        chunk = handle.read(PEER_FILE_CHUNK)
                        if not chunk:
                            break
                        client.sendall(chunk)
                        digest.update(chunk)
                _peer_send_json(
                    client,
                    {"ok": True, "message": "File sent", "bytes": size, "sha256": digest.hexdigest()},
                )
                return
            _peer_send_json(client, {"ok": False, "message": "Unsupported peer action"})
        except Exception as exc:
            try:
                _peer_send_json(client, {"ok": False, "message": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def send(self, peer: dict[str, Any], action: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = _load_peer_config()
        ip = str(peer.get("ip") or "")
        if not ip:
            return unavailable("Peer has no reachable IP", peer_id=peer.get("id"))
        port = int(peer.get("transfer_port") or PEER_DATA_PORT)
        message = {
            "action": action,
            "sender_id": config["peer_id"],
            "sender_name": socket.gethostname(),
            **payload,
        }
        try:
            with socket.create_connection((ip, port), timeout=5) as sock:
                sock.settimeout(10)
                _peer_send_json(sock, message)
                result = _peer_recv_json(sock)
        except OSError as exc:
            return unavailable(f"Peer transport failed: {exc}", peer_id=peer.get("id"), ip=ip)
        return result

    def send_file(
        self,
        peer: dict[str, Any],
        source: Path,
        relative_path: str,
        *,
        progress: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        config = _load_peer_config()
        ip = str(peer.get("ip") or "")
        if not ip:
            return unavailable("Peer has no reachable IP", peer_id=peer.get("id"))
        size = source.stat().st_size
        port = int(peer.get("transfer_port") or PEER_DATA_PORT)
        header = {
            "action": "files.push",
            "sender_id": config["peer_id"],
            "sender_name": socket.gethostname(),
            "name": source.name,
            "relative_path": PurePosixPath(relative_path).as_posix(),
            "size": size,
        }
        try:
            with socket.create_connection((ip, port), timeout=5) as sock:
                sock.settimeout(600)
                _peer_send_json(sock, header)
                ready = _peer_recv_json(sock)
                if not ready.get("ok") or not ready.get("ready"):
                    return ready
                sent = 0
                digest = hashlib.sha256()
                with source.open("rb") as handle:
                    while True:
                        if cancel and cancel.is_set():
                            raise Cancelled()
                        chunk = handle.read(PEER_FILE_CHUNK)
                        if not chunk:
                            break
                        sock.sendall(chunk)
                        digest.update(chunk)
                        sent += len(chunk)
                        if progress:
                            progress(len(chunk))
                result = _peer_recv_json(sock)
                result["local_sha256"] = digest.hexdigest()
                result["sent_bytes"] = sent
                return result
        except Cancelled:
            raise
        except OSError as exc:
            return unavailable(f"Peer file transport failed: {exc}", peer_id=peer.get("id"), ip=ip)

    def pull_file(
        self,
        peer: dict[str, Any],
        root_id: str,
        remote_path: str,
        destination: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        config = _load_peer_config()
        ip = str(peer.get("ip") or "")
        if not ip:
            return unavailable("Peer has no reachable IP", peer_id=peer.get("id"))
        port = int(peer.get("transfer_port") or PEER_DATA_PORT)
        request = {
            "action": "files.pull",
            "sender_id": config["peer_id"],
            "sender_name": socket.gethostname(),
            "root_id": str(root_id),
            "path": str(remote_path or ""),
        }
        try:
            with socket.create_connection((ip, port), timeout=5) as sock:
                sock.settimeout(600)
                _peer_send_json(sock, request)
                meta = _peer_recv_json(sock)
                if not meta.get("ok") or not meta.get("ready"):
                    return meta
                size = int(meta.get("size") or 0)
                name = Path(str(meta.get("name") or "received.bin")).name
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / name
                temp = target.with_name(target.name + f".insync-part-{uuid.uuid4().hex[:8]}")
                digest = hashlib.sha256()
                received = 0
                try:
                    with temp.open("wb") as handle:
                        remaining = size
                        while remaining:
                            if cancel and cancel.is_set():
                                raise Cancelled()
                            chunk = sock.recv(min(PEER_FILE_CHUNK, remaining))
                            if not chunk:
                                raise ConnectionError("peer disconnected during receive")
                            handle.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                            remaining -= len(chunk)
                            if progress:
                                progress(received, size)
                    final = _peer_recv_json(sock)
                    if not final.get("ok"):
                        return final
                    os.replace(temp, target)
                finally:
                    if temp.exists():
                        try:
                            temp.unlink()
                        except OSError:
                            pass
                return {
                    "ok": True,
                    "available": True,
                    "message": "File received",
                    "path": str(target),
                    "bytes": received,
                    "sha256": digest.hexdigest(),
                    "remote_sha256": final.get("sha256", ""),
                }
        except Cancelled:
            raise
        except (OSError, ConnectionError) as exc:
            return unavailable(f"Peer receive failed: {exc}", peer_id=peer.get("id"), ip=ip)


PEER_TRANSPORT = PeerTransport()


def ensure_peer_network() -> None:
    PEER_DISCOVERY.start()
    PEER_TRANSPORT.start()


def _approved_targets(target_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    ensure_peer_network()
    wanted = {str(x) for x in target_ids if x}
    peers = PEER_DISCOVERY.discover(timeout=1.0)
    selected = [peer for peer in peers if not peer.get("self") and peer.get("id") in wanted]
    found = {str(peer.get("id")) for peer in selected}
    missing = sorted(wanted - found)
    return selected, missing



def peer_files_send_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    sources = [Path(str(value)) for value in (job.params.get("sources") or []) if value]
    targets = [str(value) for value in (job.params.get("targets") or []) if value]
    if not sources:
        return unavailable("Choose one or more files/folders")
    if not targets:
        return unavailable("Choose at least one approved iNSync peer")
    peers, missing = _approved_targets(targets)
    if not peers:
        return unavailable("No approved peers are reachable", missing=missing)
    engine.progress(job, 2, "Scanning files")
    plan, bytes_per_peer = _collect_peer_file_plan(sources)
    total_bytes = max(1, bytes_per_peer * len(peers))
    transferred = 0
    results: list[dict[str, Any]] = []
    for peer in peers:
        for source, relative, size in plan:
            if job.cancel.is_set():
                raise Cancelled()
            peer_name = peer.get("name") or peer.get("ip") or peer.get("id")
            engine.progress(job, (transferred / total_bytes) * 100.0, f"Sending {source.name} to {peer_name}")
            def on_chunk(delta: int) -> None:
                nonlocal transferred
                transferred += delta
                engine.progress(
                    job,
                    min(99.0, (transferred / total_bytes) * 100.0),
                    f"Sending {source.name} to {peer_name}",
                )
            result = PEER_TRANSPORT.send_file(
                peer,
                source,
                relative,
                progress=on_chunk,
                cancel=job.cancel,
            )
            results.append({
                "peer_id": peer.get("id"),
                "peer": peer_name,
                "source": str(source),
                "relative_path": relative,
                **result,
            })
            if not result.get("ok"):
                return {
                    "ok": False,
                    "available": True,
                    "message": result.get("message") or "Peer file transfer failed",
                    "results": results,
                    "missing": missing,
                }
    return ok(
        f"Sent {len(plan)} file(s) to {len(peers)} peer(s)",
        files=len(plan),
        peers=len(peers),
        bytes=transferred,
        results=results,
        missing=missing,
    )


def _approved_peer(peer_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    peers, missing = _approved_targets([peer_id])
    return (peers[0] if peers else None), missing


def peer_files_roots_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    peer_id = str(job.params.get("peer_id") or "")
    if not peer_id:
        return unavailable("Choose an approved iNSync peer")
    peer, missing = _approved_peer(peer_id)
    if not peer:
        return unavailable("Peer is not reachable or approved", missing=missing)
    engine.progress(job, 20, "Reading shared roots")
    result = PEER_TRANSPORT.send(peer, "files.roots", {})
    return {**result, "peer_id": peer_id}


def peer_files_list_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    peer_id = str(job.params.get("peer_id") or "")
    root_id = str(job.params.get("root_id") or "")
    path = str(job.params.get("path") or "")
    if not peer_id or root_id == "":
        return unavailable("Choose a peer and shared root")
    peer, missing = _approved_peer(peer_id)
    if not peer:
        return unavailable("Peer is not reachable or approved", missing=missing)
    engine.progress(job, 20, "Reading remote folder")
    result = PEER_TRANSPORT.send(peer, "files.list", {"root_id": root_id, "path": path})
    return {**result, "peer_id": peer_id}


def peer_files_pull_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    peer_id = str(job.params.get("peer_id") or "")
    root_id = str(job.params.get("root_id") or "")
    path = str(job.params.get("path") or "")
    destination_raw = str(job.params.get("destination") or "").strip()
    if not peer_id or root_id == "" or not path or not destination_raw:
        return unavailable("Choose a peer file and local destination")
    peer, missing = _approved_peer(peer_id)
    if not peer:
        return unavailable("Peer is not reachable or approved", missing=missing)
    destination = Path(destination_raw)
    engine.progress(job, 10, "Receiving peer file")
    def on_progress(received: int, total: int) -> None:
        engine.progress(
            job,
            10.0 + (received / max(1, total)) * 88.0,
            f"Receiving {Path(path).name}",
        )
    result = PEER_TRANSPORT.pull_file(
        peer,
        root_id,
        path,
        destination,
        progress=on_progress,
        cancel=job.cancel,
    )
    return {**result, "peer_id": peer_id}


def peers_list_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    engine.progress(job, 15, "Starting peer discovery")
    ensure_peer_network()
    engine.progress(job, 30, "Discovering iNSync peers")
    peers = PEER_DISCOVERY.discover()
    remote = [peer for peer in peers if not peer.get("self")]
    return ok(f"{len(remote)} remote iNSync peer(s) found", peers=peers)


def peer_configure_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    ensure_peer_network()
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
    ensure_peer_network()
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


def _pmd_available() -> bool:
    try:
        return importlib.util.find_spec("pymobiledevice3") is not None
    except Exception:
        return False


IOS_USB_SCAN_TIMEOUT = 8.0
IOS_PAIR_TIMEOUT = 20.0


def _run_async(coro, *, timeout: float = 30.0):
    async def bounded():
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Apple device operation timed out after {timeout:g}s") from exc

    return asyncio.run(bounded())


async def _pmd_usb_ids() -> list[str]:
    from pymobiledevice3.usbmux import select_devices_by_connection_type

    try:
        devices = await asyncio.wait_for(
            select_devices_by_connection_type(connection_type="USB"),
            timeout=IOS_USB_SCAN_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Apple USB scan timed out after {IOS_USB_SCAN_TIMEOUT:g}s") from exc
    return [str(device.serial) for device in devices]


async def _pmd_lockdown(serial: str = ""):
    from pymobiledevice3.lockdown import create_using_usbmux

    ids = await _pmd_usb_ids()
    if not ids:
        raise ConnectionError("No iPhone connected")
    target = serial or ids[0]
    if serial and serial not in ids:
        raise ConnectionError("Selected iPhone is not connected")
    try:
        return await asyncio.wait_for(
            create_using_usbmux(
                serial=target,
                autopair=True,
                connection_type="USB",
                pair_timeout=int(IOS_PAIR_TIMEOUT),
            ),
            timeout=IOS_PAIR_TIMEOUT + 5,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Apple pairing timed out after {IOS_PAIR_TIMEOUT:g}s") from exc


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _ios_status_pmd(serial: str = "") -> dict[str, Any]:
    from pymobiledevice3.services.afc import AfcService

    ids = await _pmd_usb_ids()
    if not ids:
        return ok("0 iOS device(s)", devices=[], available=True)

    target = serial or ids[0]
    async with await _pmd_lockdown(target) as lockdown:
        values = dict(lockdown.all_values or {})
        storage: dict[str, int] = {}
        async with AfcService(lockdown=lockdown) as afc:
            fs = await afc.get_device_info()
            total = _as_int(fs.get("FSTotalBytes"))
            free = _as_int(fs.get("FSFreeBytes"))
            if total is not None:
                storage["total"] = total
            if free is not None:
                storage["free"] = free
        return ok(
            f"{len(ids)} iOS device(s)",
            devices=ids,
            device={
                "udid": target,
                "name": values.get("DeviceName", ""),
                "product_type": values.get("ProductType", ""),
                "ios_version": values.get("ProductVersion", ""),
                "build": values.get("BuildVersion", ""),
            },
            storage=storage,
            backend="pymobiledevice3",
        )


def ios_status_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    serial = str(job.params.get("serial") or "")
    if _pmd_available():
        engine.progress(job, 15, "Reading iPhone status")
        try:
            return _run_async(_ios_status_pmd(serial), timeout=15)
        except Exception as exc:
            pmd_error = f"{type(exc).__name__}: {exc}"
        else:
            pmd_error = ""
    else:
        pmd_error = "pymobiledevice3 is not bundled in this runtime"

    idevice_id = command_path("idevice_id")
    ideviceinfo = command_path("ideviceinfo")
    if not idevice_id:
        return unavailable(
            "Apple device bridge is unavailable",
            detail=pmd_error,
            devices=[],
        )
    rc, out, err = run_process(job, [idevice_id, "-l"], timeout=20)
    ids = [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []
    result: dict[str, Any] = {
        "ok": rc == 0,
        "available": True,
        "devices": ids,
        "message": f"{len(ids)} iOS device(s)",
        "error": err if rc else "",
        "backend": "libimobiledevice",
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
            total = _as_int(disk.get("TotalDiskCapacity")) or _as_int(disk.get("TotalDataCapacity"))
            free = _as_int(disk.get("TotalDataAvailable"))
            if total or free:
                result["storage"] = {"total": total, "free": free}
    return result


async def _ios_apps_pmd(serial: str = "") -> list[dict[str, Any]]:
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    async with await _pmd_lockdown(serial) as lockdown:
        apps = await InstallationProxyService(lockdown=lockdown).get_apps(
            application_type="User",
            calculate_sizes=False,
        )
        rows: list[dict[str, Any]] = []
        for bundle_id, info in apps.items():
            static = _as_int(info.get("StaticDiskUsage")) or 0
            dynamic = _as_int(info.get("DynamicDiskUsage")) or 0
            rows.append({
                "bundle_id": str(bundle_id),
                "name": (
                    info.get("CFBundleDisplayName")
                    or info.get("CFBundleName")
                    or info.get("CFBundleExecutable")
                    or bundle_id
                ),
                "version": info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "",
                "size": static + dynamic,
                "documents": bool(info.get("UIFileSharingEnabled") or info.get("UISupportsDocumentBrowser")),
            })
        return sorted(rows, key=lambda item: str(item.get("name", "")).casefold())


def ios_apps_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    if not _pmd_available():
        return unavailable("Installed-app management requires the bundled Apple device bridge", apps=[])
    engine.progress(job, 15, "Reading installed iPhone apps")
    try:
        apps = _run_async(_ios_apps_pmd(str(job.params.get("serial") or "")), timeout=30)
        return ok(f"{len(apps)} user app(s)", apps=apps)
    except Exception as exc:
        return unavailable(f"Could not read iPhone apps: {exc}", apps=[])


async def _ios_app_uninstall_pmd(bundle_id: str, serial: str = "") -> None:
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    async with await _pmd_lockdown(serial) as lockdown:
        await InstallationProxyService(lockdown=lockdown).uninstall(bundle_id)


def ios_app_uninstall_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    bundle_id = str(job.params.get("bundle_id") or "").strip()
    if not bundle_id:
        return unavailable("Choose an app before deleting it")
    if not _pmd_available():
        return unavailable("App delete requires the bundled Apple device bridge", bundle_id=bundle_id)
    engine.progress(job, 20, f"Deleting {bundle_id}")
    try:
        _run_async(_ios_app_uninstall_pmd(bundle_id, str(job.params.get("serial") or "")), timeout=120)
        return ok("App deleted", bundle_id=bundle_id)
    except Exception as exc:
        return unavailable(f"App delete failed: {exc}", bundle_id=bundle_id)


IOS_CAMERA_ROLL_ROOT = "DCIM"
IOS_MEDIA_ROOTS = {
    "photos": IOS_CAMERA_ROLL_ROOT,
    "videos": IOS_CAMERA_ROLL_ROOT,
    "music": "iTunes_Control/Music",
}
IOS_PHOTO_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".dng", ".aae"}
IOS_VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".3gp"}
IOS_MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".alac", ".wav", ".aiff", ".flac", ".mp4"}


def _ios_media_root(kind: str) -> str:
    kind = kind.lower()
    if kind not in IOS_MEDIA_ROOTS:
        raise ValueError("Unknown iPhone media category")
    return IOS_MEDIA_ROOTS[kind]


def _ios_scoped_remote(kind: str, remote_path: str) -> str:
    root = _ios_media_root(kind)
    value = posixpath.normpath("/" + str(remote_path or "").lstrip("/")).lstrip("/")
    if value != root and not value.startswith(root + "/"):
        raise ValueError("Remote path is outside the selected iPhone media scope")
    return value


async def _ios_media_list_pmd(kind: str, serial: str = "", limit: int = 240) -> list[dict[str, Any]]:
    from pymobiledevice3.services.afc import AfcService

    root = _ios_media_root(kind)
    allowed = IOS_PHOTO_EXTS if kind == "photos" else IOS_VIDEO_EXTS if kind == "videos" else IOS_MUSIC_EXTS
    rows: list[dict[str, Any]] = []
    async with await _pmd_lockdown(serial) as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            try:
                async for remote in afc.dirlist(root, -1):
                    if len(rows) >= limit:
                        break
                    try:
                        info = await afc.stat(remote)
                    except Exception:
                        continue
                    if info.get("st_ifmt") != "S_IFREG":
                        continue
                    suffix = Path(str(remote)).suffix.lower()
                    if allowed and suffix not in allowed:
                        continue
                    rows.append({
                        "kind": kind,
                        "path": str(remote),
                        "name": posixpath.basename(str(remote)),
                        "size": int(info.get("st_size") or 0),
                    })
            except Exception:
                return []
    return sorted(rows, key=lambda item: str(item["path"]).casefold())


def ios_media_list_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    kind = str(job.params.get("kind") or "photos").lower()
    if not _pmd_available():
        return unavailable("Photo/music browsing requires the bundled Apple device bridge", kind=kind, items=[])
    engine.progress(job, 15, f"Reading iPhone {kind}")
    try:
        items = _run_async(_ios_media_list_pmd(kind, str(job.params.get("serial") or "")), timeout=60)
        return ok(f"{len(items)} {kind} item(s)", kind=kind, source="camera-roll" if kind in {"photos", "videos"} else "media-library", items=items)
    except Exception as exc:
        return unavailable(f"Could not list iPhone {kind}: {exc}", kind=kind, items=[])



def _ios_preview_mime(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".dng": "image/x-adobe-dng",
    }.get(suffix, "")


async def _ios_media_preview_pmd(kind: str, remote_path: str, serial: str = "") -> bytes:
    from pymobiledevice3.services.afc import AfcService

    remote = _ios_scoped_remote(kind, remote_path)
    async with await _pmd_lockdown(serial) as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            return await afc.get_file_contents(remote)


async def _ios_video_preview_pmd(job: Job, remote_path: str, serial: str = "") -> tuple[bytes, str]:
    from pymobiledevice3.services.afc import AfcService

    remote = _ios_scoped_remote("videos", remote_path)
    async with await _pmd_lockdown(serial) as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            info = await afc.stat(remote)
            size = int(info.get("st_size") or 0)
            if size > 1024 * 1024 * 1024:
                return b"", "Video is larger than the 1 GB inline-preview limit"
            with tempfile.TemporaryDirectory(prefix="insync-ios-video-") as td:
                target = Path(td) / (posixpath.basename(remote) or "preview.mov")
                await afc.pull(remote, str(target))
                return _video_frame_from_file(job, target, timeout=60)


def ios_media_preview_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    kind = str(job.params.get("kind") or "photos").lower()
    remote_path = str(job.params.get("remote_path") or "")
    if not remote_path:
        return unavailable("Choose iPhone media before previewing it", kind=kind)
    if not _pmd_available():
        return unavailable("Media preview requires the bundled Apple device bridge", kind=kind, remote_path=remote_path)

    if kind == "videos":
        engine.progress(job, 18, "Reading iPhone video frame")
        try:
            raw, err = _run_async(_ios_video_preview_pmd(job, remote_path, str(job.params.get("serial") or "")), timeout=180)
            if not raw:
                return unavailable(err or "Could not read iPhone video frame", kind=kind, remote_path=remote_path)
            return ok(
                "iPhone video frame ready",
                kind=kind,
                remote_path=remote_path,
                data_url=f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}",
            )
        except Exception as exc:
            return unavailable(f"Could not read iPhone video frame: {exc}", kind=kind, remote_path=remote_path)

    mime = _ios_preview_mime(remote_path)
    if kind != "photos" or not mime:
        return unavailable("Preview is unavailable for this iPhone item", kind=kind, remote_path=remote_path)
    engine.progress(job, 18, "Reading iPhone photo preview")
    try:
        raw = _run_async(_ios_media_preview_pmd(kind, remote_path, str(job.params.get("serial") or "")), timeout=90)
        if len(raw) > 8 * 1024 * 1024:
            return unavailable("Photo is too large for an inline preview", kind=kind, remote_path=remote_path)
        return ok(
            "iPhone preview ready",
            kind=kind,
            remote_path=remote_path,
            data_url=f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
        )
    except Exception as exc:
        return unavailable(f"Could not read iPhone photo preview: {exc}", kind=kind, remote_path=remote_path)


async def _ios_media_pull_pmd(kind: str, remote_path: str, destination: Path, serial: str = "") -> Path:
    from pymobiledevice3.services.afc import AfcService

    remote = _ios_scoped_remote(kind, remote_path)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / posixpath.basename(remote)
    async with await _pmd_lockdown(serial) as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            await afc.pull(remote, str(target))
    return target


def ios_media_pull_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    kind = str(job.params.get("kind") or "photos").lower()
    remote_path = str(job.params.get("remote_path") or "")
    destination_raw = str(job.params.get("destination") or "").strip()
    if not remote_path or not destination_raw:
        return unavailable("Choose an iPhone item and PC destination")
    destination = Path(destination_raw)
    if not _pmd_available():
        return unavailable("Media export requires the bundled Apple device bridge", kind=kind)
    engine.progress(job, 15, "Sending iPhone item to PC")
    try:
        target = _run_async(_ios_media_pull_pmd(kind, remote_path, destination, str(job.params.get("serial") or "")), timeout=600)
        return ok("Saved to PC", kind=kind, local_path=str(target), remote_path=remote_path)
    except Exception as exc:
        return unavailable(f"Could not save iPhone item: {exc}", kind=kind, remote_path=remote_path)


async def _ios_media_delete_pmd(kind: str, remote_path: str, serial: str = "") -> None:
    from pymobiledevice3.services.afc import AfcService

    remote = _ios_scoped_remote(kind, remote_path)
    async with await _pmd_lockdown(serial) as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            await afc.rm(remote)


def ios_media_delete_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    kind = str(job.params.get("kind") or "photos").lower()
    remote_path = str(job.params.get("remote_path") or "")
    if not remote_path:
        return unavailable("Choose an iPhone item before deleting it", kind=kind)
    if kind == "music":
        return unavailable(
            "Music delete is blocked until the library-safe Apple media adapter is qualified",
            kind=kind,
            remote_path=remote_path,
        )
    if not _pmd_available():
        return unavailable("Photo delete requires the bundled Apple device bridge", kind=kind)
    engine.progress(job, 20, "Deleting iPhone photo")
    try:
        _run_async(_ios_media_delete_pmd(kind, remote_path, str(job.params.get("serial") or "")), timeout=120)
        return ok("Photo deleted", kind=kind, remote_path=remote_path)
    except Exception as exc:
        return unavailable(f"Photo delete failed: {exc}", kind=kind, remote_path=remote_path)


IOS_DOCUMENTS_ROOT = "/Documents"


def _ios_document_path(remote_path: str) -> str:
    raw = str(remote_path or "").replace("\\", "/").strip()
    if raw in {"", "/"}:
        return IOS_DOCUMENTS_ROOT
    value = posixpath.normpath("/" + raw.lstrip("/"))
    if value != IOS_DOCUMENTS_ROOT and not value.startswith(IOS_DOCUMENTS_ROOT + "/"):
        value = posixpath.normpath(IOS_DOCUMENTS_ROOT + "/" + raw.lstrip("/"))
    if value != IOS_DOCUMENTS_ROOT and not value.startswith(IOS_DOCUMENTS_ROOT + "/"):
        raise ValueError("App Documents path is outside /Documents")
    return value


async def _ios_documents_service(lockdown, bundle_id: str):
    from pymobiledevice3.services.house_arrest import HouseArrestService

    return await HouseArrestService.create(
        lockdown=lockdown,
        bundle_id=bundle_id,
        documents_only=True,
    )


async def _ios_documents_list_pmd(
    bundle_id: str,
    directory: str = IOS_DOCUMENTS_ROOT,
    serial: str = "",
    limit: int = 240,
) -> tuple[str, list[dict[str, Any]]]:
    current = _ios_document_path(directory)
    rows: list[dict[str, Any]] = []
    async with await _pmd_lockdown(serial) as lockdown:
        async with await _ios_documents_service(lockdown, bundle_id) as docs:
            if not await docs.isdir(current):
                raise ValueError("Selected app Documents path is not a folder")
            for name in await docs.listdir(current):
                if len(rows) >= limit:
                    break
                remote = posixpath.join(current, name)
                try:
                    info = await docs.stat(remote)
                except Exception:
                    continue
                entry_type = info.get("st_ifmt")
                if entry_type not in {"S_IFREG", "S_IFDIR"}:
                    continue
                is_dir = entry_type == "S_IFDIR"
                rows.append({
                    "kind": "documents",
                    "bundle_id": bundle_id,
                    "path": str(remote),
                    "name": str(name),
                    "size": 0 if is_dir else int(info.get("st_size") or 0),
                    "is_dir": is_dir,
                    "entry_type": "folder" if is_dir else "file",
                })
    rows.sort(key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").casefold()))
    return current, rows


def ios_documents_list_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    bundle_id = str(job.params.get("bundle_id") or "").strip()
    current_path = str(job.params.get("path") or IOS_DOCUMENTS_ROOT)
    if not bundle_id:
        return unavailable("Choose an app before browsing Documents", bundle_id=bundle_id, path=IOS_DOCUMENTS_ROOT, items=[])
    if not _pmd_available():
        return unavailable("App Documents require the bundled Apple device bridge", bundle_id=bundle_id, path=IOS_DOCUMENTS_ROOT, items=[])
    engine.progress(job, 15, "Reading app Documents")
    try:
        current, items = _run_async(
            _ios_documents_list_pmd(bundle_id, current_path, str(job.params.get("serial") or "")),
            timeout=60,
        )
        parent = IOS_DOCUMENTS_ROOT if current == IOS_DOCUMENTS_ROOT else _ios_document_path(posixpath.dirname(current))
        return ok(
            f"{len(items)} item(s) in {current}",
            bundle_id=bundle_id,
            path=current,
            parent_path=parent,
            items=items,
        )
    except Exception as exc:
        return unavailable(
            f"Could not browse app Documents: {exc}",
            bundle_id=bundle_id,
            path=_ios_document_path(current_path),
            items=[],
        )


async def _ios_documents_pull_pmd(bundle_id: str, remote_path: str, destination: Path, serial: str = "") -> Path:
    remote = _ios_document_path(remote_path)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / posixpath.basename(remote)
    async with await _pmd_lockdown(serial) as lockdown:
        async with await _ios_documents_service(lockdown, bundle_id) as docs:
            await docs.pull(remote, str(target))
    return target


def ios_documents_pull_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    bundle_id = str(job.params.get("bundle_id") or "").strip()
    remote_path = str(job.params.get("remote_path") or "")
    destination_raw = str(job.params.get("destination") or "").strip()
    if not bundle_id or not remote_path or not destination_raw:
        return unavailable("Choose an app document and PC destination")
    destination = Path(destination_raw)
    if not _pmd_available():
        return unavailable("App Documents require the bundled Apple device bridge", bundle_id=bundle_id)
    engine.progress(job, 15, "Saving app document to PC")
    try:
        target = _run_async(_ios_documents_pull_pmd(bundle_id, remote_path, destination, str(job.params.get("serial") or "")), timeout=600)
        return ok("Document saved to PC", bundle_id=bundle_id, local_path=str(target), remote_path=remote_path)
    except Exception as exc:
        return unavailable(f"Could not save app document: {exc}", bundle_id=bundle_id)


async def _ios_documents_push_pmd(
    bundle_id: str,
    local_paths: list[Path],
    destination_path: str = IOS_DOCUMENTS_ROOT,
    serial: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    destination = _ios_document_path(destination_path)
    uploaded: list[dict[str, Any]] = []
    async with await _pmd_lockdown(serial) as lockdown:
        async with await _ios_documents_service(lockdown, bundle_id) as docs:
            if not await docs.isdir(destination):
                raise ValueError("Selected app destination is not a folder")
            for local_path in local_paths:
                remote = posixpath.join(destination, local_path.name)
                await docs.push(str(local_path), remote, progress_bar=False)
                uploaded.append({
                    "name": local_path.name,
                    "local_path": str(local_path),
                    "remote_path": remote,
                    "size": local_path.stat().st_size,
                })
    return destination, uploaded


def ios_documents_push_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    bundle_id = str(job.params.get("bundle_id") or "").strip()
    raw_paths = job.params.get("local_paths")
    if isinstance(raw_paths, list):
        local_paths = [Path(str(value)) for value in raw_paths if str(value or "").strip()]
    else:
        legacy = str(job.params.get("local_path") or "").strip()
        local_paths = [Path(legacy)] if legacy else []
    if not bundle_id or not local_paths:
        return unavailable("Choose an app and one or more local files before sending")
    missing = [str(path) for path in local_paths if not path.is_file()]
    if missing:
        return unavailable("One or more selected files are unavailable", bundle_id=bundle_id, missing=missing)
    if not _pmd_available():
        return unavailable("App Documents require the bundled Apple device bridge", bundle_id=bundle_id)
    destination_path = str(job.params.get("destination_path") or IOS_DOCUMENTS_ROOT)
    engine.progress(job, 15, f"Sending {len(local_paths)} file(s) to {destination_path}")
    try:
        timeout = max(600, 600 * len(local_paths))
        destination, uploaded = _run_async(
            _ios_documents_push_pmd(
                bundle_id,
                local_paths,
                destination_path,
                str(job.params.get("serial") or ""),
            ),
            timeout=timeout,
        )
        result = {
            "bundle_id": bundle_id,
            "destination_path": destination,
            "items": uploaded,
            "count": len(uploaded),
        }
        if len(uploaded) == 1:
            result["remote_path"] = uploaded[0]["remote_path"]
            result["local_path"] = uploaded[0]["local_path"]
        return ok(f"{len(uploaded)} file(s) sent to app Documents", **result)
    except Exception as exc:
        return unavailable(f"Could not send app document: {exc}", bundle_id=bundle_id)


async def _ios_documents_mkdir_pmd(
    bundle_id: str,
    parent_path: str,
    name: str,
    serial: str = "",
) -> str:
    parent = _ios_document_path(parent_path)
    folder_name = str(name or "").strip()
    if not folder_name or folder_name in {".", ".."} or "/" in folder_name or "\\" in folder_name:
        raise ValueError("Folder name must be a single valid name")
    remote = _ios_document_path(posixpath.join(parent, folder_name))
    async with await _pmd_lockdown(serial) as lockdown:
        async with await _ios_documents_service(lockdown, bundle_id) as docs:
            if not await docs.isdir(parent):
                raise ValueError("Selected parent is not a folder")
            await docs.makedirs(remote)
    return remote


def ios_documents_mkdir_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    bundle_id = str(job.params.get("bundle_id") or "").strip()
    parent_path = str(job.params.get("path") or IOS_DOCUMENTS_ROOT)
    name = str(job.params.get("name") or "").strip()
    if not bundle_id or not name:
        return unavailable("Choose an app and enter a folder name")
    if not _pmd_available():
        return unavailable("App Documents require the bundled Apple device bridge", bundle_id=bundle_id)
    engine.progress(job, 20, "Creating app Documents folder")
    try:
        remote = _run_async(
            _ios_documents_mkdir_pmd(bundle_id, parent_path, name, str(job.params.get("serial") or "")),
            timeout=120,
        )
        return ok(
            "Folder created",
            bundle_id=bundle_id,
            path=remote,
            parent_path=_ios_document_path(parent_path),
            name=posixpath.basename(remote),
        )
    except Exception as exc:
        return unavailable(f"Could not create app Documents folder: {exc}", bundle_id=bundle_id)


async def _ios_documents_delete_pmd(bundle_id: str, remote_path: str, serial: str = "") -> None:
    remote = _ios_document_path(remote_path)
    async with await _pmd_lockdown(serial) as lockdown:
        async with await _ios_documents_service(lockdown, bundle_id) as docs:
            await docs.rm(remote)


def ios_documents_delete_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    bundle_id = str(job.params.get("bundle_id") or "").strip()
    remote_path = str(job.params.get("remote_path") or "")
    if not bundle_id or not remote_path:
        return unavailable("Choose an app document before deleting it")
    if not _pmd_available():
        return unavailable("App Documents require the bundled Apple device bridge", bundle_id=bundle_id)
    engine.progress(job, 20, "Deleting app document")
    try:
        _run_async(_ios_documents_delete_pmd(bundle_id, remote_path, str(job.params.get("serial") or "")), timeout=120)
        return ok("App document deleted", bundle_id=bundle_id, remote_path=remote_path)
    except Exception as exc:
        return unavailable(f"Could not delete app document: {exc}", bundle_id=bundle_id)


async def _ios_ipa_install_pmd(path: Path, serial: str = "") -> None:
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    async with await _pmd_lockdown(serial) as lockdown:
        await InstallationProxyService(lockdown=lockdown).install_from_local(path)


def ios_ipa_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    path = Path(str(job.params.get("path") or ""))
    if not path.is_file():
        return unavailable("Choose an IPA before installing")
    try:
        metadata = _ipa_metadata(path)
    except Exception as exc:
        return unavailable(f"IPA validation failed: {exc}")

    if _pmd_available():
        engine.progress(job, 20, "Installing IPA through Apple device bridge")
        try:
            _run_async(_ios_ipa_install_pmd(path, str(job.params.get("serial") or "")), timeout=600)
            return ok("IPA installed", path=str(path), metadata=metadata, backend="pymobiledevice3")
        except Exception as exc:
            return unavailable(f"IPA install failed: {exc}", path=str(path), metadata=metadata)

    installer = command_path("ideviceinstaller")
    if not installer:
        return unavailable(
            "IPA is validated, but the Apple install backend is not installed",
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
        "backend": "ideviceinstaller",
    }

PS4_FTP_PORT = int(os.environ.get("INSYNC_PS4_FTP_PORT", "2121"))
PS4_RPI_PORT = int(os.environ.get("INSYNC_PS4_RPI_PORT", "12800"))
PS4_COMPANION_PORT = int(os.environ.get("INSYNC_PS4_COMPANION_PORT", "9025"))
PS4_BINLOADER_PORT = int(os.environ.get("INSYNC_PS4_BINLOADER_PORT", "9090"))
PS4_KLOG_PORT = int(os.environ.get("INSYNC_PS4_KLOG_PORT", "3232"))
PS4_PACKAGE_HTTP_PORT = int(os.environ.get("INSYNC_PS4_HTTP_PORT", "8337"))
PS4_DISCOVERY_LOCK = threading.RLock()
PS4_SERVER_LOCK = threading.RLock()
PS4_PACKAGE_ROUTES: dict[str, Path] = {}
PS4_PACKAGE_SERVER: ThreadingHTTPServer | None = None
PS4_PACKAGE_SERVER_THREAD: threading.Thread | None = None
PS4_LAST_IP = ""


def _ps4_state_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "iNSync"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ps4.json"


def _ps4_load_state() -> dict[str, Any]:
    try:
        payload = json.loads(_ps4_state_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _ps4_write_state(**changes: Any) -> None:
    try:
        payload = _ps4_load_state()
        payload.update(changes)
        payload["updated_at"] = time.time()
        _ps4_state_path().write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _ps4_load_saved_ip() -> str:
    try:
        value = str(_ps4_load_state().get("ip") or "").strip()
        ipaddress.ip_address(value)
        return value
    except Exception:
        return ""


def _ps4_save_ip(ip: str) -> None:
    _ps4_write_state(ip=ip)


def _ps4_companion_token() -> str:
    return str(_ps4_load_state().get("companion_token") or "").strip()


def _ps4_save_companion_token(token: str) -> None:
    _ps4_write_state(companion_token=str(token or "").strip())


def _bundled_ps4_companion_pkg() -> Path | None:
    candidates: list[Path] = []
    source_root = Path(__file__).resolve().parents[1]
    candidates.append(source_root / "resources" / "ps4" / "iNSync-Companion.pkg")
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        candidates.append(exe.parent.parent / "resources" / "ps4" / "iNSync-Companion.pkg")
        candidates.append(exe.parent / "ps4" / "iNSync-Companion.pkg")
    override = os.environ.get("INSYNC_PS4_COMPANION_PKG", "").strip()
    if override:
        candidates.insert(0, Path(override))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None

def _tcp_open(host: str, port: int, timeout: float = 0.45) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.05, float(timeout))):
            return True
    except OSError:
        return False


def _ps4_ftp_banner(ip: str, timeout: float = 0.65) -> str:
    ftp = FTP()
    try:
        ftp.connect(ip, PS4_FTP_PORT, timeout=timeout)
        return str(ftp.getwelcome() or "")
    except Exception:
        return ""
    finally:
        try:
            ftp.close()
        except Exception:
            pass


def _ps4_arp_candidates() -> list[str]:
    candidates: list[str] = []
    try:
        _rc, out, _err = run_quick(["arp", "-a"], timeout=5)
        for match in re.finditer(r"(?m)^\s*(\d+\.\d+\.\d+\.\d+)\s+", out or ""):
            value = match.group(1)
            try:
                addr = ipaddress.ip_address(value)
                if addr.is_private:
                    candidates.append(value)
            except ValueError:
                pass
    except Exception:
        pass
    return list(dict.fromkeys(candidates))


def _ps4_local_ipv4s() -> list[str]:
    values: list[str] = []
    try:
        _rc, out, _err = run_quick(["ipconfig"], timeout=8)
        for value in re.findall(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", out or "", flags=re.I):
            try:
                addr = ipaddress.ip_address(value)
                if addr.is_private and not addr.is_loopback:
                    values.append(value)
            except ValueError:
                pass
    except Exception:
        pass
    return list(dict.fromkeys(values))


def _ps4_discover_ip() -> str:
    global PS4_LAST_IP
    with PS4_DISCOVERY_LOCK:
        preferred = [
            str(os.environ.get("INSYNC_PS4_IP") or "").strip(),
            PS4_LAST_IP,
            _ps4_load_saved_ip(),
            *_ps4_arp_candidates(),
        ]
        for candidate in dict.fromkeys(x for x in preferred if x):
            if "GoldHEN FTP" in _ps4_ftp_banner(candidate):
                PS4_LAST_IP = candidate
                _ps4_save_ip(candidate)
                return candidate

        # Bounded fallback: scan each private local /24 for a GoldHEN FTP banner.
        networks: list[ipaddress.IPv4Network] = []
        for local_ip in _ps4_local_ipv4s():
            try:
                networks.append(ipaddress.ip_network(local_ip + "/24", strict=False))
            except ValueError:
                pass
        hosts: list[str] = []
        for network in networks:
            hosts.extend(str(host) for host in network.hosts())
        hosts = list(dict.fromkeys(hosts))[:1024]
        if not hosts:
            return ""

        def probe(candidate: str) -> str:
            return candidate if "GoldHEN FTP" in _ps4_ftp_banner(candidate, timeout=0.16) else ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=96) as pool:
            for result in pool.map(probe, hosts, chunksize=8):
                if result:
                    PS4_LAST_IP = result
                    _ps4_save_ip(result)
                    return result
        return ""


def _ps4_ftp_connect(ip: str, timeout: float = 12.0) -> FTP:
    ftp = FTP()
    ftp.connect(ip, PS4_FTP_PORT, timeout=timeout)
    banner = str(ftp.getwelcome() or "")
    if "GoldHEN FTP" not in banner:
        ftp.close()
        raise RuntimeError(f"Unexpected FTP service on {ip}:{PS4_FTP_PORT}: {banner}")
    try:
        ftp.login()
    except Exception:
        ftp.login("anonymous", "")
    return ftp


def _ps4_goldhen_config_hdd(ftp: FTP) -> dict[str, Any]:
    from io import BytesIO

    buf = BytesIO()
    ftp.retrbinary("RETR /data/GoldHEN/config.ini", buf.write)
    original = buf.getvalue()
    text = original.decode("utf-8", "replace")
    updated = re.sub(r"(?m)^Pkg_Source\s*=\s*\d+\s*$", "Pkg_Source = 1", text)
    changed = updated != text
    if changed:
        ftp.storbinary("STOR /data/GoldHEN/config.ini", BytesIO(updated.encode("utf-8")))
    return {
        "changed": changed,
        "pkg_source_hdd": bool(re.search(r"(?m)^Pkg_Source\s*=\s*1\s*$", updated)),
        "bgft_enabled": (
            re.search(r"(?m)^Bgft_Enabled\s*=\s*(\d+)", updated).group(1)
            if re.search(r"(?m)^Bgft_Enabled\s*=\s*(\d+)", updated)
            else None
        ),
    }


def _ps4_ftp_stage_packages(
    ip: str,
    paths: list[Path],
    job: Job | None = None,
    engine: JobEngine | None = None,
) -> dict[str, Any]:
    total = sum(path.stat().st_size for path in paths)
    transferred = 0
    staged: list[dict[str, Any]] = []
    ftp = _ps4_ftp_connect(ip, timeout=15)
    try:
        try:
            ftp.mkd("/data/pkg")
        except Exception:
            pass
        config = _ps4_goldhen_config_hdd(ftp)
        for index, path in enumerate(paths, start=1):
            if job and job.cancel.is_set():
                raise Cancelled()
            remote = "/data/pkg/" + path.name
            existing = None
            try:
                existing = ftp.size(remote)
            except Exception:
                pass
            if existing == path.stat().st_size:
                transferred += path.stat().st_size
                staged.append(
                    {
                        "name": path.name,
                        "local_path": str(path),
                        "remote_path": remote,
                        "size": path.stat().st_size,
                        "skipped_existing": True,
                    }
                )
                if engine and job:
                    engine.progress(job, max(15.0, 90 * transferred / max(1, total)), f"Already staged: {path.name}")
                continue

            sent_this_file = 0

            def on_block(block: bytes) -> None:
                nonlocal transferred, sent_this_file
                if job and job.cancel.is_set():
                    raise Cancelled()
                size = len(block)
                transferred += size
                sent_this_file += size
                if engine and job:
                    engine.progress(
                        job,
                        max(15.0, min(90.0, 90.0 * transferred / max(1, total))),
                        f"Sending {index}/{len(paths)} to PS4: {path.name}",
                    )

            with path.open("rb") as handle:
                ftp.storbinary("STOR " + remote, handle, blocksize=1024 * 1024, callback=on_block)
            remote_size = ftp.size(remote)
            if remote_size != path.stat().st_size:
                raise IOError(f"PS4 FTP size mismatch for {path.name}: {remote_size} != {path.stat().st_size}")
            staged.append(
                {
                    "name": path.name,
                    "local_path": str(path),
                    "remote_path": remote,
                    "size": path.stat().st_size,
                    "skipped_existing": False,
                }
            )
        return {"config": config, "packages": staged}
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


class _PS4PackageRequestHandler(BaseHTTPRequestHandler):
    server_version = "iNSync-PS4-PKG/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _resolve(self) -> Path | None:
        parsed = urlparse(self.path)
        with PS4_SERVER_LOCK:
            path = PS4_PACKAGE_ROUTES.get(parsed.path)
        return path if path and path.is_file() else None

    def _send_file(self, head_only: bool = False) -> None:
        path = self._resolve()
        if not path:
            self.send_error(404)
            return
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = 200
        range_header = str(self.headers.get("Range") or "")
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = min(end, int(match.group(2)))
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_GET(self) -> None:
        self._send_file(False)

    def do_HEAD(self) -> None:
        self._send_file(True)


def _ps4_local_ip_for_remote(remote_ip: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_ip, PS4_FTP_PORT))
        return str(sock.getsockname()[0])
    finally:
        sock.close()


def _ps4_package_server(paths: list[Path], ps4_ip: str) -> tuple[str, list[str]]:
    global PS4_PACKAGE_SERVER, PS4_PACKAGE_SERVER_THREAD
    with PS4_SERVER_LOCK:
        if PS4_PACKAGE_SERVER is None:
            ports = [PS4_PACKAGE_HTTP_PORT, 0] if PS4_PACKAGE_HTTP_PORT else [0]
            last_error: Exception | None = None
            for port in ports:
                try:
                    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _PS4PackageRequestHandler)
                    server.daemon_threads = True
                    PS4_PACKAGE_SERVER = server
                    PS4_PACKAGE_SERVER_THREAD = threading.Thread(
                        target=server.serve_forever,
                        name="insync-ps4-pkg-http",
                        daemon=True,
                    )
                    PS4_PACKAGE_SERVER_THREAD.start()
                    break
                except OSError as exc:
                    last_error = exc
            if PS4_PACKAGE_SERVER is None:
                raise RuntimeError(f"Could not start PS4 package HTTP server: {last_error}")
        port = int(PS4_PACKAGE_SERVER.server_address[1])
        local_ip = _ps4_local_ip_for_remote(ps4_ip)
        urls: list[str] = []
        for path in paths:
            token = uuid.uuid4().hex
            route = f"/pkg/{token}/{quote(path.name)}"
            PS4_PACKAGE_ROUTES[route] = path
            urls.append(f"http://{local_ip}:{port}{route}")
        return f"{local_ip}:{port}", urls


def _ps4_companion_request(
    ip: str,
    endpoint: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = 6.0,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib_request.Request(
        f"http://{ip}:{PS4_COMPANION_PORT}{endpoint}",
        data=data,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            decoded = json.loads(raw or "{}")
            return decoded if isinstance(decoded, dict) else {"response": decoded}
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            return {"ok": False, "unauthorized": True, "http_status": 401, "response": body}
        raise RuntimeError(f"iNSync Companion HTTP {exc.code}: {body}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"iNSync Companion request failed: {exc}") from exc


def _ps4_companion_authenticated_status(ip: str) -> tuple[bool, dict[str, Any] | None]:
    token = _ps4_companion_token()
    if not token:
        return False, None
    try:
        data = _ps4_companion_request(ip, "/v1/status", token=token, timeout=1.2)
    except Exception:
        return False, None
    if data.get("unauthorized"):
        _ps4_save_companion_token("")
        return False, None
    return bool(data.get("ok")), data


def _ps4_companion_queue(ip: str) -> dict[str, Any]:
    token = _ps4_companion_token()
    if not token:
        return {"items": []}
    return _ps4_companion_request(ip, "/v1/queue", token=token, timeout=2.5)


def _ps4_companion_games(ip: str) -> dict[str, Any]:
    token = _ps4_companion_token()
    if not token:
        return {"games": []}
    return _ps4_companion_request(ip, "/v1/games", token=token, timeout=4.0)


def _ps4_rpi_request(ip: str, endpoint: str, payload: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib_request.Request(
        f"http://{ip}:{PS4_RPI_PORT}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    return decoded
                return {"response": decoded}
            except json.JSONDecodeError:
                return {"response": raw, "http_status": getattr(response, "status", 200)}
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"RPI HTTP {exc.code}: {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"RPI request failed: {exc}") from exc


def _ps4_find_task_id(payload: Any) -> int | None:
    if isinstance(payload, dict):
        for key in ("task_id", "taskId", "id"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        for value in payload.values():
            task_id = _ps4_find_task_id(value)
            if task_id is not None:
                return task_id
    if isinstance(payload, list):
        for value in payload:
            task_id = _ps4_find_task_id(value)
            if task_id is not None:
                return task_id
    return None


def _ps4_status_data() -> dict[str, Any]:
    ip = _ps4_discover_ip()
    if not ip:
        return unavailable("No GoldHEN PS4 found on the local network", console="playstation")
    ftp_ready = "GoldHEN FTP" in _ps4_ftp_banner(ip)
    companion_ready = _tcp_open(ip, PS4_COMPANION_PORT, 0.4)
    companion_paired = False
    companion_status: dict[str, Any] | None = None
    if companion_ready:
        companion_paired, companion_status = _ps4_companion_authenticated_status(ip)
    rpi_ready = _tcp_open(ip, PS4_RPI_PORT, 0.4)
    binloader_ready = _tcp_open(ip, PS4_BINLOADER_PORT, 0.4)
    klog_ready = _tcp_open(ip, PS4_KLOG_PORT, 0.4)
    staged: list[dict[str, Any]] = []
    if ftp_ready:
        try:
            ftp = _ps4_ftp_connect(ip, timeout=4)
            try:
                for name, facts in ftp.mlsd("/data/pkg"):
                    if name in (".", "..") or facts.get("type") != "file":
                        continue
                    if str(name).lower().endswith(".pkg"):
                        staged.append({"name": name, "size": int(facts.get("size") or 0)})
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()
        except Exception:
            pass
    companion_pkg = _bundled_ps4_companion_pkg()
    if companion_ready and companion_paired:
        message = f"PS4 {ip} ready — iNSync Companion paired"
        mode = "insync-companion"
    elif companion_ready:
        message = f"PS4 {ip} ready — iNSync Companion is open; pair to control installs"
        mode = "insync-companion-unpaired"
    elif rpi_ready:
        message = f"PS4 {ip} ready — Remote Package Installer can bootstrap iNSync Companion"
        mode = "rpi-bootstrap"
    elif ftp_ready:
        message = f"PS4 {ip} ready over GoldHEN FTP — iNSync Companion can be staged for one-time install"
        mode = "goldhen-ftp-bootstrap"
    else:
        message = f"PS4 {ip} detected but package transport is unavailable"
        mode = "unavailable"
    return ok(
        message,
        console="playstation",
        ip=ip,
        mode=mode,
        ftp_ready=ftp_ready,
        ftp_port=PS4_FTP_PORT,
        companion_ready=companion_ready,
        companion_paired=companion_paired,
        companion_port=PS4_COMPANION_PORT,
        companion_status=companion_status,
        companion_pkg_ready=bool(companion_pkg),
        companion_pkg_size=companion_pkg.stat().st_size if companion_pkg else 0,
        rpi_ready=rpi_ready,
        rpi_port=PS4_RPI_PORT,
        binloader_ready=binloader_ready,
        binloader_port=PS4_BINLOADER_PORT,
        klog_ready=klog_ready,
        klog_port=PS4_KLOG_PORT,
        staged_packages=staged,
        staged_count=len(staged),
    )

def console_status_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    console = str(job.params.get("console") or "console").strip().lower()
    if console != "playstation":
        return unavailable(f"{console} adapter is not qualified yet", console=console)
    engine.progress(job, 15, "Finding GoldHEN PS4")
    result = _ps4_status_data()
    engine.progress(job, 85, str(result.get("message") or "PS4 status ready"))
    return result


def console_companion_install_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    engine.progress(job, 8, "Finding PS4")
    status = _ps4_status_data()
    if not status.get("ok"):
        return status
    ip = str(status.get("ip") or "")
    if status.get("companion_ready"):
        return ok(
            "iNSync Companion is already installed and open",
            console="playstation",
            ip=ip,
            mode="insync-companion",
            companion_ready=True,
            companion_paired=bool(status.get("companion_paired")),
        )
    pkg = _bundled_ps4_companion_pkg()
    if not pkg:
        return unavailable("The bundled iNSync Companion PKG is missing", console="playstation", ip=ip)

    if status.get("rpi_ready"):
        engine.progress(job, 20, "Starting iNSync Companion package server")
        server, urls = _ps4_package_server([pkg], ip)
        req = urllib_request.Request(urls[0], method="HEAD")
        with urllib_request.urlopen(req, timeout=4) as response:
            if int(response.headers.get("Content-Length") or -1) != pkg.stat().st_size:
                raise IOError("iNSync Companion package server size mismatch")
        engine.progress(job, 55, "Installing iNSync Companion on PS4")
        response = _ps4_rpi_request(ip, "/api/install", {"type": "direct", "packages": urls}, timeout=12)
        engine.progress(job, 95, "Companion install request accepted")
        return ok(
            "iNSync Companion install started. Open iNSync Companion on the PS4 when installation completes.",
            console="playstation",
            ip=ip,
            mode="rpi-bootstrap",
            install_started=True,
            package_server=server,
            rpi_response=response,
            companion_pkg=str(pkg),
        )

    if status.get("ftp_ready"):
        engine.progress(job, 20, "Staging iNSync Companion through GoldHEN FTP")
        staged = _ps4_ftp_stage_packages(ip, [pkg], job, engine)
        engine.progress(job, 95, "iNSync Companion staged")
        return ok(
            "iNSync Companion is staged in /data/pkg. This PS4 currently exposes FTP only, so install it once from GoldHEN Package Installer, then open it and pair.",
            console="playstation",
            ip=ip,
            mode="goldhen-ftp-bootstrap",
            install_started=False,
            requires_console_install=True,
            companion_pkg=str(pkg),
            package_source="/data/pkg",
            **staged,
        )
    return unavailable(
        "PS4 found, but neither iNSync Companion, RPI nor GoldHEN FTP is available",
        console="playstation",
        ip=ip,
    )


def console_companion_pair_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    status = _ps4_status_data()
    if not status.get("ok"):
        return status
    ip = str(status.get("ip") or "")
    if not status.get("companion_ready"):
        return unavailable("Open iNSync Companion on the PS4 first", console="playstation", ip=ip)
    if status.get("companion_paired"):
        return ok("iNSync Companion is already paired", console="playstation", ip=ip, paired=True)

    request_id = uuid.uuid4().hex
    client = socket.gethostname() or "iNSync PC"
    engine.progress(job, 10, "Requesting PS4 pairing")
    response = _ps4_companion_request(
        ip,
        "/v1/pair/request",
        method="POST",
        payload={"request_id": request_id, "client": client},
        timeout=4,
    )
    if str(response.get("status") or "") != "waiting":
        return unavailable("Companion did not accept the pair request", response=response, ip=ip)

    wait_seconds = max(5.0, min(120.0, float(job.params.get("timeout") or 90.0)))
    deadline = time.monotonic() + wait_seconds
    engine.progress(job, 20, "Press X in iNSync Companion on the PS4 to pair")
    while time.monotonic() < deadline:
        result = _ps4_companion_request(
            ip,
            f"/v1/pair/status?request_id={quote(request_id)}",
            timeout=3,
        )
        state = str(result.get("status") or "")
        if state == "approved":
            token = str(result.get("token") or "")
            if not token:
                return unavailable("PS4 approved pairing but returned no token", ip=ip)
            _ps4_save_companion_token(token)
            engine.progress(job, 95, "PS4 paired")
            paired, companion_status = _ps4_companion_authenticated_status(ip)
            return ok(
                "iNSync Companion paired",
                console="playstation",
                ip=ip,
                paired=paired,
                companion_status=companion_status,
            )
        if state == "rejected":
            return unavailable("Pairing was rejected on the PS4", console="playstation", ip=ip)
        time.sleep(0.5)
    return unavailable(
        "Pair request is still waiting. Press X in iNSync Companion and try Pair again.",
        console="playstation",
        ip=ip,
        pairing_waiting=True,
    )


def console_companion_queue_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    status = _ps4_status_data()
    if not status.get("ok"):
        return status
    ip = str(status.get("ip") or "")
    if not status.get("companion_paired"):
        return unavailable("Pair iNSync Companion first", console="playstation", ip=ip)
    queue_data = _ps4_companion_queue(ip)
    return ok(
        "PS4 install queue refreshed",
        console="playstation",
        ip=ip,
        items=list(queue_data.get("items") or []),
    )


def console_companion_games_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    status = _ps4_status_data()
    if not status.get("ok"):
        return status
    ip = str(status.get("ip") or "")
    if not status.get("companion_paired"):
        return unavailable("Pair iNSync Companion first", console="playstation", ip=ip)
    games_data = _ps4_companion_games(ip)
    return ok(
        "Installed PS4 titles refreshed",
        console="playstation",
        ip=ip,
        games=list(games_data.get("games") or []),
    )


def console_companion_action_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    status = _ps4_status_data()
    if not status.get("ok"):
        return status
    ip = str(status.get("ip") or "")
    if not status.get("companion_paired"):
        return unavailable("Pair iNSync Companion first", console="playstation", ip=ip)
    try:
        item_id = int(job.params.get("id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    action = str(job.params.get("action") or "").strip().lower()
    if item_id <= 0 or action not in {"pause", "resume", "cancel", "top"}:
        return unavailable("Invalid PS4 queue action")
    result = _ps4_companion_request(
        ip,
        f"/v1/queue/{item_id}/{action}",
        method="POST",
        payload={},
        token=_ps4_companion_token(),
        timeout=4,
    )
    queue_data = _ps4_companion_queue(ip)
    return ok(
        f"PS4 queue action '{action}' sent",
        console="playstation",
        ip=ip,
        action=action,
        id=item_id,
        response=result,
        items=list(queue_data.get("items") or []),
    )


def console_pkg_job(job: Job, engine: JobEngine) -> dict[str, Any]:
    console = str(job.params.get("console") or "playstation").strip().lower()
    if console != "playstation":
        return unavailable(f"{console} package backend is not qualified yet", console=console)
    raw_paths = [str(x) for x in (job.params.get("paths") or []) if str(x or "").strip()]
    if not raw_paths:
        return unavailable("Choose one or more PS4 PKG files")
    paths = [Path(value) for value in raw_paths]
    invalid = [str(path) for path in paths if not path.is_file() or path.suffix.lower() != ".pkg"]
    if invalid:
        return unavailable("One or more selected PS4 packages are unavailable or not .pkg files", invalid=invalid)

    engine.progress(job, 8, "Finding iNSync Companion")
    status = _ps4_status_data()
    if not status.get("ok"):
        return status
    ip = str(status.get("ip") or "")
    if not status.get("companion_ready"):
        return unavailable(
            "Install and open iNSync Companion on the PS4 before sending game PKGs",
            console="playstation",
            ip=ip,
            requires_companion=True,
            bootstrap_mode=status.get("mode"),
            companion_pkg_ready=bool(status.get("companion_pkg_ready")),
        )
    if not status.get("companion_paired"):
        return unavailable(
            "Pair iNSync Companion before sending game PKGs",
            console="playstation",
            ip=ip,
            requires_pairing=True,
        )

    engine.progress(job, 20, "Starting local PKG server")
    server, urls = _ps4_package_server(paths, ip)
    for url, path in zip(urls, paths):
        req = urllib_request.Request(url, method="HEAD")
        with urllib_request.urlopen(req, timeout=4) as response:
            if int(response.headers.get("Content-Length") or -1) != path.stat().st_size:
                raise IOError(f"Local package server size mismatch for {path.name}")

    token = _ps4_companion_token()
    queued: list[dict[str, Any]] = []
    total = max(1, len(paths))
    for index, (path, url) in enumerate(zip(paths, urls), start=1):
        engine.progress(
            job,
            30 + (55 * (index - 1) / total),
            f"Queueing {path.name} on PS4",
        )
        response = _ps4_companion_request(
            ip,
            "/v1/queue/add",
            method="POST",
            payload={"url": url, "name": path.name, "size": path.stat().st_size},
            token=token,
            timeout=5,
        )
        if response.get("unauthorized"):
            _ps4_save_companion_token("")
            return unavailable(
                "Companion pairing expired; pair again before sending PKGs",
                console="playstation",
                ip=ip,
                requires_pairing=True,
            )
        queued.append(
            {
                "id": int(response.get("id") or 0),
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "url": url,
            }
        )
    queue_data = _ps4_companion_queue(ip)
    engine.progress(job, 95, f"{len(queued)} PKG(s) queued on PS4")
    return ok(
        f"{len(queued)} PKG(s) added to the iNSync Companion install queue",
        console="playstation",
        ip=ip,
        mode="insync-companion",
        install_started=True,
        package_server=server,
        packages=queued,
        items=list(queue_data.get("items") or []),
    )


OPERATIONS: dict[str, Callable[[Job, JobEngine], dict[str, Any]]] = {
    "sharing.status": sharing_status_job,
    "sharing.toggle": sharing_toggle,
    "adb.devices": adb_devices_job,
    "adb.info": adb_info_job,
    "adb.apps": adb_apps_job,
    "adb.app.icon": adb_app_icon_job,
    "adb.wifi.status": adb_wifi_status_job,
    "adb.wifi.enable": adb_wifi_enable_job,
    "adb.wifi.connect": adb_wifi_connect_job,
    "adb.wifi.disconnect": adb_wifi_disconnect_job,
    "adb.wifi.usb": adb_wifi_usb_job,
    "adb.wifi.pair": adb_wifi_pair_job,
    "adb.wifi.mdns": adb_wifi_mdns_job,
    "adb.media.list": adb_media_list_job,
    "adb.media.preview": adb_media_preview_job,
    "adb.media.pull": adb_media_pull_job,
    "adb.files.push": adb_files_push_job,
    "adb.media.delete": adb_media_delete_job,
    "adb.install": adb_install_job,
    "adb.uninstall": adb_uninstall_job,
    "adb.app.export": adb_app_export_job,
    "adb.disable": adb_disable_job,
    "files.copy": files_copy_job,
    "peer.files.send": peer_files_send_job,
    "peer.files.roots": peer_files_roots_job,
    "peer.files.list": peer_files_list_job,
    "peer.files.pull": peer_files_pull_job,
    "peer.list": peers_list_job,
    "peer.configure": peer_configure_job,
    "peer.approve": peer_approve_job,
    "clipboard.text": clipboard_send_text_job,
    "clipboard.image": clipboard_send_image_job,
    "ios.status": ios_status_job,
    "ios.validate": ios_validate_job,
    "ios.ipa": ios_ipa_job,
    "ios.apps": ios_apps_job,
    "ios.app.uninstall": ios_app_uninstall_job,
    "ios.media.list": ios_media_list_job,
    "ios.media.preview": ios_media_preview_job,
    "ios.media.pull": ios_media_pull_job,
    "ios.media.delete": ios_media_delete_job,
    "ios.documents.list": ios_documents_list_job,
    "ios.documents.pull": ios_documents_pull_job,
    "ios.documents.push": ios_documents_push_job,
    "ios.documents.mkdir": ios_documents_mkdir_job,
    "ios.documents.delete": ios_documents_delete_job,
    "console.status": console_status_job,
    "console.pkg": console_pkg_job,
    "console.companion.install": console_companion_install_job,
    "console.companion.pair": console_companion_pair_job,
    "console.companion.queue": console_companion_queue_job,
    "console.companion.games": console_companion_games_job,
    "console.companion.action": console_companion_action_job,
}


def snapshot() -> dict[str, Any]:
    adb = adb_path()
    modern_adb = adb_modern_path()
    return ok(
        "iNSync engine ready",
        platform=sys.platform,
        engine={"workers": len(ENGINE.workers), "queued": ENGINE.q.qsize()},
        capabilities={
            "network_sharing": sys.platform == "win32",
            "file_copy": True,
            "peer_files": True,
            "peer_file_browse": True,
            "adb": bool(adb),
            "adb_wifi": bool(adb) and bool(modern_adb),
            "adb_wifi_pair": bool(modern_adb),
            "adb_app_export": bool(adb),
            "adb_app_icons": bool(adb),
            "media_video_preview": bool(ffmpeg_path()),
            "android_media": bool(adb),
            "ios": _pmd_available() or bool(command_path("idevice_id")),
            "ios_bridge": _pmd_available(),
            "ios_media": _pmd_available(),
            "ios_apps": _pmd_available(),
            "ios_documents": _pmd_available(),
            "ipa_install": _pmd_available() or bool(command_path("ideviceinstaller")),
            "ipa_validation": True,
            "peer_discovery": True,
            "peer_roles": True,
            "console_pkg": "playstation-insync-companion",
            "clipboard_peer": True,
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
    ensure_peer_network()
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
