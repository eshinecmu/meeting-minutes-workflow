/**
 * mmwf 桌面壳 · 主进程
 * ============================================================================
 *
 * 职责只有三件（其余全在渲染层与 Python 服务）：
 *   ① 单实例锁（F8.3）—— 同一台设备同一时间只允许一个会话
 *   ② 托管 Python 子进程 —— 拉起、转发日志、退出时收尸
 *   ③ 把「后端就绪信息（端口 + 令牌）」交给渲染层
 *
 * ⚠️ 本文件**不含任何业务规则**（SAD M01：表现层不含业务规则）。
 * 它只做进程编排与信息传递。
 */

const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const net = require('net');

// ---------------------------------------------------------------------------
// 路径契约（★ 必须与 Python 侧 `deps.app_dir()` 逐段对齐，SAD §11）
//
//   Python：`%LOCALAPPDATA%/mmwf/{port, session.token}`
//   Electron：`os.homedir()/AppData/Local/mmwf/{port, session.token}`
//
// ⚠️ 为什么要逐段对齐而不是各算各的：这两个文件是**跨进程的唯一握手通道**，
// 路径差一个字符，前端就永远读不到 —— 表现是"后端启动了但前端一直转圈"，
// 排查起来极费时间。所以这里刻意写成与 T1-11 验收脚本同一种拼法。
// ---------------------------------------------------------------------------
function mmwfUserDir() {
  // Windows 固定走 AppData/Local；非 Windows 退到用户主目录（与 Python 侧一致）。
  if (process.platform === 'win32') {
    return path.join(os.homedir(), 'AppData', 'Local', 'mmwf');
  }
  return path.join(os.homedir(), 'mmwf');
}

const PORT_FILE = path.join(mmwfUserDir(), 'port');
const TOKEN_FILE = path.join(mmwfUserDir(), 'session.token');

// ---------------------------------------------------------------------------
// 运行期根目录：开发期 vs 打包后（★ 本卡新增补强，原施工卡骨架没有）
//
// ⚠️ 原骨架写死 `process.resourcesPath`，那是**打包后**才存在的路径。
// 后果：本地开发根本跑不起来，只能等打包才能第一次验证 —— 而打包本身
// 又要先能跑。这里做分支，让开发期能直接指到项目里的 `.venv` 与 `src/`。
//
// 判据用 `app.isPackaged`（Electron 官方 API），不要用 `__dirname` 推断 ——
// 后者在 asar 里行为与解包时不同。
// ---------------------------------------------------------------------------
function resourcesRoot() {
  if (app.isPackaged) {
    return process.resourcesPath;
  }
  // 开发期：src/desktop/main.js -> 上两级就是项目根
  return path.resolve(__dirname, '..', '..');
}

/** Python 解释器路径。开发期优先用项目 `.venv`，其次系统 `python`。 */
function pythonPath() {
  if (app.isPackaged) {
    return process.platform === 'win32'
      ? path.join(process.resourcesPath, 'python', 'python.exe')
      : path.join(process.resourcesPath, 'python', 'bin', 'python');
  }
  const venv =
    process.platform === 'win32'
      ? path.join(resourcesRoot(), '.venv', 'Scripts', 'python.exe')
      : path.join(resourcesRoot(), '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : 'python';
}

/** 后端工作目录（`python -m mmwf.main` 要能 import 到 mmwf）。 */
function backendCwd() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app');
  }
  return path.join(resourcesRoot(), 'src');
}

// ---------------------------------------------------------------------------
// 日志
//
// ⚠️⚠️ 后端 stdout/stderr **绝对不能 `ignore`**（本卡新增补强）。
// 原骨架写的是 `stdio: ['ignore', 'pipe', 'pipe']` 但从不读 pipe，
// pipe 写满后**子进程会阻塞在写日志上**；而且后端启动失败
// （缺依赖 / 迁移报错 / 配置错）时，唯一的线索就在这两条流里。
// 全丢掉的结果是：界面只能白屏，排查没有入口。
// ---------------------------------------------------------------------------
function backendLogFile() {
  const dir = path.join(mmwfUserDir(), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'backend.log');
}

