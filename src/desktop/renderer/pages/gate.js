/** 内容确认：四要素并排展示；逐条核对只属于本轮未提交草稿。 */
import * as api from '../api-client.js';
import { TEXT, G2_BUTTONS, G3_BUTTONS, FACTOR_LABELS, statusText } from '../strings.js';

export async function renderGate(view, meeting, opts, shell) {
  const { level } = opts;
  const S = shell;
  const pending = meeting.run?.pending_gate;
  const roundToken = api.gateRoundToken(pending);
  view.replaceChildren();
  const head = document.createElement('section');
  head.className = 'card';
  const heading = document.createElement('h2');
  heading.textContent = level === 'G2' ? '待确认议题' : '待审定纪要';
  head.appendChild(heading);
  const status = document.createElement('p');
  status.textContent = statusText(meeting.status);
  head.appendChild(status);
  const notice = document.createElement('p');
  notice.textContent = '未提交：逐条已核对和编辑尚未提交，刷新后需重新核对。';
  head.appendChild(notice);
  view.appendChild(head);
  let invalid = false;
  let submitting = false;
  let revision = 0;
  const draftReviewed = new Set();
  let draft = JSON.parse(JSON.stringify(pending?.payload || {}));
  const listWrap = document.createElement('section');
  listWrap.className = 'card';
  view.appendChild(listWrap);
  let items = [];
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.id = 'only-pending';
  cb.checked = true;

  function invalidate() {
    invalid = true;
    draft = {};
    draftReviewed.clear();
    diffWrap.replaceChildren();
    notice.textContent = '送审内容已更新，旧草稿已作废，请重新载入。';
    view.querySelectorAll('input, textarea, button').forEach(el => { el.disabled = true; });
    const reload = document.createElement('button');
    reload.textContent = '重新载入';
    reload.addEventListener('click', () => S.route(meeting.meeting_id));
    notice.appendChild(reload);
  }

  async function currentCheckpoint() {
    const current = await api.call('GET', `/meetings/${meeting.meeting_id}`);
    if (!view.contains(head)) return false;
    if (api.gateRoundToken(current.run?.pending_gate) !== roundToken || current.run?.pending_gate?.gate !== level) {
      invalidate();
      return false;
    }
    return true;
  }

  function editField(parent, label, value, save) {
    const lab = document.createElement('label');
    lab.textContent = label;
    const input = document.createElement('textarea');
    input.value = value ?? '';
    input.addEventListener('input', () => {
      save(input.value);
      revision += 1;
      diffWrap.replaceChildren();
      notice.textContent = '未提交：编辑和已核对标记尚未提交。';
    });
    lab.appendChild(input);
    parent.appendChild(lab);
  }

  function renderItems(onlyPending) {
    listWrap.replaceChildren();
    const shown = items.filter((t, index) => {
      const serverConfirmed = t.confirmed === true;
      const reviewed = draftReviewed.has(index);
      return !onlyPending || isPending(serverConfirmed, reviewed);
    });
    for (const t of shown) {
      const index = items.indexOf(t);
      const card = document.createElement('div');
      card.className = 'factor';
      const title = document.createElement('h3');
      title.textContent = draft.topics[index].title;
      card.appendChild(title);
      const four = document.createElement('div');
      four.className = 'four';
      const raw = factorItem(FACTOR_LABELS.raw, t.raw_text);
      four.appendChild(raw);
      four.appendChild(factorItem(FACTOR_LABELS.extracted, t.extracted));
      four.appendChild(factorItem(FACTOR_LABELS.risk, riskText(t.risk)));
      const basis = factorItem(FACTOR_LABELS.basis, t.basis.length ? '' : '暂无出处，请核对');
      for (const source of t.basis) {
        const jump = document.createElement('button');
        jump.className = 'btn';
        jump.textContent = `${source.segment_id} ${timecode(source.start_ms)}`;
        jump.addEventListener('click', () => {
          // 跳到该时间对应的原文；没有音频播放器时不伪称已开始播放。
          raw.querySelector('.value').textContent = `${timecode(source.start_ms)} ${source.text}`;
          raw.scrollIntoView({ block: 'center' });
        });
        basis.appendChild(jump);
      }
      four.appendChild(basis);
      card.appendChild(four);
      const confidence = document.createElement('p');
      confidence.className = t.low_confidence ? 'low-confidence' : '';
      confidence.textContent = `来源转写置信：${t.confidence == null ? '未提供' : t.confidence}${t.low_confidence ? '（低置信，请核对）' : ''}`;
      card.appendChild(confidence);
      for (const field of ['title', 'discussion', 'readings']) {
        editField(card, field === 'title' ? '议题' : field === 'discussion' ? '讨论内容' : '学习材料（每行一项）',
          field === 'readings' ? (draft.topics[index][field] || []).join('\n') : draft.topics[index][field],
          value => { draft.topics[index][field] = field === 'readings' ? (value ? value.split('\n') : []) : value; });
      }
      const reviewed = document.createElement('input');
      reviewed.type = 'checkbox';
      reviewed.checked = draftReviewed.has(index);
      reviewed.addEventListener('change', () => {
        if (reviewed.checked) draftReviewed.add(index); else draftReviewed.delete(index);
        renderItems(cb.checked);
      });
      const label = document.createElement('label');
      label.textContent = '已核对（未提交）';
      label.appendChild(reviewed);
      card.appendChild(label);
      listWrap.appendChild(card);
    }
    if (!shown.length) listWrap.textContent = '没有待确认的议题了。关闭过滤可继续修改。';
    // 提交完整列表并保留全部只读字段；事项不允许用户改归属。
    for (const name of ['resolutions', 'action_items']) {
      draft[name].forEach((item, index) => {
        const card = document.createElement('div');
        card.className = 'factor';
        for (const field of ['text', 'owner', 'deadline']) {
          editField(card, ({ text: '事项', owner: '责任人', deadline: '期限' })[field], item[field],
            value => { draft[name][index][field] = value; });
        }
        listWrap.appendChild(card);
      });
    }
  }

  const issueWrap = document.createElement('section');
  view.appendChild(issueWrap);
  const diffWrap = document.createElement('section');
  diffWrap.className = 'card gate-diff';
  if (level === 'G2') {
    const data = await api.call('GET', `/meetings/${meeting.meeting_id}/topics`);
    if (!(await currentCheckpoint())) return;
    items = data.topics || [];
    // 编辑基准取 checkpoint 原始载荷；展示投影绝不回传为抽取正文。
    draft = JSON.parse(JSON.stringify({ topics: pending.payload.topics || [],
      resolutions: pending.payload.resolutions || [], action_items: pending.payload.action_items || [] }));
    const label = document.createElement('label');
    label.htmlFor = 'only-pending';
    label.textContent = TEXT.ONLY_PENDING;
    head.appendChild(cb);
    head.appendChild(label);
    head.querySelector('#only-pending').addEventListener('change', e => renderItems(e.target.checked));
    renderItems(true);
  } else {
    const data = await api.call('GET', `/meetings/${meeting.meeting_id}/minutes`);
    if (!(await currentCheckpoint())) return;
    draft = { minutes: editableMinutes(data.minutes_structured || {}) };
    const previewText = document.createElement('pre');
    previewText.className = 'minutes-preview';
    previewText.textContent = data.minutes_markdown || '';
    listWrap.appendChild(previewText);
    function fields(parent, item, names) {
      for (const field of names) {
        editField(parent, field, Array.isArray(item[field]) ? item[field].join('\n') : item[field], value => {
          item[field] = ['attendees', 'readings'].includes(field) ? (value ? value.split('\n') : []) : value;
        });
      }
    }
    fields(listWrap, draft.minutes, ['title', 'meeting_time', 'location', 'host', 'attendees', 'recorder']);
    draft.minutes.topics.forEach((topic, index) => {
      fields(listWrap, topic, ['name', 'discussion', 'readings']);
      const original = data.minutes_structured?.topics?.[index] || {};
      const sources = document.createElement('pre');
      sources.textContent = sourceText(original);
      listWrap.appendChild(sources); // 来源只读，不进入编辑载荷。
      for (const kind of ['resolutions', 'action_items', 'pending_decisions']) {
        topic[kind].forEach((item, itemIndex) => {
          fields(listWrap, item, kind === 'pending_decisions' ? ['content', 'owner', 'deadline', 'decision_maker'] : ['content', 'owner', 'deadline']);
          const basis = document.createElement('pre');
          basis.textContent = sourceText(original[kind]?.[itemIndex]);
          listWrap.appendChild(basis);
        });
      }
    });
    const issues = await api.call('GET', `/meetings/${meeting.meeting_id}/issues`);
    if (!(await currentCheckpoint())) return;
    renderIssues(issueWrap, issues);
    const download = document.createElement('button');
    download.textContent = '下载问题清单';
    download.addEventListener('click', async () => {
      try { await api.downloadIssues(meeting.meeting_id); } catch (err) { S.showError(err); }
    });
    head.appendChild(download);
  }

  const actions = document.createElement('section');
  actions.className = 'card';
  const preview = document.createElement('button');
  preview.className = 'btn';
  preview.textContent = '查看差异：系统生成版 / 人工修改版';
  preview.addEventListener('click', async () => {
    const requestedRevision = revision;
    try {
      if (invalid || !(await currentCheckpoint())) return;
      const data = await api.call('POST', `/meetings/${meeting.meeting_id}/gate/${level}/diff`, api.gateRequest(roundToken, { payload: draft }));
      if (invalid || revision !== requestedRevision || !view.contains(head)) return;
      renderDiff(diffWrap, data.segments);
      if (level === 'G3') {
        listWrap.querySelector('.minutes-preview').textContent = data.minutes_markdown;
        renderIssues(issueWrap, { issues: data.issues, complete: true });
      }
    } catch (err) {
      await handleError(err);
    }
  });
  actions.appendChild(preview);
  const labels = level === 'G2' ? G2_BUTTONS : G3_BUTTONS;
  for (const decision of ['approve', 'reject', 'edit']) {
    const button = document.createElement('button');
    button.className = 'btn';
    button.textContent = labels[decision];
    button.addEventListener('click', async () => {
      if (invalid || submitting) return;
      submitting = true;
      actions.querySelectorAll('button').forEach(el => { el.disabled = true; });
      try {
        if (!(await currentCheckpoint())) return;
        await api.call('POST', `/meetings/${meeting.meeting_id}/gate/${level}/decide`, api.gateRequest(roundToken, {
          decision, operator: S.ctx.operator,
          ...(decision === 'edit' ? { payload: draft } : {}),
        }));
        await S.route(meeting.meeting_id);
      } catch (err) {
        await handleError(err);
      } finally {
        submitting = false;
        if (!invalid) actions.querySelectorAll('button').forEach(el => { el.disabled = false; });
      }
    });
    actions.appendChild(button);
  }
  view.appendChild(actions);
  view.appendChild(diffWrap);

  async function handleError(err) {
    if (err.code === 'E4001') await currentCheckpoint();
    S.showError(err);
  }
  // 轮询只检查令牌，不覆盖本轮编辑。离开页面后停止。
  async function watchCheckpoint() {
    if (!view.contains(head) || invalid) return;
    try { if (!(await currentCheckpoint())) return; } catch (err) { S.showError(err); }
    if (view.contains(head) && !invalid) setTimeout(watchCheckpoint, 3000);
  }
  await watchCheckpoint();
}

