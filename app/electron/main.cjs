const {app,BrowserWindow,ipcMain,screen,dialog,clipboard,nativeImage,Tray,Menu}=require("electron");
const path=require("node:path");
const fs=require("node:fs");
const crypto=require("node:crypto");
const {SidecarBridge}=require("./sidecar.cjs");
const manifest=require("./backend-manifest.json");

const WIDGET_WIDTH=224;
const WIDGET_HEIGHT=262;
const WIDGET_HANDLE=11;

const DEFAULT_STATE={
  mode:"connection",
  connection:"disconnected",
  clipboardEnabled:false,
  clipboardAuto:false,
  clipboardTargets:[],
  clipboardTypes:{text:true,image:true},
  clipboardDirection:"two-way"
};

let mainWindow=null;
let widgetWindow=null;
let bridge=null;
let tray=null;
let state={...DEFAULT_STATE,clipboardTypes:{...DEFAULT_STATE.clipboardTypes}};
let widgetDock={edge:"right",y:null,displayId:null};
let widgetExpanded=false;
let widgetProgrammaticMove=false;
let clipboardTimer=null;
let clipboardBusy=false;
let clipboardLast={text:"",image:""};

function prefs(){
  return {
    preload:path.join(__dirname,"preload.cjs"),
    contextIsolation:true,
    nodeIntegration:false,
    sandbox:true,
    webSecurity:true,
    devTools:!app.isPackaged
  };
}

function harden(win){
  win.webContents.setWindowOpenHandler(()=>({action:"deny"}));
  win.webContents.on("will-navigate",(e,url)=>{if(!url.startsWith("file:"))e.preventDefault()});
}

function runtimeStatePath(){
  return path.join(app.getPath("userData"),"insync-state.json");
}

function normalizeClipboardTargets(value){
  if(!Array.isArray(value))return [];
  return [...new Set(value.map(x=>String(x||"").trim()).filter(Boolean))].slice(0,100);
}

function normalizeRuntimeState(raw={}){
  const next={...DEFAULT_STATE,clipboardTypes:{...DEFAULT_STATE.clipboardTypes}};
  if(["connection","clipboard"].includes(raw.mode))next.mode=raw.mode;
  if(["disconnected","receiving","sending"].includes(raw.connection))next.connection=raw.connection;
  if(typeof raw.clipboardEnabled==="boolean")next.clipboardEnabled=raw.clipboardEnabled;
  if(typeof raw.clipboardAuto==="boolean")next.clipboardAuto=raw.clipboardAuto;
  next.clipboardTargets=normalizeClipboardTargets(raw.clipboardTargets);
  if(raw.clipboardTypes&&typeof raw.clipboardTypes==="object"){
    next.clipboardTypes={
      text:raw.clipboardTypes.text!==false,
      image:raw.clipboardTypes.image!==false
    };
  }
  if(["two-way","send","receive"].includes(raw.clipboardDirection))next.clipboardDirection=raw.clipboardDirection;
  return next;
}

function loadRuntimeState(){
  try{
    const raw=JSON.parse(fs.readFileSync(runtimeStatePath(),"utf8"));
    state=normalizeRuntimeState(raw.state||raw);
    const d=raw.widgetDock||{};
    widgetDock={
      edge:d.edge==="left"?"left":"right",
      y:(d.y!==null&&d.y!==undefined&&Number.isFinite(Number(d.y)))?Math.round(Number(d.y)):null,
      displayId:d.displayId??null
    };
  }catch{
    state=normalizeRuntimeState({});
  }
}

function saveRuntimeState(){
  try{
    const target=runtimeStatePath();
    fs.mkdirSync(path.dirname(target),{recursive:true});
    fs.writeFileSync(target,JSON.stringify({schema:1,state,widgetDock},null,2),"utf8");
  }catch{}
}

function sendToWindows(channel,payload){
  for(const win of [mainWindow,widgetWindow]){
    if(win&&!win.isDestroyed())win.webContents.send(channel,payload);
  }
}

function broadcastState(){sendToWindows("insync:state:update",state)}

function clipboardImageHash(image){
  if(!image||image.isEmpty())return "";
  try{return crypto.createHash("sha256").update(image.toPNG()).digest("hex")}catch{return ""}
}

function seedClipboardLast(){
  try{clipboardLast.text=clipboard.readText()||""}catch{clipboardLast.text=""}
  try{clipboardLast.image=clipboardImageHash(clipboard.readImage())}catch{clipboardLast.image=""}
}