function log(scope, chunk) {
  const text = chunk.toString('utf-8');
  process.stdout.write(`[${scope}] ${text}`);
  try {
    fs.appendFileSync(backendLogFile(), `[${scope}] ${text}`, 'utf-8');
  } catch {
    // 日志写不进磁盘不该拖垮程序；控制台那份还在。
  }
}

// ---------------------------------------------------------------------------
// 后端进程
// ---------------------------------------------------------------------------
let pyProc = null;
let backendInfo = null; // { port, token }
let mainWindow = null;
let backendFailed = false;

function startBackend() {
  return new Promise((resolve, reject) => {
    const py = pythonPath();
    log('mmwf', `启动后端：${py} -m mmwf.main（cwd=${backendCwd()}）\n`);

    pyProc = spawn(py, ['-m', 'mmwf.main'], {
      cwd: backendCwd(),
      env: {
        ...process.env,
        // ★ 铁律 1 的运行时版本：Windows 上不设这个，中文 stderr 会乱码。
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    // 必须持续消费两条流，否则 pipe 缓冲写满会阻塞子进程。
    pyProc.stdout.on('data', (d) => log('backend', d));
    pyProc.stderr.on('data', (d) => log('backend', d));

    pyProc.on('error', (err) => {
      backendFailed = true;
      reject(new Error(`无法启动后端进程（${py}）：${err.message}`));
    });

    pyProc.on('exit', (code, signal) => {
      log('mmwf', `后端进程退出：code=${code} signal=${signal}\n`);
      if (!backendInfo && !backendFailed) {
        // 还没握手成功就退出了 —— 这是"启动失败"，不是"正常关闭"。
        backendFailed = true;
        reject(
          new Error(
            `后端启动失败（退出码 ${code}）。详情见日志：${backendLogFile()}`
          )
        );
      } else if (backendInfo) {
        // 运行中崩溃：告知使用者，别让人对着白屏猜。
        notifyUnexpectedExit(code);
      }
    });

    // 轮询端口/令牌文件（Python 侧顺序：先落两个文件、再 listen）
    const deadline = Date.now() + 30000;
    const timer = setInterval(() => {
      if (Date.now() > deadline) {
        clearInterval(timer);
        backendFailed = true;
        reject(
          new Error(
            `后端 30 秒内未就绪。请检查日志：${backendLogFile()}`
          )
        );
        return;
      }
      try {
        if (fs.existsSync(PORT_FILE) && fs.existsSync(TOKEN_FILE)) {
          const port = parseInt(fs.readFileSync(PORT_FILE, 'utf-8').trim(), 10);
          const token = fs.readFileSync(TOKEN_FILE, 'utf-8').trim();
          if (!Number.isInteger(port) || port <= 0 || !token) return;

          // ⚠️★ 端口文件是"先落文件、后 listen"（T1-11 实测）。
          // 文件出现 ≠ 能连 —— 直接发请求会吃到"连接被拒"，
          // 而那是**竞态**、不是缺陷。这里先探一次 TCP 再放行。
          probeTcp(port)
            .then(() => {
              clearInterval(timer);
              backendInfo = { port, token };
              resolve(backendInfo);
            })
            .catch(() => {
              /* 还没 listen，下一轮再试 */
            });
        }
      } catch {
        /* 文件正在写、读半截，下一轮再试 */
      }
    }, 300);
  });
}

/** 探一次 TCP：能连上说明真的 listen 了。 */
function probeTcp(port, timeoutMs = 500) {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: '127.0.0.1', port });
    const done = (err) => {
      sock.removeAllListeners();
      sock.destroy();
      err ? reject(err) : resolve();
    };
    sock.setTimeout(timeoutMs, () => done(new Error('probe timeout')));
    sock.once('connect', () => done());
    sock.once('error', (e) => done(e));
  });
}

function stopBackend() {
  if (pyProc && pyProc.exitCode === null && !pyProc.killed) {
    try {
      pyProc.kill();
    } catch {
      /* 已经没了 */
    }
  }
}