function isPending(serverConfirmed, draftReviewed) {
  return !(serverConfirmed || draftReviewed);
}

function factorItem(label, value) {
  const item = document.createElement('div');
  item.className = 'item';
  const lab = document.createElement('div');
  lab.className = 'label';
  lab.textContent = label;
  const text = document.createElement('div');
  text.className = 'value';
  text.textContent = value || '暂无出处，请核对';
  item.append(lab, text);
  return item;
}

function renderDiff(container, segments) {
  container.replaceChildren();
  for (const segment of segments) {
    const block = document.createElement('div');
    block.className = `diff-${segment.type}`;
    const pre = document.createElement('pre');
    pre.textContent = segment.type === 'unchanged' ? segment.lines.join('\n\n') :
      [segment.old ? `系统生成版：\n${segment.old.join('\n\n')}` : '',
        segment.new ? `人工修改版：\n${segment.new.join('\n\n')}` : ''].filter(Boolean).join('\n\n');
    if (segment.type === 'unchanged') {
      const toggle = document.createElement('button');
      toggle.textContent = `展开未变段落${segment.elided ? `（已省略 ${segment.elided} 段）` : ''}`;
      pre.hidden = true;
      toggle.addEventListener('click', () => { pre.hidden = !pre.hidden; });
      block.appendChild(toggle);
    }
    block.appendChild(pre);
    container.appendChild(block);
  }
}

