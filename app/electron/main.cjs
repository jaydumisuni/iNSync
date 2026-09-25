const {app,BrowserWindow,ipcMain,screen,dialog,clipboard,nativeImage}=require("electron");
const path=require("node:path");
const {SidecarBridge}=require("./sidecar.cjs");
const manifest=require("./backend-manifest.json");

let mainWindow=null;
let widgetWindow=null;
let bridge=null;
let state={mode:"connection",connection:"disconnected",clipboardEnabled:true};

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
function sendToWindows(channel,payload){
  for(const win of [mainWindow,widgetWindow]){
    if(win&&!win.isDestroyed())win.webContents.send(channel,payload);
  }
}
function broadcastState(){sendToWindows("insync:state:update",state)}
function forwardBackendEvent(event){
  if(event?.type==="sharing.state"&&event.connection){
    state={...state,connection:event.connection};
    broadcastState();
  }
  if(event?.type==="peer.clipboard"&&state.clipboardEnabled){
    if(event.kind==="text"&&typeof event.text==="string"){
      clipboard.writeText(event.text);
    }else if(event.kind==="image"&&typeof event.data_url==="string"){
      const image=nativeImage.createFromDataURL(event.data_url);
      if(!image.isEmpty())clipboard.writeImage(image);
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
function widgetBounds(){
  const a=screen.getPrimaryDisplay().workArea;
  return {x:a.x+a.width-224,y:Math.round(a.y+a.height*.36),width:224,height:262};
}
function createWidget(){
  if(widgetWindow&&!widgetWindow.isDestroyed())return widgetWindow;
  widgetWindow=new BrowserWindow({
    ...widgetBounds(),
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
  widgetWindow.on("closed",()=>widgetWindow=null);
  return widgetWindow;
}
function showWidget(){
  const w=createWidget();
  if(mainWindow&&!mainWindow.isDestroyed())mainWindow.hide();
  w.setBounds(widgetBounds(),false);
  w.show();
  w.focus();
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
  mainWindow.on("minimize",e=>{e.preventDefault();showWidget()});
  mainWindow.on("closed",()=>mainWindow=null);
  return mainWindow;
}
function showMain(){
  const w=createMain();
  if(widgetWindow&&!widgetWindow.isDestroyed())widgetWindow.hide();
  if(w.isMinimized())w.restore();
  w.show();
  w.focus();
  broadcastState();
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
    ipcMain.handle("insync:runtime",()=>({runtime:"electron",packaged:app.isPackaged,version:app.getVersion()}));
    ipcMain.handle("insync:window",(_e,action)=>{
      if(action==="minimize"){showWidget();return {ok:true}}
      if(action==="close"){app.quit();return {ok:true}}
      return {ok:false};
    });
    ipcMain.handle("insync:widget:minimize",()=>{showWidget();return {ok:true}});
    ipcMain.handle("insync:main:show",()=>{showMain();return {ok:true}});
    ipcMain.handle("insync:state:get",()=>state);
    ipcMain.handle("insync:state:set",(_e,patch={})=>{
      const allowed={};
      if(["connection","clipboard"].includes(patch.mode))allowed.mode=patch.mode;
      if(["disconnected","receiving","sending"].includes(patch.connection))allowed.connection=patch.connection;
      if(typeof patch.clipboardEnabled==="boolean")allowed.clipboardEnabled=patch.clipboardEnabled;
      state={...state,...allowed};
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
    showMain();
  });
  app.on("window-all-closed",()=>{if(process.platform!=="darwin")app.quit()});
  app.on("activate",()=>showMain());
  app.on("before-quit",()=>bridge?.stop());
}