/** 运行中崩溃时弹窗 —— 别让使用者对着白屏猜发生了什么。 */
function notifyUnexpectedExit(code) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  dialog.showMessageBox(mainWindow, {
    type: 'error',
    title: '会议纪要工作台',
    message: '后台服务意外停止',
    detail: `程序需要重新启动。（错误编号 ${code}）\n日志：${backendLogFile()}`,
    buttons: ['退出程序'],
  }).finally(() => app.quit());
}

// ---------------------------------------------------------------------------
// 单实例锁（F8.3）
//
// ⚠️ 顺序很关键：`requestSingleInstanceLock()` 必须在 `app.whenReady()` **之前**
// 调用，且第二实例要立刻 `quit()` —— 否则第二个窗口会先建出来再退，
// 使用者会看到窗口闪一下。
// ---------------------------------------------------------------------------
//: 一期操作人（下拉 + 手输，不做密码，SAD §11.3）。渲染层登录后通过 IPC 告知。
// ---------------------------------------------------------------------------
// 图形加速兜底（★ 打包前实测新增，必须有）
//
// 实测现象（本机 Electron 30.5.1）：不关硬件加速时，GPU 子进程反复崩溃 ——
//
//     ERROR:gpu_process_host.cc(997)] GPU process exited unexpectedly:
//                                    exit_code=-1073741819      (0xC0000005 访问冲突)
//     FATAL:gpu_data_manager_impl_private.cc(448)] GPU process isn't usable. Goodbye.
//
// 结果是 **Electron 直接退出**：窗口没出来、后端也被连带收尸，
// 使用者只看到"双击了没反应"。**没有任何可读的错误提示。**
//
// ⚠️ 这不是本机特有的怪毛病，而是**虚拟机 / 远程桌面 / 受管办公机 / 老显卡驱动**
// 下的常见问题（Chromium 系共有的老毛病）。而本项目的目标机恰恰大量是
// 党政机关的虚拟桌面与办公机 —— 在那上面"启动即死且无提示"是不可接受的。
//
// 为什么可以直接关掉而不是"失败了再重试"：
//   本应用的界面是**纯表单**（提交 / 复核 / 闸口 / 导出），
//   没有 3D、没有 WebGL、没有 canvas 动画。硬件加速在这里**没有任何收益**，
//   却引入了一条"崩溃即不可用"的失败路径。代价为零、收益是消除一类死机。
//
// 留了逃生门：确实需要 GPU 时设 `MMWF_ENABLE_GPU=1`。
// 将来若界面引入图表/富媒体渲染，再评估是否默认开启。
//
// ---------------------------------------------------------------------------
// ★★ T1 收尾实测更正：光关 GPU 不够，根因是**沙箱起不来** ★★
//
// 打包产物上继续排查（工具：tools/probe_electron_flags.py、
// tools/probe_electron_dev.py、tools/cdp_probe.py、tools/cdp_matrix.py），
// 用 CDP 直接问渲染层 DOM，得到一条**只看进程数绝对看不出来**的结论：
//
//   | 开关集                                  | 进程活着 | 渲染层真的渲染 |
//   |-----------------------------------------|----------|----------------|
//   | 不加开关                                 | 否（直接退） | 否 |
//   | disable-gpu / +compositing / +swiftshader | 否（直接退） | 否 |
//   | disable-software-rasterizer（原默认集）   | 否（直接退） | 否 |
//   | --disable-gpu-sandbox                    | 是        | **否（卡死）**  |
//   | --in-process-gpu                         | 是        | **否（卡死）**  |
//   | --no-sandbox                             | 是        | 是 |
//   | --disable-gpu-sandbox + 渲染层 sandbox:false | 是     | **是** |
//
// ⚠️ 注意第 4、5 行：进程活着、窗口也在（tasklist 有标题），
// 但渲染子进程**从未解析过 HTML**（`document.title` 为空、连 `1+1` 都算不出来）。
// 只统计进程数会得出"已修好"的错误结论 —— 这个坑我踩过一次，记在验收记录里。
//
// 根因：本机（以及目标机同类环境）**Chromium 的 Windows 沙箱无法初始化**，
// GPU 子进程与渲染子进程都起不来。所以：
//   · 关掉 GPU 子进程沙箱  -> 主进程不再自杀，但渲染层照样起不来；
//   · 再关掉**渲染层**沙箱  -> 页面才真正渲染。
//
// 为什么用 `webPreferences.sandbox=false` 而**不是** `--no-sandbox`：
//   `--no-sandbox` 是把**所有**子进程（含网络服务、utility）的沙箱全关掉；
//   `webPreferences.sandbox=false` 只关**本窗口渲染层**这一个。
//   本应用的网络栈只连 127.0.0.1 回环、页面只加载本地打包文件，
//   网络服务那层沙箱没有任何理由跟着一起关。**能窄就不宽。**
//
// 代价（必须在验收记录里写明，不能含糊）：
//   渲染层沙箱与 GPU 子进程沙箱在本版本默认关闭。
//   想恢复严格模式：设 `MMWF_STRICT_SANDBOX=1`（在能正常初始化沙箱的机器上用）。
// ---------------------------------------------------------------------------
const STRICT_SANDBOX = process.env.MMWF_STRICT_SANDBOX === '1';

