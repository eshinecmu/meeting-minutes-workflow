/**
 * mmwf 渲染层 · 应用外壳
 * ============================================================================
 *
 * 职责：初始化后端连接、管理"当前在第几步"、把当前会议交给对应页面渲染。
 *
 * ⚠️ 页面模块不自己发 fetch，统一走 `api-client` ——
 * 这样信封解包、令牌失效重试只有一份实现。
 */

import * as api from './api-client.js';
import { TEXT } from './strings.js';
import { renderSubmit } from './pages/submit.js';
import { renderReview } from './pages/review.js';
import { renderGate } from './pages/gate.js';
import { renderExport } from './pages/export.js';

const view = document.getElementById('view');
const stepsBar = document.getElementById('steps');
const operatorLabel = document.getElementById('operator-label');

/** 当前上下文：操作人 + 当前会议。 */
export const ctx = {
  operator: '',
  meetingId: null,
  status: null,
};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

/** 渲染一段提示条。kind: error | info | ok */
export function notice(kind, text) {
  const div = document.createElement('div');
  div.className = `notice ${kind}`;
  div.textContent = text;
  return div;
}

/** 清空并返回视图容器，供各页面填充。 */
export function clearView() {
  view.replaceChildren();
  return view;
}

/** 错误统一在这里提示 —— 业务错误码与中文提示都来自后端（N3.3）。 */
export function showError(err) {
  const el = notice('error', err && err.message ? err.message : '操作失败，请重试。');
  view.prepend(el);
}

/** 按会议状态高亮对应的步骤。 */
export function markStep(stepName) {
  stepsBar.hidden = false;
  for (const el of stepsBar.querySelectorAll('.step')) {
    el.classList.toggle('active', el.dataset.step === stepName);
  }
}

/**
 * 交给各页面的「外壳能力」。
 *
 * ⚠️ 为什么要打包成一个对象往下传，而不是让页面 `import app.js`：
 * `app.js` 要 `import` 各页面，页面若反过来 `import app.js` 就成了
 * **循环依赖** —— ES module 下循环依赖的求值顺序很容易踩坑
 * （拿到 undefined 的函数）。显式传参最直白，也最好测。
 */
export const shell = {
  ctx,
  route,
  notice,
  clearView,
  showError,
  markStep,
};

// ---------------------------------------------------------------------------
// 步骤路由：按会议状态决定进哪个页面
//
// ⚠️ 判据是**会议状态**而不是"使用者点了哪个按钮" ——
// 这样刷新/重启后能回到正确的页面，而不是重新走一遍。
// ---------------------------------------------------------------------------
export async function route(meetingId) {
  ctx.meetingId = meetingId;
  const meeting = await api.call('GET', `/meetings/${meetingId}`);
  ctx.status = meeting.status;

  clearView();

  switch (meeting.status) {
    case 'G2_awaiting':
      markStep('gate');
      await renderGate(view, meeting, { level: 'G2' }, shell);
      break;
    case 'G3_awaiting':
      markStep('gate');
      await renderGate(view, meeting, { level: 'G3' }, shell);
      break;
    case 'archived':
      markStep('export');
      renderExport(view, meeting, shell);
      break;
    default:
      // 其余状态（转写中 / 起草中 …）走"文稿核对"页，
      // 它同时负责展示进度（N3.2：长任务必须有细粒度进度）。
      markStep('submit');
      await renderReview(view, meeting, shell);
      break;
  }
}

// ---------------------------------------------------------------------------
// 首页：操作人 + 文字稿投递
// ---------------------------------------------------------------------------
async function boot() {
  const input = document.getElementById('operator');
  const enter = document.getElementById('enter');
  const hint = document.getElementById('boot-hint');

  if (hint) hint.textContent = '首次使用请先填写操作人，用于记录操作留痕。';

  enter.addEventListener('click', async () => {
    const name = (input.value || '').trim();
    if (!name) {
      clearView().appendChild(notice('error', '请填写操作人姓名。'));
      return;
    }
    ctx.operator = name;
    operatorLabel.textContent = `操作人：${name}`;
    await window.mmwf.setOperator(name); // 供 F8.3 占用提示使用
    await renderSubmit(clearView(), shell);
  });
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------
async function main() {
  try {
    await api.init();
  } catch (err) {
    // 后端没就绪 —— 主进程已在启动阶段做过兜底提示，这里再兜一层。
    clearView().appendChild(
      notice('error', '无法连接到后台服务，请重启程序。')
    );
    return;
  }
  await boot();
}

main();

//: 供页面模块引用（避免循环 import 时拿不到）
export { api, TEXT };
