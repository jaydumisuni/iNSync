const {spawn}=require("node:child_process");
const fs=require("node:fs");
const path=require("node:path");
const readline=require("node:readline");

class SidecarBridge{
  constructor(app,manifest,onEvent=null){
    this.app=app;
    this.manifest=manifest||{};
    this.onEvent=typeof onEvent==="function"?onEvent:()=>{};
    this.child=null;
    this.pending=new Map();
    this.nextId=1;
  }
  _profile(){
    if(this.app.isPackaged&&this.manifest.packaged)return this.manifest.packaged;
    if(!this.app.isPackaged&&this.manifest.development)return this.manifest.development;
    return this.manifest;
  }
  get configured(){return Boolean(this._profile()?.executable)}
  _resolve(profile){
    const raw=String(profile.executable||"");
    const isBare=!path.isAbsolute(raw)&&!raw.includes("/")&&!raw.includes("\\");
    if(isBare)return raw;
    const root=this.app.isPackaged?process.resourcesPath:__dirname;
    let exe=path.resolve(root,raw);
    if(process.platform==="win32"&&!path.extname(exe)&&fs.existsSync(exe+".exe"))exe+=".exe";
    return exe;
  }
  async start(){
    if(!this.configured||this.child)return;
    const profile=this._profile();
    const exe=this._resolve(profile);
    const args=Array.isArray(profile.args)?profile.args:[];
    this.child=spawn(exe,args,{
      cwd:profile.cwd?path.resolve(this.app.isPackaged?process.resourcesPath:__dirname,profile.cwd):path.dirname(exe),
      shell:false,
      windowsHide:true,
      stdio:["pipe","pipe","pipe"],
      env:{...process.env,...(profile.env||{}),TTG_UI_RUNTIME:"electron",PYTHONIOENCODING:"utf-8",PYTHONUTF8:"1"}
    });
    readline.createInterface({input:this.child.stdout}).on("line",line=>this._line(line));
    this.child.stderr?.on("data",chunk=>{if(!this.app.isPackaged)process.stderr.write(chunk)});
    this.child.on("exit",(code)=>{
      for(const q of this.pending.values())q.reject(new Error("backend exited"));
      this.pending.clear();
      this.child=null;
      this.onEvent({type:"engine.exit",code});
    });
  }
  _line(line){
    let message;
    try{message=JSON.parse(line)}catch{return}
    if(message&&message.event){
      this.onEvent({type:String(message.event),...(message.data||{})});
      return;
    }
    const q=this.pending.get(message.id);
    if(!q)return;
    this.pending.delete(message.id);
    message.error?q.reject(new Error(String(message.error))):q.resolve(message.result);
  }
  async invoke(method,params={}){
    if(!new Set(this.manifest.allowedMethods||[]).has(method))throw new Error("backend method not allowed: "+method);
    await this.start();
    if(!this.child?.stdin?.writable)throw new Error("backend unavailable");
    const id=this.nextId++;
    return new Promise((resolve,reject)=>{
      this.pending.set(id,{resolve,reject});
      this.child.stdin.write(JSON.stringify({id,method,params})+"\n");
    });
  }
  stop(){
    if(this.child){this.child.kill();this.child=null}
  }
}
module.exports={SidecarBridge};
