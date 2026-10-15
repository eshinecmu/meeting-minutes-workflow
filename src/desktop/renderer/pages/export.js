/**
 * 页面 · 导出（第 ③ 步）
 * ============================================================================
 * 归档后把纪要导出为符合 GB/T 9704—2012 的 .docx。
 *
 * ⚠️ 导出是**幂等**的：重复调用返回同一个路径。
 * 所以界面上要处理好"已经导出过了"的情况，别让人以为没成功。
 */

import * as api from '../api-client.js';
import { statusText, TEXT } from '../strings.js';

export async function renderExport(view, meeting, shell) {
  const S = shell;
  view.replaceChildren();

  const card = document.createElement('section');
  card.className = 'card';

  const h2 = document.createElement('h2');
  h2.textContent = '导出会议纪要';
  card.appendChild(h2);

  const statusRow = document.createElement('div');
  statusRow.className = 'row';
  const badge = document.createElement('span');
  badge.className = 'badge done';
  badge.textContent = statusText(meeting.status);
  statusRow.appendChild(badge);
  card.appendChild(statusRow);

  // 纪要正文（给人最后看一眼再导出）
  const body = document.createElement('div');
  body.className = 'field';
  body.style.marginTop = 'var(--sp-4)';

  let mm = '';
  try {
    const data = await api.call('GET', `/meetings/${meeting.meeting_id}/minutes`);
    mm = data.minutes_markdown || '';
  } catch {
    mm = '';
  }
  const pre = document.createElement('pre');
  pre.style.whiteSpace = 'pre-wrap';
  pre.style.fontFamily = 'var(--font)';
  pre.textContent = mm || '暂无纪要内容。';
  body.appendChild(pre);
  card.appendChild(body);

  // 动作
  const row = document.createElement('div');
  row.className = 'row';

  const exportBtn = document.createElement('button');
  exportBtn.className = 'btn btn-primary';
  exportBtn.textContent = '导出为文档';
  row.appendChild(exportBtn);

  const backBtn = document.createElement('button');
  backBtn.className = 'btn';
  backBtn.textContent = '返回查看';
  backBtn.addEventListener('click', () => S.route(meeting.meeting_id));
  row.appendChild(backBtn);

  card.appendChild(row);

  // 结果提示区
  const result = document.createElement('div');
  result.style.marginTop = 'var(--sp-4)';
  card.appendChild(result);

  exportBtn.addEventListener('click', async () => {
    exportBtn.disabled = true;
    result.replaceChildren();
    try {
      const data = await api.call(
        'POST',
        `/meetings/${meeting.meeting_id}/export`,
        {}
      );
      const path = data.path;
      result.appendChild(
        S.notice(
          'ok',
          data.reused
            ? `文档已生成：${path}`
            : `文档已生成：${path}`
        )
      );

      // 撤销入口（导出是幂等的，误导出可以撤）
      const undo = document.createElement('button');
      undo.className = 'btn';
      undo.textContent = '撤销导出';
      undo.style.marginTop = 'var(--sp-2)';
      undo.addEventListener('click', async () => {
        try {
          await api.call(
            'POST',
            `/meetings/${meeting.meeting_id}/export/undo`,
            { rel_path: path }
          );
          result.replaceChildren(S.notice('info', '已撤销导出。'));
        } catch (err) {
          S.showError(err);
        }
      });
      result.appendChild(undo);
    } catch (err) {
      S.showError(err);
    } finally {
      exportBtn.disabled = false;
    }
  });

  view.appendChild(card);

  // 术语提示：导出文件按密级管理（F8.5 的界面侧提醒，不做定密决策）
  view.appendChild(
    S.notice('info', `导出文件请按会议密级管理，确认流转范围。${TEXT.FORMAT_CHECK}已完成。`)
  );
}
