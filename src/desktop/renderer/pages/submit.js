/**
 * 页面 · 投递（第 ① 步）
 * ============================================================================
 * 把会议录音或文字稿放进来，建一条会议记录。
 *
 * ⚠️ 一期界面只接**已实通**的端点（T1-11 的 11 个）。
 * 音频上传端点（`/audio`）尚未实现，所以这里如实说明，
 * 不做一个点了会报错的按钮 —— 那比没有按钮更糟。
 */

import * as api from '../api-client.js';

export async function renderSubmit(view, S) {
  view.replaceChildren();

  const card = document.createElement('section');
  card.className = 'card';
  card.innerHTML = `
    <h2>投递会议内容</h2>
    <div class="field">
      <label for="m-title">会议名称</label>
      <input type="text" id="m-title" placeholder="例如：季度工作会" />
    </div>
    <div class="field">
      <label for="m-host">主持人</label>
      <input type="text" id="m-host" placeholder="例如：张三" />
    </div>
    <div class="field">
      <label for="m-attendees">参会人（用顿号或逗号分隔）</label>
      <input type="text" id="m-attendees" placeholder="例如：张三、李四" />
    </div>
    <div class="field">
      <label for="m-record">会议记录稿</label>
      <textarea id="m-record"
        placeholder="把会议记录粘贴到这里，每句尽量带时间码，例如：[00:00:12] 张三：今天研究季度工作安排。"></textarea>
    </div>
    <div class="row">
      <button class="btn btn-primary" id="m-create">建立会议</button>
    </div>
  `;
  view.appendChild(card);

  view.appendChild(
    S.notice(
      'info',
      '当前版本支持文字记录稿。录音文件的导入功能将在后续版本提供。'
    )
  );

  card.querySelector('#m-create').addEventListener('click', async () => {
    const title = card.querySelector('#m-title').value.trim();
    const host = card.querySelector('#m-host').value.trim();
    const attendeesRaw = card.querySelector('#m-attendees').value.trim();
    const record = card.querySelector('#m-record').value.trim();

    if (!title || !record) {
      S.showError({ message: '请填写会议名称与会议记录稿。' });
      return;
    }

    const attendees = attendeesRaw
      ? attendeesRaw.split(/[、,，\s]+/).filter(Boolean)
      : [];

    try {
      const data = await api.call('POST', '/meetings', {
        title,
        record_markdown: record,
        host: host || undefined,
        attendees,
        recorder: S.ctx.operator, // 记录人 = 当前操作人
      });
      const meetingId = data.meeting_id;

      // 建完立刻启动流水线，让它跑到第一个待确认点停下。
      await api.call('POST', `/meetings/${meetingId}/start`, {});
      await S.route(meetingId);
    } catch (err) {
      S.showError(err);
    }
  });
}
