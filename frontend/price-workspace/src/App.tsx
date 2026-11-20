import { useRef, useState } from 'preact/hooks';
import { errorText } from './api';
import type { BulkPlan, WorkspaceApi } from './api';
import { Modal } from './Modal';
import { parseAsinDraft, parsePriceDraft } from './model';
import { Results } from './Results';
import { useWorkspace } from './useWorkspace';

type Editor = 'create' | 'rename' | 'delete' | 'asins' | 'price' | 'bulk';
const editorTitles: Record<Editor, string> = {
  create: '新建竞品组', rename: '重命名竞品组', delete: '删除竞品组',
  asins: '编辑 ASIN 名单', price: '编辑本品价', bulk: '巡查全部分组',
};

export function App({ api, demo }: { api: WorkspaceApi; demo: boolean }) {
  const { groups, selectedId, snapshot, loading, readError, load,
    notice, setNotice, jobId, progress, pollError, followJob } = useWorkspace(api);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState('');
  const [editorError, setEditorError] = useState('');
  const [plan, setPlan] = useState<BulkPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const operating = useRef(false);
  const writable = snapshot !== null && !readError && !loading && !busy && !jobId;
  const nonempty = groups.filter(group => group.count > 0).length;

  async function perform(work: () => Promise<void>, onError = setEditorError) {
    if (operating.current) return;
    operating.current = true;
    setBusy(true);
    setEditorError('');
    try { await work(); }
    catch (error) { onError(errorText(error)); }
    finally { operating.current = false; setBusy(false); }
  }

  function open(kind: Editor, value = '') {
    setDraft(value);
    setEditorError('');
    setPlan(null);
    setEditor(kind);
  }

  function submitEditor() {
    if (!editor || !snapshot || !writable) return;
    const id = snapshot.group_id;
    void perform(async () => {
      let selected = id;
      let message = '已保存。';
      switch (editor) {
        case 'create':
        case 'rename': {
          const name = draft.trim();
          if (!name || name.length > 60) throw new Error('请输入 1–60 个字符的分组名称。');
          const result = await api.saveGroup(name, editor === 'rename' ? id : undefined);
          selected = result.group.id;
          message = editor === 'create' ? `已新建「${result.group.name}」。` : `已重命名为「${result.group.name}」。`;
          break;
        }
        case 'delete':
          selected = (await api.deleteGroup(id)).group_id;
          message = `已删除「${snapshot.group_name}」。`;
          break;
        case 'asins': {
          const asins = parseAsinDraft(draft, snapshot.max_asins);
          await api.saveAsins(id, asins);
          message = `名单已保存，共 ${asins.length} 个 ASIN。`;
          break;
        }
        case 'price': {
          const price = parsePriceDraft(draft);
          await api.savePrice(id, price);
          message = price === null ? '已清除本品价。' : '本品价已保存；已重新对比今日结果。';
          break;
        }
        case 'bulk': return;
      }
      setEditor(null);
      setNotice({ text: message });
      await load(selected);
    });
  }

  function runGroup() {
    if (!snapshot || !writable || !snapshot.asins.length) return;
    void perform(async () => {
      const result = await api.start(snapshot.group_id);
      followJob(result.job_id);
    }, text => setNotice({ text: `未能启动巡查：${text}`, warning: true }));
  }

  function previewBulk() {
    if (!writable) return;
    setPlan(null);
    void perform(async () => setPlan(await api.bulkPlan()));
  }

  function runBulk() {
    if (!plan?.can_run || !writable) return;
    void perform(async () => {
      const result = await api.startAll(plan.plan_hash);
      setEditor(null);
      followJob(result.job_id);
    }, text => {
      setPlan(null);
      setEditorError(`未能启动巡查：${text} 请重新预览后确认。`);
    });
  }

  const dialog = editor && <Modal key={editor} title={editorTitles[editor]} busy={busy} onClose={() => setEditor(null)}>
    {editor === 'bulk' ? <>
      {plan ? <>
        <p>依序巡查 <strong>{plan.group_count}</strong> 个有名单的分组。{plan.membership_count} 个名单条目合并为 <strong>{plan.unique_asin_count}</strong> 个唯一 ASIN。</p>
        <ul class="plan-list">{plan.groups.map(group => <li key={group.id}><span>{group.name}</span><span class="numeric">{group.count} 项</span></li>)}</ul>
        <p class="muted">跳过 {plan.empty_group_count} 个空组和 {plan.duplicates_skipped} 个重复条目。单次上限 {plan.limit} 个唯一 ASIN。</p>
        {!plan.can_run && <p class="form-error">{plan.unique_asin_count > plan.limit ? '超出上限。请减少名单，或改为巡查单组。' : '没有已保存的 ASIN 可以巡查。'}</p>}
        <p class="callout">{demo ? '这是模拟巡查，不会访问 Amazon。' : '确认后会打开匿名 Amazon US 浏览器（配送邮编 10001）。遇到验证码或访问限制会停止。'}</p>
      </> : <p>{busy ? '正在读取已保存的分组和 ASIN…' : '重新读取计划后，再确认要巡查的范围。'}</p>}
      {editorError && <p class="form-error" role="alert">{editorError}</p>}
      <div class="modal-actions">
        <button type="button" class="secondary" disabled={busy} onClick={() => setEditor(null)}>取消</button>
        {!plan ? <button type="button" class="primary" disabled={!writable} onClick={previewBulk}>重新预览</button>
          : <button type="button" class="primary" disabled={!plan.can_run || !writable} onClick={runBulk}>确认巡查</button>}
      </div>
    </> : <form onSubmit={event => { event.preventDefault(); submitEditor(); }}>
      {editor === 'delete' ? <p>删除「{snapshot?.group_name}」及其 ASIN 成员关系？商品已有的巡查历史不会删除。</p>
        : editor === 'asins' ? <>
          <label class="field-label" htmlFor="edit-asins">ASIN 名单</label>
          <textarea id="edit-asins" rows={9} spellcheck={false} value={draft} aria-describedby="field-help" disabled={busy}
            onInput={event => setDraft(event.currentTarget.value)} />
          <p id="field-help" class="field-help">每行一个 10 位 ASIN，每组最多 {snapshot?.max_asins} 个。保存将替换当前名单；留空可清空名单。</p>
        </> : editor === 'price' ? <>
          <label class="field-label" htmlFor="edit-price">本品售价（USD）</label>
          <input id="edit-price" type="text" inputMode="decimal" maxLength={30} value={draft} aria-describedby="field-help"
            disabled={busy} onInput={event => setDraft(event.currentTarget.value)} />
          <p id="field-help" class="field-help">输入 USD 购买价，最多两位小数。留空可清除；这里只修改本组的对比基准。</p>
        </> : <>
          <label class="field-label" htmlFor="edit-group-name">分组名称</label>
          <input id="edit-group-name" type="text" required maxLength={60} value={draft} disabled={busy}
            onInput={event => setDraft(event.currentTarget.value)} />
        </>}
      {editorError && <p class="form-error" role="alert">{editorError}</p>}
      <div class="modal-actions">
        <button type="button" class="secondary" disabled={busy} onClick={() => setEditor(null)}>取消</button>
        <button type="submit" class={editor === 'delete' ? 'danger' : 'primary'} disabled={!writable}>
          {busy ? '正在保存…' : { asins: '保存名单', price: '保存本品价', delete: '删除分组', create: '新建分组', rename: '保存分组名称' }[editor]}
        </button>
      </div>
    </form>}
  </Modal>;

  return <>
    <a class="skip-link" href="#workspace">跳到价格巡查</a>
    <header class="masthead">
      <div class="masthead-inner">
        <div class="wordmark"><span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none"><path d="M5 5v14h14M8 14l4-4 3 2 4-6M16 6h3v3"/></svg>
        </span>竞品工作台</div>
        <div class="masthead-tail">
          {demo ? <span class="mode-tag demo-tag">离线演示</span> : <><span class="mode-tag">本地工作台</span><a href="/#amazon">返回旧版 ↗</a></>}
        </div>
      </div>
    </header>
    <main id="workspace" class="workspace" tabIndex={-1}>
      <header class="page-head">
        <div><h1>价格巡查</h1><p>Amazon US<span aria-hidden="true"> / </span>美元报价</p></div>
        <div class="page-actions">
          <button type="button" class="text-button" disabled={busy || loading} onClick={() => void load()}>重新读取</button>
          <button type="button" class="secondary" disabled={!writable || !nonempty}
            onClick={() => { open('bulk'); previewBulk(); }}>巡查全部分组</button>
          <button type="button" class="primary" disabled={!writable || !snapshot?.asins.length} onClick={runGroup}>巡查本组</button>
        </div>
      </header>
      {demo && <div class="demo-note"><strong>示例数据</strong><span>可试用分组、编辑和模拟巡查。不会保存到本地数据库，刷新后恢复初始示例。</span></div>}
      {readError && <div class="banner warning" role="alert"><strong>读取失败</strong><span>{readError}</span></div>}
      {jobId ? <div class="banner job-banner" role="status">
        <strong>{progress?.group_name ? `${progress.group_name} · 巡查中` : '巡查进行中'}</strong>
        {progress && progress.total > 0 && <div class="job-meter">
          <progress aria-label="巡查进度" max={progress.total} value={progress.done} />
          <span class="numeric">{progress.done}/{progress.total}</span>
        </div>}
        <span>{pollError || progress?.detail || '正在读取进度…'}</span>
      </div> : notice && <div class={`banner${notice.warning ? ' warning' : ''}`} role={notice.warning ? 'alert' : 'status'}>{notice.text}</div>}
      <div class="workspace-grid">
        <aside class="sidebar" aria-label="分组和对比设置">
          <section class="groups" aria-labelledby="groups-title">
            <div class="section-head"><h2 id="groups-title">竞品分组</h2>
              <button type="button" class="text-button" disabled={!writable} onClick={() => open('create')}>＋ 新建</button>
            </div>
            <ul class="group-list">{groups.map(group => <li key={group.id}>
              <button type="button" aria-pressed={selectedId === group.id} class="group-button" disabled={busy}
                onClick={() => { if (group.id !== selectedId) void load(group.id, false); }}>
                <span class="group-name">{group.name}</span><span class="group-count numeric" aria-label={`${group.count} 项`}>{group.count}</span>
              </button>
            </li>)}</ul>
            {!groups.length && <p class="muted">{loading ? '正在读取分组…' : '分组暂不可用'}</p>}
          </section>
          <section class="group-settings" aria-labelledby="settings-title">
            <div class="section-head"><h2 id="settings-title">本品售价</h2><button type="button" class="text-button" disabled={!writable}
              onClick={() => open('price', snapshot?.reference_price)}>编辑本品价</button></div>
            <p class="own-price">{snapshot?.reference_price ? <><span class="numeric">${snapshot.reference_price}</span><span class="currency">USD</span></>
              : <span class="unconfigured">{snapshot ? '未设置' : '—'}</span>}</p>
            <p class="field-help">只用于本组的价格对比。</p>
            <button type="button" class="secondary edit-list" disabled={!writable}
              onClick={() => open('asins', snapshot?.asins.join('\n'))}>编辑 ASIN 名单</button>
            {snapshot && <p class="list-count numeric">{snapshot.asins.length} / {snapshot.max_asins} 个 ASIN</p>}
            <details class="group-options"><summary>管理当前组</summary>
              <div><button type="button" class="text-button" disabled={!writable} onClick={() => open('rename', snapshot?.group_name)}>重命名</button>
                <button type="button" class="text-button text-danger" disabled={!writable || groups.length <= 1} onClick={() => open('delete')}>删除分组</button></div>
            </details>
          </section>
          <p class="manual-note">仅在手动启动后巡查。{!demo && '运行时会访问 Amazon US。'}</p>
        </aside>
        <Results key={selectedId} snapshot={snapshot} loading={loading} error={readError} demo={demo} />
      </div>
    </main>
    {dialog}
  </>;
}
