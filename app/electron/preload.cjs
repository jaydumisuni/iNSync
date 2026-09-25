const { contextBridge, ipcRenderer } = require("electron");
function sub(channel, cb){ if(typeof cb!=="function") return ()=>{}; const h=(_e,p)=>cb(p); ipcRenderer.on(channel,h); return ()=>ipcRenderer.removeListener(channel,h); }
contextBridge.exposeInMainWorld("ttg", Object.freeze({
  runtime:()=>ipcRenderer.invoke("insync:runtime"),
  windowControl:(action)=>ipcRenderer.invoke("insync:window",action),
  widgetMinimize:()=>ipcRenderer.invoke("insync:widget:minimize"),
  showMain:()=>ipcRenderer.invoke("insync:main:show"),
  stateGet:()=>ipcRenderer.invoke("insync:state:get"),
  stateSet:(patch)=>ipcRenderer.invoke("insync:state:set",patch||{}),
  onState:(cb)=>sub("insync:state:update",cb),
  backend:Object.freeze({invoke:(method,params={})=>ipcRenderer.invoke("insync:backend",method,params)})
}));