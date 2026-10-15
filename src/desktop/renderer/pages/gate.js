/**
 * 页面 · 内容确认（第 ② 步）
 * ============================================================================
 * G2（待确认议题）与 G3（待审定纪要）共用此页。
 *
 * ⚠️⚠️ 本页承载两条强制要求：
 *
 *   F4.2 —— **G2 每条待确认项必须展示四要素，并排可见**：
 *           原始内容 / 系统抽取结果 / 风险评级 / 抽取依据。
 *           这是使用者判断"该不该点确认"的**唯一依据**，
 *           所以**不许**藏进折叠面板或次要页面。
 *
 *   F4.4 —— **「只显示待确认项」过滤可用**。默认只显示未确认项，
 *           关掉开关才显示全部。
 *
 * ⚠️ 术语（N3.3）：内部叫"闸口/HITL"，界面叫「**待确认事项**」。
 *    按钮文案必须严格照用：G2 = 确认无误,继续 / 退回重做 / 改完继续；
 *    G3 = 审定通过 / 退回修改 / 改完继续。
 */

import * as api from '../api-client.js';
import { TEXT, G2_BUTTONS, G3_BUTTONS, FACTOR_LABELS, statusText } from '../strings.js';

export async function renderGate(view, meeting, opts, shell) {
  const S = shell;
  const { level } = opts; // 'G2' | 'G3'
  view.replaceChildren();

  // ── 页头 ────────────────────────────────────────────────────────────────
  const head = document.createElement('section');
  head.className = 'card';
  const h2 = document.createElement('h2');
  h2.textContent = level === 'G2' ? '待确认议题' : '待审定纪要';
  head.appendChild(h2);

  const statusRow = document.createElement('div');
  statusRow.className = 'row';
  const badge = document.createElement('span');
  badge.className = 'badge pending';
  badge.textContent = statusText(meeting.status);
  statusRow.appendChild(badge);
  head.appendChild(statusRow);

  // ── G2：待确认议题 + 四要素 ────────────────────────────────────────────
  let items = [];
  if (level === 'G2') {
    const data = await api.call('GET', `/meetings/${meeting.meeting_id}/topics`);
    // 后端的议题结构里，`confirmed` 标记是否已确认（用于 F4.4 过滤）。
    items = Array.isArray(data) ? data : (data.topics || []);

    // F4.4：过滤开关
    const filterRow = document.createElement('div');
    filterRow.className = 'row';
    filterRow.style.marginTop = 'var(--sp-4)';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'only-pending';
    cb.checked = true;
    const lab = document.createElement('label');
    lab.htmlFor = 'only-pending';
    lab.textContent = TEXT.ONLY_PENDING; // 「只显示待确认项」
    filterRow.appendChild(cb);
    filterRow.appendChild(lab);
    head.appendChild(filterRow);

    head.querySelector('#only-pending').addEventListener('change', (e) => {
      renderItems(e.target.checked);
    });
  }

  view.appendChild(head);

  // ── 列表容器 ───────────────────────────────────────────────────────────
  const listWrap = document.createElement('section');
  listWrap.className = 'card';
  view.appendChild(listWrap);

  function renderItems(onlyPending) {
    listWrap.replaceChildren();

    const shown = onlyPending ? items.filter((t) => !t.confirmed) : items;

    if (shown.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = onlyPending
        ? '没有待确认的议题了。'
        : '暂无可显示的议题。';
      listWrap.appendChild(empty);
      return;
    }

    const list = document.createElement('div');
    list.className = 'factor-list';

    for (const t of shown) {
      const card = document.createElement('div');
      card.className = 'factor';

      // 标题行
      const hd = document.createElement('div');
      hd.className = 'factor-head';
      const title = document.createElement('span');
      title.className = 'factor-title';
      title.textContent = t.title || '（未命名议题）';
      hd.appendChild(title);

      const risk = document.createElement('span');
      risk.className = `badge ${t.risk === 'high' ? 'fail' : t.risk === 'medium' ? 'pending' : ''}`;
      risk.textContent = `${FACTOR_LABELS.risk}：${riskText(t.risk)}`;
      hd.appendChild(risk);
      card.appendChild(hd);

      // ★ 四要素：并排可见（F4.2）
      const four = document.createElement('div');
      four.className = 'four';

      four.appendChild(
        factorItem(FACTOR_LABELS.raw, t.raw_text || t.source_text || '（无）')
      );
      four.appendChild(
        factorItem(FACTOR_LABELS.extracted, t.extracted || t.title || '（无）')
      );
      four.appendChild(factorItem(FACTOR_LABELS.risk, riskText(t.risk)));
      four.appendChild(
        factorItem(FACTOR_LABELS.basis, t.basis || t.rationale || '（无）')
      );

      card.appendChild(four);
      list.appendChild(card);
    }

    listWrap.appendChild(list);
  }

  function factorItem(label, value) {
    const d = document.createElement('div');
    d.className = 'item';
    const l = document.createElement('div');
    l.className = 'label';
    l.textContent = label;
    const v = document.createElement('div');
    v.className = 'value';
    v.textContent = String(value);
    d.appendChild(l);
    d.appendChild(v);
    return d;
  }

  if (level === 'G2') {
    renderItems(true); // 默认只显示待确认项
  } else {
    // G3：展示纪要草稿（审定对象）
    const list = document.createElement('div');
    list.className = 'factor-list';
    const card = document.createElement('div');
    card.className = 'factor';

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
    pre.textContent = mm || '正在起草纪要，请稍候……';
    card.appendChild(pre);
    list.appendChild(card);
    listWrap.appendChild(list);
  }

  // ── 决策按钮 ───────────────────────────────────────────────────────────
  const actions = document.createElement('section');
  actions.className = 'card';
  const row = document.createElement('div');
  row.className = 'row';

  const labels = level === 'G2' ? G2_BUTTONS : G3_BUTTONS;
  const decisions = [
    { key: 'approve', cls: 'btn-primary' },
    { key: 'reject', cls: 'btn btn-danger' },
    { key: 'edit', cls: 'btn' },
  ];

  for (const d of decisions) {
    const b = document.createElement('button');
    b.className = `btn ${d.cls}`;
    b.textContent = labels[d.key];
    b.addEventListener('click', async () => {
      try {
        const res = await api.call(
          'POST',
          `/meetings/${meeting.meeting_id}/gate/${level}/decide`,
          { decision: d.key, operator: S.ctx.operator }
        );
        const next = res.next_state;
        if (next === 'archived' || next === 'G3_awaiting') {
          await S.route(meeting.meeting_id);
        } else {
          await S.route(meeting.meeting_id);
        }
      } catch (err) {
        S.showError(err);
      }
    });
    row.appendChild(b);
  }

  actions.appendChild(row);
  view.appendChild(actions);
}

function riskText(risk) {
  switch (risk) {
    case 'high':
      return '高';
    case 'medium':
      return '中';
    case 'low':
      return '低';
    default:
      return '未评级';
  }
}