async function queueClipboardOperation(operation,params){
  try{
    const b=await getBridge();
    await b.invoke("job.submit",{operation,params});
  }catch{}
}

async function clipboardTick(){
  if(clipboardBusy||!state.clipboardEnabled||!state.clipboardAuto)return;
  if(state.clipboardDirection==="receive")return;
  const targets=normalizeClipboardTargets(state.clipboardTargets);
  if(!targets.length)return;
  clipboardBusy=true;
  try{
    if(state.clipboardTypes?.text!==false){
      let text="";
      try{text=clipboard.readText()||""}catch{}
      if(text!==clipboardLast.text){
        clipboardLast.text=text;
        if(text)await queueClipboardOperation("clipboard.text",{text,targets,direction:state.clipboardDirection,automatic:true});
      }
    }
    if(state.clipboardTypes?.image!==false){
      let image=null,hash="";
      try{image=clipboard.readImage();hash=clipboardImageHash(image)}catch{}
      if(hash!==clipboardLast.image){
        clipboardLast.image=hash;
        if(hash&&image&&!image.isEmpty()){
          await queueClipboardOperation("clipboard.image",{data_url:image.toDataURL(),targets,direction:state.clipboardDirection,automatic:true});
        }
      }
    }
  }finally{
    clipboardBusy=false;
  }
}

function configureClipboardMonitor({seed=false}={}){
  if(seed)seedClipboardLast();
  if(clipboardTimer){clearInterval(clipboardTimer);clipboardTimer=null}
  if(state.clipboardEnabled&&state.clipboardAuto&&state.clipboardDirection!=="receive"){
    clipboardTimer=setInterval(()=>{clipboardTick()},450);
  }
}

function forwardBackendEvent(event){
  if(event?.type==="sharing.state"&&event.connection){
    state={...state,connection:event.connection};
    saveRuntimeState();
    broadcastState();
  }
  if(event?.type==="peer.clipboard"&&state.clipboardEnabled&&state.clipboardDirection!=="send"){
    if(event.kind==="text"&&state.clipboardTypes?.text!==false&&typeof event.text==="string"){
      clipboard.writeText(event.text);
      clipboardLast.text=event.text;
    }else if(event.kind==="image"&&state.clipboardTypes?.image!==false&&typeof event.data_url==="string"){
      const image=nativeImage.createFromDataURL(event.data_url);
      if(!image.isEmpty()){
        clipboard.writeImage(image);
        clipboardLast.image=clipboardImageHash(image);
      }
    }
  }
  sendToWindows("insync:backend:event",event);
}

async function getBridge(){
  if(!bridge){
    bridge=new SidecarBridge(app,manifest,forwardBackendEvent);
    await bridge.start();
  }
  return bridge;
}

function displayForDock(){
  const all=screen.getAllDisplays();
  const match=all.find(d=>String(d.id)===String(widgetDock.displayId));
  return match||screen.getPrimaryDisplay();
}

function widgetBounds(expanded=widgetExpanded){
  const display=displayForDock();
  const a=display.workArea;
  const minY=a.y;
  const maxY=Math.max(a.y,a.y+a.height-WIDGET_HEIGHT);
  const defaultY=Math.round(a.y+a.height*.36);
  const y=Math.max(minY,Math.min(maxY,Number.isFinite(widgetDock.y)?widgetDock.y:defaultY));
  const edge=widgetDock.edge==="left"?"left":"right";
  const x=edge==="left"
    ?(expanded?a.x:a.x-WIDGET_WIDTH+WIDGET_HANDLE)
    :(expanded?a.x+a.width-WIDGET_WIDTH:a.x+a.width-WIDGET_HANDLE);
  return {x,y,width:WIDGET_WIDTH,height:WIDGET_HEIGHT};
}

function persistDockFromWindow(){
  if(!widgetWindow||widgetWindow.isDestroyed())return;
  const bounds=widgetWindow.getBounds();
  const display=screen.getDisplayMatching(bounds);
  const a=display.workArea;
  const distLeft=Math.abs(bounds.x-a.x);
  const distRight=Math.abs((bounds.x+bounds.width)-(a.x+a.width));
  widgetDock={
    edge:distLeft<=distRight?"left":"right",
    y:Math.max(a.y,Math.min(a.y+a.height-WIDGET_HEIGHT,bounds.y)),
    displayId:display.id
  };
  saveRuntimeState();
}

