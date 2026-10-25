"""Package an audited upgrade ZIP into a single Windows customer executable."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--hooks", type=Path)
    args = parser.parse_args()
    for name in ("payload", "manifest", "output", "work", "hooks"):
        value = getattr(args,name)
        if value is not None:
            setattr(args,name,value.resolve())
    repo = Path(__file__).resolve().parents[1]
    args.work.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    assert hashlib.sha256(args.payload.read_bytes()).hexdigest() == manifest["zip_sha256"]
    assert manifest["version"] == "1.6.25"
    commit = subprocess.check_output(["git","-c","safe.directory="+repo.as_posix(),"-C",str(repo),"rev-parse","HEAD"],text=True).strip()
    shutil.copy2(args.payload,args.work / "payload.zip")
    shutil.copy2(args.manifest,args.work / "payload-manifest.json")
    version = "VSVersionInfo(ffi=FixedFileInfo(filevers=(1,6,25,0),prodvers=(1,6,25,0),mask=0x3f,flags=0x0,OS=0x40004,fileType=0x1,subtype=0x0,date=(0,0)),kids=[StringFileInfo([StringTable('080404b0',[StringStruct('FileDescription','千牛助手自动升级'),StringStruct('FileVersion','1.6.25'),StringStruct('ProductName','Qianniu AI Service Auto Upgrade'),StringStruct('ProductVersion','1.6.25'),StringStruct('Comments','Upgrader source "+commit+"')])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])"
    (args.work / "version.txt").write_text(version,encoding="utf-8")
    command = [sys.executable,"-m","PyInstaller","--noconfirm","--onefile","--windowed","--uac-admin",
               "--name","QianniuAIService-1.6.25-AutoUpgrade","--distpath",str(args.work / "dist"),
               "--workpath",str(args.work / "cache"),"--specpath",str(args.work),"--paths",str(repo),
               "--hidden-import","tkinter.ttk","--hidden-import","tkinter.filedialog",
               "--add-data",str(args.work / "payload.zip")+";.",
               "--add-data",str(args.work / "payload-manifest.json")+";.",
               "--version-file",str(args.work / "version.txt")]
    if args.hooks:
        command += ["--additional-hooks-dir",str(args.hooks)]
    command.append(str(repo / "tools/upgrade_installed.py"))
    with (args.work / "build.log").open("w",encoding="utf-8") as log:
        subprocess.run(command,cwd=repo,stdout=log,stderr=subprocess.STDOUT,check=True)
    target = args.output / "千牛助手-1.6.25-自动升级.exe"
    shutil.copy2(args.work / "dist/QianniuAIService-1.6.25-AutoUpgrade.exe",target)
    result = {"version":"1.6.25","installer_type":"auto_detect_existing_installation",
              "app_source_commit":manifest["commit"],"upgrader_source_commit":commit,
              "exe_bytes":target.stat().st_size,"exe_sha256":hashlib.sha256(target.read_bytes()).hexdigest()}
    target.with_suffix(".json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    target.with_suffix(".sha256").write_text(result["exe_sha256"]+"  "+target.name+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))


if __name__ == "__main__":
    main()
