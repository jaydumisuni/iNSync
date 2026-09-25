#!/usr/bin/env python3
from __future__ import annotations
import json, os, shutil, subprocess, sys
from pathlib import Path

def ok(message="", **extra): return {"ok": True, "message": message, **extra}
def unavailable(message, **extra): return {"ok": False, "available": False, "message": message, **extra}

def run(cmd, timeout=20):
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,shell=False)
    return p.returncode,p.stdout.strip(),p.stderr.strip()

def adb_path(): return shutil.which("adb")

def adb_devices(_):
    adb=adb_path()
    if not adb: return unavailable("ADB backend not installed on this host", devices=[])
    rc,out,err=run([adb,"devices","-l"])
    devices=[]
    if rc==0:
        for line in out.splitlines()[1:]:
            if not line.strip(): continue
            parts=line.split()
            devices.append({"serial":parts[0],"state":parts[1] if len(parts)>1 else "unknown","detail":" ".join(parts[2:])})
    return {"ok":rc==0,"available":True,"devices":devices,"error":err if rc else ""}

def adb_apps(params):
    adb=adb_path()
    if not adb: return unavailable("ADB backend not installed on this host", apps=[])
    serial=str(params.get("serial") or "")
    cmd=[adb]+(["-s",serial] if serial else [])+["shell","pm","list","packages","-3"]
    rc,out,err=run(cmd)
    apps=[x.split("package:",1)[-1].strip() for x in out.splitlines() if x.strip().startswith("package:")] if rc==0 else []
    return {"ok":rc==0,"available":True,"apps":apps,"error":err if rc else ""}

def adb_install(params):
    path=Path(str(params.get("path") or ""))
    if not path.is_file(): return unavailable("Choose an APK before installing")
    adb=adb_path()
    if not adb: return unavailable("ADB backend not installed on this host")
    serial=str(params.get("serial") or "")
    cmd=[adb]+(["-s",serial] if serial else [])+["install","-r",str(path)]
    rc,out,err=run(cmd,timeout=300)
    return {"ok":rc==0,"available":True,"message":out or err}

def adb_uninstall(params):
    package=str(params.get("package") or "").strip()
    if not package: return unavailable("Package name required")
    adb=adb_path()
    if not adb: return unavailable("ADB backend not installed on this host")
    serial=str(params.get("serial") or "")
    cmd=[adb]+(["-s",serial] if serial else [])+["uninstall",package]
    rc,out,err=run(cmd,timeout=60)
    return {"ok":rc==0,"available":True,"message":out or err}

def dispatch(method, params):
    if method=="app.snapshot":
        return ok("iNSync backend ready",platform=sys.platform,adb=bool(adb_path()),
                  capabilities={"network_sharing":sys.platform=="win32","adb":bool(adb_path()),"ios":"adapter-pending","console_pkg":"adapter-pending","clipboard_peer":"adapter-pending"})
    if method=="sharing.status":
        if sys.platform!="win32": return unavailable("Windows sharing adapter is not active on this host", platform=sys.platform)
        return ok("Windows host detected; proven ATHENA sharing adapter must be bound during Windows qualification", available=True, qualified=False)
    if method=="sharing.toggle":
        if sys.platform!="win32": return unavailable("Sharing mutation is Windows-only for this build", platform=sys.platform)
        return unavailable("ATHENA sharing adapter is intentionally not rebound until Windows physical qualification", qualified=False)
    if method=="adb.devices": return adb_devices(params)
    if method=="adb.apps": return adb_apps(params)
    if method=="adb.install": return adb_install(params)
    if method=="adb.uninstall": return adb_uninstall(params)
    if method=="clipboard.text": return unavailable("Peer clipboard transport adapter not qualified yet", payload="text")
    if method=="clipboard.image": return unavailable("Peer clipboard transport adapter not qualified yet", payload="image")
    if method=="ios.status": return unavailable("iOS device-service adapter not qualified yet")
    if method=="ios.ipa": return unavailable("IPA signing/install adapter not qualified yet")
    if method.startswith("console."): return unavailable("Console adapter not qualified yet")
    if method.startswith("files."): return ok("File transport contract ready; peer transfer adapter pending", qualified=False)
    raise ValueError("unknown method: "+method)

def main():
    for raw in sys.stdin:
        try:
            req=json.loads(raw); rid=req.get("id"); method=str(req.get("method") or ""); params=req.get("params") or {}
            result=dispatch(method,params)
            sys.stdout.write(json.dumps({"id":rid,"result":result},separators=(",",":"))+"\n"); sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(json.dumps({"id":req.get("id") if isinstance(locals().get("req"),dict) else None,"error":str(e)},separators=(",",":"))+"\n"); sys.stdout.flush()

if __name__=="__main__": main()