function applyWidgetBounds(expanded){
  if(!widgetWindow||widgetWindow.isDestroyed())return;
  widgetExpanded=Boolean(expanded);
  widgetProgrammaticMove=true;
  widgetWindow.setBounds(widgetBounds(widgetExpanded),false);
  setTimeout(()=>{widgetProgrammaticMove=false},120);
}

function createWidget(){
  if(widgetWindow&&!widgetWindow.isDestroyed())return widgetWindow;
  widgetWindow=new BrowserWindow({
    ...widgetBounds(false),
    frame:false,
    transparent:true,
    backgroundColor:"#00000000",
    hasShadow:false,
    resizable:false,
    alwaysOnTop:true,
    skipTaskbar:true,
    show:false,
    webPreferences:prefs()
  });
  harden(widgetWindow);
  widgetWindow.loadFile(path.join(__dirname,"renderer","widget.html"));
  widgetWindow.on("moved",()=>{
    if(widgetProgrammaticMove)return;
    persistDockFromWindow();
    applyWidgetBounds(true);
  });
  widgetWindow.on("closed",()=>widgetWindow=null);
  return widgetWindow;
}

function setWidgetExpanded(expanded,focus=false){
  const w=createWidget();
  applyWidgetBounds(Boolean(expanded));
  if(!w.isVisible()){
    if(expanded)w.show();else w.showInactive();
  }
  if(expanded&&focus)w.focus();
  return {ok:true,expanded:widgetExpanded,dock:widgetDock};
}

function showWidget(expanded=false){
  const w=createWidget();
  if(mainWindow&&!mainWindow.isDestroyed())mainWindow.hide();
  setWidgetExpanded(expanded,expanded);
  if(!expanded)w.showInactive();
  broadcastState();
}

function createMain(){
  if(mainWindow&&!mainWindow.isDestroyed())return mainWindow;
  mainWindow=new BrowserWindow({
    width:820,
    height:750,
    minWidth:760,
    minHeight:695,
    maxWidth:900,
    maxHeight:825,
    center:true,
    frame:false,
    transparent:true,
    backgroundColor:"#00000000",
    hasShadow:false,
    resizable:true,
    maximizable:false,
    show:false,
    autoHideMenuBar:true,
    webPreferences:prefs()
  });
  harden(mainWindow);
  mainWindow.loadFile(path.join(__dirname,"renderer","index.html"));
  mainWindow.once("ready-to-show",()=>{mainWindow.show();broadcastState()});
  mainWindow.on("minimize",e=>{e.preventDefault();showWidget(false)});
  mainWindow.on("closed",()=>mainWindow=null);
  return mainWindow;
}

function revealMainWindow(w){
  if(!w||w.isDestroyed())return;
  if(w.isMinimized())w.restore();
  w.setSkipTaskbar(false);
  w.show();
  try{w.moveTop()}catch{}
  w.focus();
  if(widgetWindow&&!widgetWindow.isDestroyed())widgetWindow.hide();
  broadcastState();
}

function showMain(){
  const w=createMain();
  if(w.webContents.isLoadingMainFrame()){
    w.once("ready-to-show",()=>revealMainWindow(w));
    return;
  }
  revealMainWindow(w);
}

function createTray(){
  if(tray&&!tray.isDestroyed())return tray;
  const source=nativeImage.createFromPath(path.join(__dirname,"renderer","assets","insync-logo-transparent.png"));
  const icon=source.isEmpty()?nativeImage.createEmpty():source.resize({width:20,height:20,quality:"best"});
  tray=new Tray(icon);
  tray.setToolTip("iNSync");
  tray.on("double-click",()=>showMain());
  tray.setContextMenu(Menu.buildFromTemplate([
    {label:"Open iNSync",click:()=>showMain()},
    {label:"Show widget",click:()=>showWidget(true)},
    {type:"separator"},
    {label:"Exit",click:()=>app.quit()}
  ]));
  return tray;
}

function safeFilters(options={}){
  const requested=Array.isArray(options.filters)?options.filters:[];
  return requested.slice(0,8).map(item=>({
    name:String(item?.name||"Files").slice(0,60),
    extensions:(Array.isArray(item?.extensions)?item.extensions:[])
      .map(x=>String(x).replace(/[^A-Za-z0-9*]/g,"").slice(0,16))
      .filter(Boolean)
      .slice(0,16)
  })).filter(x=>x.extensions.length);
}