function timecode(ms) {
  // T2-04 §2.3：缺真实时间时明确提示，不承诺音频跳转。
  if (!Number.isFinite(ms)) return '暂无时间信息';
  const seconds = Math.floor(ms / 1000);
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map(value => String(value).padStart(2, '0')).join(':');
}

function riskText(risk) {
  return ({ high: '高', medium: '中', low: '低' })[risk] || '未评级';
}

function editableMinutes(value) {
  const pick = (item, keys) => Object.fromEntries(keys.filter(k => k in item).map(k => [k, item[k]]));
  const out = pick(value, ['title', 'meeting_time', 'location', 'host', 'attendees', 'recorder']);
  out.topics = (value.topics || []).map(topic => {
    const next = pick(topic, ['name', 'discussion', 'readings']);
    for (const kind of ['resolutions', 'action_items', 'pending_decisions']) {
      next[kind] = (topic[kind] || []).map(item => pick(item, ['content', 'owner', 'deadline', 'decision_maker']));
    }
    return next;
  });
  return out;
}

function renderIssues(container, payload) {
  container.replaceChildren();
  if (!payload.complete) container.textContent = '历史检查未完成';
  for (const issue of payload.issues || []) {
    const row = document.createElement('p');
    row.className = `issue-${issue.level}`;
    row.textContent = `[${issue.level}] ${issue.code} ${issue.target}：${issue.message}`;
    if (issue.code.includes('FUZZY')) row.textContent += ' 原文有字词差异，待核对';
    container.appendChild(row);
  }
}

function sourceText(item = {}) {
  const refs = item.source_refs || [];
  return refs.length ? refs.map(ref => `${ref.segment_id} ${timecode(ref.start_ms)} ${ref.quote || ''}\n${ref.matched_text || ref.text || ''}${ref.matched === 'fuzzy' ? '（原文有字词差异，待核对）' : ''}`).join('\n') : (item.source_quote || '暂无出处，请核对');
}
