const {app,BrowserWindow,ipcMain,screen}=require("electron");
const path=require("node:path");
const {SidecarBridge}=require("./sidecar.cjs");
const manifest=require("./backend-manifest.json");
let mainWindow=null,widgetWindow=null,bridge=null,quitting=false;
let uiState={mode:"connection",connection:"receiving",clipboardEnabled:true};
if(process.platform==="win32") app.disableHardwareAcceleration();
function prefs(){return {preload:path.join(__dirname,"preload.cjs"),contextIsolation:true,nodeIntegration:false,sandbox:true,webSecurity:true,devTools:!app.isPackaged}}
function harden(win){win.webContents.setWindowOpenHandler(()=>({action:"deny"}));win.webContents.on("will-navigate",(e,u)=>{if(!u.startsWith("file:"))e.preventDefault()})}
async function ensureBridge(){if(!bridge){bridge=new SidecarBridge(app,manifest);await bridge.start()}return bridge}
function pushState(){for(const w of [mainWindow,widgetWindow])if(w&&!w.isDestroyed())w.webContents.send("insync:state:update",uiState)}
function widgetBounds(){const a=screen.getPrimaryDisplay().workArea;return {x:a.x+a.width-246,y:Math.round(a.y+a.height*.38),width:236,height:248}}
function createWidget(){
 if(widgetWindow&&!widgetWindow.isDestroyed())return widgetWindow;
 widgetWindow=new BrowserWindow({...widgetBounds(),frame:false,transparent:true,resizable:false,alwaysOnTop:true,skipTaskbar:true,show:false,backgroundColor:"#00000000",webPreferences:prefs()});
 harden(widgetWindow);widgetWindow.loadFile(path.join(__dirname,"renderer","widget.html"));widgetWindow.on("closed",()=>widgetWindow=null);return widgetWindow;
}
function showWidget(){const w=createWidget();if(mainWindow&&!mainWindow.isDestroyed())mainWindow.hide();w.show();w.focus();pushState()}
function createMain(){
 if(mainWindow&&!mainWindow.isDestroyed())return mainWindow;
 const a=screen.getPrimaryDisplay().workArea;const width=Math.max(840,Math.min(1120,a.width-60));const height=Math.max(700,Math.min(980,a.height-44));
 mainWindow=new BrowserWindow({width,height,minWidth:760,minHeight:620,center:true,frame:false,show:false,backgroundColor:"#000000",autoHideMenuBar:true,webPreferences:prefs()});
 harden(mainWindow);mainWindow.loadFile(path.join(__dirname,"renderer","index.html"));
 mainWindow.once("ready-to-show",()=>mainWindow.show());
 mainWindow.on("minimize",e=>{e.preventDefault();showWidget()});
 mainWindow.on("close",e=>{if(!quitting){quitting=true;}});
 mainWindow.on("closed",()=>mainWindow=null);return mainWindow;
}
function showMain(){const w=createMain();if(widgetWindow&&!widgetWindow.isDestroyed())widgetWindow.hide();if(w.isMinimized())w.restore();w.show();w.focus();pushState()}
app.whenReady().then(()=>{
 ipcMain.handle("insync:runtime",()=>({runtime:"electron",packaged:app.isPackaged,version:app.getVersion()}));
 ipcMain.handle("insync:window",(_e,action)=>{if(action==="minimize"){showWidget();return {ok:true}}if(action==="maximize"){if(mainWindow?.isMaximized())mainWindow.unmaximize();else mainWindow?.maximize();return {ok:true,maximized:!!mainWindow?.isMaximized()}}if(action==="close"){quitting=true;app.quit();return {ok:true}}return {ok:false}});
 ipcMain.handle("insync:widget:minimize",()=>{showWidget();return {ok:true}});
 ipcMain.handle("insync:main:show",()=>{showMain();return {ok:true}});
 ipcMain.handle("insync:state:get",()=>uiState);
 ipcMain.handle("insync:state:set",(_e,patch={})=>{uiState={...uiState,...patch};pushState();return uiState});
 ipcMain.handle("insync:backend",async(_e,method,params={})=>{const b=await ensureBridge();return b.invoke(method,params)});
 createWidget();showMain();
});
app.on("window-all-closed",()=>{if(process.platform!=="darwin")app.quit()});
app.on("activate",()=>showMain());
app.on("before-quit",()=>{quitting=true;bridge?.stop()});