const gotLock=app.requestSingleInstanceLock();
if(!gotLock){
  app.quit();
}else{
  app.on("second-instance",()=>showMain());
  app.whenReady().then(()=>{
    loadRuntimeState();
    configureClipboardMonitor({seed:true});
    createTray();
    if(process.platform==="win32"&&app.isPackaged){
      try{app.setLoginItemSettings({openAtLogin:true,path:process.execPath,args:["--insync-startup"]})}catch{}
    }

    ipcMain.handle("insync:runtime",()=>({runtime:"electron",packaged:app.isPackaged,version:app.getVersion()}));
    ipcMain.handle("insync:window",(_e,action)=>{
      if(action==="minimize"){showWidget(false);return {ok:true}}
      if(action==="close"){showWidget(false);return {ok:true}}
      if(action==="exit"){app.quit();return {ok:true}}
      return {ok:false};
    });
    ipcMain.handle("insync:widget:minimize",()=>{showWidget(false);return {ok:true}});
    ipcMain.handle("insync:widget:expand",(_e,expanded)=>setWidgetExpanded(Boolean(expanded),Boolean(expanded)));
    ipcMain.handle("insync:main:show",()=>{showMain();return {ok:true}});
    ipcMain.handle("insync:state:get",()=>state);
    ipcMain.handle("insync:state:set",(_e,patch={})=>{
      const beforeAuto=state.clipboardEnabled&&state.clipboardAuto;
      const allowed={};
      if(["connection","clipboard"].includes(patch.mode))allowed.mode=patch.mode;
      if(["disconnected","receiving","sending"].includes(patch.connection))allowed.connection=patch.connection;
      if(typeof patch.clipboardEnabled==="boolean")allowed.clipboardEnabled=patch.clipboardEnabled;
      if(typeof patch.clipboardAuto==="boolean")allowed.clipboardAuto=patch.clipboardAuto;
      if(Array.isArray(patch.clipboardTargets))allowed.clipboardTargets=normalizeClipboardTargets(patch.clipboardTargets);
      if(patch.clipboardTypes&&typeof patch.clipboardTypes==="object"){
        allowed.clipboardTypes={
          text:patch.clipboardTypes.text!==false,
          image:patch.clipboardTypes.image!==false
        };
      }
      if(["two-way","send","receive"].includes(patch.clipboardDirection))allowed.clipboardDirection=patch.clipboardDirection;
      state={...state,...allowed};
      saveRuntimeState();
      const afterAuto=state.clipboardEnabled&&state.clipboardAuto;
      configureClipboardMonitor({seed:!beforeAuto&&afterAuto});
      broadcastState();
      return state;
    });
    ipcMain.handle("insync:dialog:files",async(_e,options={})=>{
      const result=await dialog.showOpenDialog(mainWindow||undefined,{
        title:String(options.title||"Choose files").slice(0,100),
        properties:["openFile",...(options.multi===false?[]:["multiSelections"])],
        filters:safeFilters(options)
      });
      return {canceled:result.canceled,paths:result.filePaths};
    });
    ipcMain.handle("insync:dialog:folder",async(_e,options={})=>{
      const result=await dialog.showOpenDialog(mainWindow||undefined,{
        title:String(options.title||"Choose folder").slice(0,100),
        properties:["openDirectory","createDirectory"]
      });
      return {canceled:result.canceled,path:result.filePaths[0]||""};
    });
    ipcMain.handle("insync:clipboard:text",()=>({text:clipboard.readText()}));
    ipcMain.handle("insync:clipboard:image",()=>{
      const image=clipboard.readImage();
      return {empty:image.isEmpty(),dataUrl:image.isEmpty()?"":image.toDataURL()};
    });
    ipcMain.handle("insync:backend",async(_e,method,params={})=>{
      const b=await getBridge();
      return b.invoke(method,params);
    });
    if(process.argv.includes("--insync-startup"))showWidget(false);
    else showMain();
  });
  app.on("window-all-closed",()=>{});
  app.on("activate",()=>showMain());
  app.on("before-quit",()=>{if(clipboardTimer)clearInterval(clipboardTimer);bridge?.stop();if(tray&&!tray.isDestroyed())tray.destroy()});
}
