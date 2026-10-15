/**
 * mmwf 渲染层 · API 客户端
 * ============================================================================
 *
 * 唯一职责：把「统一信封」翻译成渲染层好用的形态。
 *
 * ⚠️⚠️ 后端**永远返回 HTTP 200**（SAD §6.3）：
 * 业务成败只体现在信封体的 `ok` / `code` 上，HTTP 状态码只用来表达
 * "服务不可达"这类传输层故障。
 *
 * 所以**绝对不能**写 `if (!res.ok) throw ...`：
 * HTTP 200 时 `res.ok` 恒为 true → 那句判断永远不成立 →
 * 所有业务错误（E6001/E6002/E9001…）会被**静默吞掉**，
 * 界面停在原地、什么也不说。这正是统一信封要消灭的东西，
 * 却很容易在客户端把它重新引入。
 */

const API_PREFIX = '/api/v1';

let BASE = null;
let TOKEN = null;

/** 业务错误。`code` 是后端的错误码，`message` 是面向使用者的中文提示（N3.3）。 */
export class AppError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'AppError';
    this.code = code;
  }
}

/** 会话失效。前端遇到它要**重读令牌重试**（见 `call` 的重试逻辑）。 */
export const E_SESSION_EXPIRED = 'E6001';

/**
 * 初始化：从主进程取端口与令牌。
 * ⚠️ 必须在发第一个请求**之前**调用 —— 否则 BASE 是 null。
 */
export async function init() {
  const info = await window.mmwf.getBackendInfo();
  BASE = `http://127.0.0.1:${info.port}${API_PREFIX}`;
  TOKEN = info.token;
}

/**
 * 发一次请求，返回信封里的 `data`。
 *
 * @param {string} method  GET / POST / PATCH ...
 * @param {string} path    形如 `/meetings`（不含 `/api/v1` 前缀）
 * @param {object} [body]
 * @returns {Promise<any>} 业务数据（`env.data`）
 * @throws {AppError} 业务失败时抛，带 `code` 与中文 `message`
 */
export async function call(method, path, body) {
  return requestOnce(method, path, body, true);
}

async function requestOnce(method, path, body, allowRetry) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-MMWF-Token': TOKEN,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    // 传输层故障（后端没起、端口错了）—— 这类**确实是**用 HTTP 层表达的。
    throw new AppError('E_NETWORK', '无法连接到后台服务，请重启程序。');
  }

  let env;
  try {
    env = await res.json();
  } catch {
    throw new AppError('E_BAD_RESPONSE', '后台返回了无法识别的内容。');
  }

  // ★ 判 `env.ok`，不是 `res.ok`。
  if (!env.ok) {
    // 令牌失效：重读一次令牌再试（可能是后端重启换了令牌）。
    if (env.code === E_SESSION_EXPIRED && allowRetry) {
      await refreshToken();
      return requestOnce(method, path, body, false);
    }
    throw new AppError(env.code, env.message);
  }
  return env.data;
}

/**
 * 重读令牌（后端重启后会换新令牌，旧令牌立刻失效 —— T1-11 已定契约）。
 * 只重读、不重启后端：主进程持有的仍是同一个进程的端口与令牌。
 */
async function refreshToken() {
  const info = await window.mmwf.getBackendInfo();
  TOKEN = info.token;
}
