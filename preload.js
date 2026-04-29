const { contextBridge, ipcRenderer } = require('electron');

// Expose a minimal, safe API to the renderer (index.html)
contextBridge.exposeInMainWorld('ventus', {
    // Window chrome controls (used by custom title bar buttons)
    minimize:  () => ipcRenderer.send('window-minimize'),
    maximize:  () => ipcRenderer.send('window-maximize'),
    close:     () => ipcRenderer.send('window-close'),

    // Boot on startup toggle
    getStartup:     () => ipcRenderer.invoke('startup-get'),
    enableStartup:  () => ipcRenderer.invoke('startup-enable'),
    disableStartup: () => ipcRenderer.invoke('startup-disable'),
});
