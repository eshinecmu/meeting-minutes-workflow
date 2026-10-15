/**
 * mmwf 桌面壳 · preload
 * ============================================================================
 *
 * 在 `contextIsolation: true` 下，渲染层拿不到 Node / Electron 的能力。
 * 这个文件是**唯一的**受控通道：只暴露界面上真正需要的几个方法。
 *
 * ⚠️ 设计原则：**暴露面越小越安全**。
 * 绝不暴露 `ipcRenderer` 本身或 `require` —— 那等于把整个主进程能力
 * 交出去，`contextIsolation` 就白设了。
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('mmwf', {
  /**
   * 取后端就绪信息 `{ port, token }`。
   * 渲染层必须在发第一个请求前调用它。
   */
  getBackendInfo: () => ipcRenderer.invoke('mmwf:backend-info'),

  /** 告知主进程当前操作人（用于 F8.3 的"正由 XXX 使用中"提示）。 */
  setOperator: (name) => ipcRenderer.invoke('mmwf:set-operator', name),
});
