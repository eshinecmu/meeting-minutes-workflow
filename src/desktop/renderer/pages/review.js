/**
 * 页面 · 文稿核对（第 ② 步的前置）
 * ============================================================================
 * 展示转写/整理后的**文稿**，以及长任务的细粒度进度（N3.2）。
 *
 * ⚠️ 术语：中间产物在界面上统一叫「**文稿**」，不叫"中间产物""产物"（N3.3）。
 */

import * as api from '../api-client.js';
import { TEXT, statusText, progressText } from '../strings.js';

export async function renderReview(view, meeting, shell) {
  const S = shell;
  view.replaceChildren();

  const card = document.createElement('section');
  card.className = 'card';

  const head = document.createElement('h2');
  head.textContent = TEXT.DRAFT; // 「文稿」
  card.appendChild(head);

  // -- 状态 + 进度 --------------------------------------------------------
  const statusRow = document.createElement('div');
  statusRow.className = 'row';
  const badge = document.createElement('span');
  badge.className = 'badge pending';
  badge.textContent = statusText(meeting.status); // 内部状态 → 界面文案
  statusRow.appendChild(badge);

  // 进度（有 progress 字段才显示）
  const p = meeting.progress;
  if (p && Number.isFinite(p.current_chunk) && Number.isFinite(p.total_chunks)) {
    const prog = document.createElement('span');
    prog.className = 'operator';
    prog.textContent = progressText(
      p.current_chunk,
      p.total_chunks,
      Math.max(0, Math.round((p.eta_seconds || 0) / 60))
    );
    statusRow.appendChild(prog);
  }
  card.appendChild(statusRow);

  // -- 文稿正文 ----------------------------------------------------------
  const body = document.createElement('div');
  body.className = 'field';
  body.style.marginTop = 'var(--sp-4)';

  let md = '';
  try {
    const rec = await api.call('GET', `/meetings/${meeting.meeting_id}/record`);
    md = rec.markdown || '';
  } catch {
    md = '';
  }

  if (md) {
    const pre = document.createElement('pre');
    pre.style.whiteSpace = 'pre-wrap';
    pre.style.fontFamily = 'var(--font)';
    pre.textContent = md;
    body.appendChild(pre);
  } else {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = '正在整理文稿，请稍候……';
    body.appendChild(empty);
  }
  card.appendChild(body);

  // -- 操作 --------------------------------------------------------------
  const row = document.createElement('div');
  row.className = 'row';
  const refresh = document.createElement('button');
  refresh.className = 'btn';
  refresh.textContent = '刷新';
  refresh.addEventListener('click', () => S.route(meeting.meeting_id));
  row.appendChild(refresh);
  card.appendChild(row);

  view.appendChild(card);
}
