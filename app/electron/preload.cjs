const {contextBridge,ipcRenderer}=require("electron");
function sub(channel,cb){
  if(typeof cb!=="function")return()=>{};
  const handler=(_e,payload)=>cb(payload);
  ipcRenderer.on(channel,handler);
  return()=>ipcRenderer.removeListener(channel,handler);
}
contextBridge.exposeInMainWorld("ttg",Object.freeze({
  runtime:()=>ipcRenderer.invoke("insync:runtime"),
  windowControl:(action)=>ipcRenderer.invoke("insync:window",action),
  widgetMinimize:()=>ipcRenderer.invoke("insync:widget:minimize"),
  widgetExpand:(expanded)=>ipcRenderer.invoke("insync:widget:expand",!!expanded),
  showMain:()=>ipcRenderer.invoke("insync:main:show"),
  stateGet:()=>ipcRenderer.invoke("insync:state:get"),
  stateSet:(patch)=>ipcRenderer.invoke("insync:state:set",patch||{}),
  onState:(cb)=>sub("insync:state:update",cb),
  onBackendEvent:(cb)=>sub("insync:backend:event",cb),
  dialog:Object.freeze({
    openFiles:(options={})=>ipcRenderer.invoke("insync:dialog:files",options),
    openFolder:(options={})=>ipcRenderer.invoke("insync:dialog:folder",options)
  }),
  clipboard:Object.freeze({
    readText:()=>ipcRenderer.invoke("insync:clipboard:text"),
    readImage:()=>ipcRenderer.invoke("insync:clipboard:image")
  }),
  backend:Object.freeze({
    invoke:(method,params={})=>ipcRenderer.invoke("insync:backend",method,params),
    submit:(operation,params={})=>ipcRenderer.invoke("insync:backend","job.submit",{operation,params}),
    cancel:(jobId)=>ipcRenderer.invoke("insync:backend","job.cancel",{job_id:jobId}),
    status:(jobId)=>ipcRenderer.invoke("insync:backend","job.status",{job_id:jobId}),
    list:()=>ipcRenderer.invoke("insync:backend","job.list",{})
  })
}));