if (process.env.MMWF_ENABLE_GPU !== '1') {
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-gpu');
  app.commandLine.appendSwitch('disable-gpu-compositing');
  if (!STRICT_SANDBOX) {
    // 让 GPU 子进程能起来（否则主进程直接 FATAL 退出）
    app.commandLine.appendSwitch('disable-gpu-sandbox');
  }
}

let currentOperator = '本机用户';

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 第二个实例被拒绝时，提示占用，并把已有窗口拉到前面。
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
      dialog.showMessageBox(mainWindow, {
        type: 'warning',
        title: '会议纪要工作台',
        message: `系统正由 ${currentOperator} 使用中`,
        detail: '同一台设备同一时间只允许一个会话。',
        buttons: ['知道了'],
      });
    }
  });
}

// ---------------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------------
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    title: '会议纪要工作台',
    backgroundColor: '#f5f6f8',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, // 默认安全值，别关
      nodeIntegration: false, // 渲染层不碰 Node
      // ★ 渲染层沙箱：默认关（见文件上方「T1 收尾实测更正」）。
      //   本机与目标机同类环境下，Chromium 的渲染子进程沙箱起不来，
      //   表现是**页面永不渲染**（窗口空白、title 为空），
      //   而不是报错 —— 极难排查，所以在这里写死原因。
      //   之所以不用 `--no-sandbox`：那个会连网络服务的沙箱一起关掉，
      //   而本应用只需要放开这一个窗口。
      //   想恢复严格模式：`MMWF_STRICT_SANDBOX=1`。
      sandbox: STRICT_SANDBOX,
      // ⚠️ 刻意**不设** webSecurity: false。
      // 本机回环请求通常不受 CORS 限制；真撞上报错再加，
      // 且必须在此处注明「仅本机回环专用」。安全默认值优先。
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ---------------------------------------------------------------------------
// IPC：把后端就绪信息交给渲染层
// ---------------------------------------------------------------------------
ipcMain.handle('mmwf:backend-info', async () => {
  if (!backendInfo) throw new Error('后端尚未就绪');
  return backendInfo;
});

//: 渲染层登录后告知操作人（写入审计由 Python 侧负责，这里只用于占用提示）。
ipcMain.handle('mmwf:set-operator', (_evt, name) => {
  if (typeof name === 'string' && name.trim()) currentOperator = name.trim();
  return true;
});

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------
app.whenReady().then(async () => {
  try {
    await startBackend();
  } catch (err) {
    // ★ 验收第 2 条：后端启动失败必须有**明确提示**，不能白屏。
    dialog.showMessageBoxSync({
      type: 'error',
      title: '会议纪要工作台',
      message: '无法启动后台服务',
      detail:
        `${err.message}\n\n` +
        '常见原因：\n' +
        '· 尚未安装运行环境（请先完成环境准备）\n' +
        '· 端口被占用或权限不足\n' +
        '· 数据目录被其他程序锁定',
      buttons: ['退出程序'],
    });
    app.quit();
    return;
  }

  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  stopBackend();
  app.quit();
});

app.on('before-quit', stopBackend);
