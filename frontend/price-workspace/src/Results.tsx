import { useMemo, useState } from 'preact/hooks';
import type { Snapshot } from './api';
import { displayRows, sortableColumns, usdPrice } from './model';
import type { Filter, Sort, SortKey } from './model';

export function Results({ snapshot, loading, error, demo }: {
  snapshot: Snapshot | null; loading: boolean; error: string; demo: boolean;
}) {
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [sort, setSort] = useState<Sort>({ key: 'price', direction: 'asc' });
  const items = snapshot?.items;
  const rows = useMemo(() => displayRows(items ?? [], filter, search, sort), [items, filter, search, sort]);
  const filters: { key: Filter; label: string; count: number }[] = [
    { key: 'all', label: '全部', count: items?.length ?? 0 },
    { key: 'below', label: '低于本品', count: items?.filter(row => row.below_own_price).length ?? 0 },
    { key: 'unpriced', label: '无 USD 价', count: items?.filter(row => usdPrice(row) === null).length ?? 0 },
  ];
  function sortBy(key: SortKey) {
    setSort(previous => ({ key, direction: previous.key === key && previous.direction === 'asc' ? 'desc' : 'asc' }));
  }
  return <section class="results" aria-labelledby="results-title" aria-busy={loading}>
    <header class="results-head">
      <div>
        <h2 id="results-title">今日巡查结果</h2>
        <p class="results-date">{snapshot ? <>北京时间 · <time dateTime={snapshot.today}>{snapshot.today}</time></> : '仅展示北京时间当天的结果'}</p>
      </div>
      <label class="search">
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="8.5" cy="8.5" r="4.5"/><path d="m12 12 4 4"/></svg>
        <input type="search" aria-label="搜索商品标题或 ASIN" placeholder="搜索标题或 ASIN" value={search}
          onInput={event => setSearch(event.currentTarget.value)} />
      </label>
    </header>
    <div class="table-tools">
      <div class="filters" role="group" aria-label="筛选巡查结果">
        {filters.map(({ key, label, count }) => <button key={key} type="button" class="filter" aria-label={label}
          aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}<span aria-hidden="true">{count}</span></button>)}
      </div>
      <span class="row-count" role="status">{snapshot ? `${rows.length} / ${items?.length ?? 0} 项` : ''}</span>
    </div>
    <div class="table-scroll" role="region" aria-label="巡查价格表格" tabIndex={0}>
      <table>
        <thead><tr>{(Object.keys(sortableColumns) as SortKey[]).map(key => <th key={key} scope="col"
          class={`col-${key}`} aria-sort={sort.key === key ? (sort.direction === 'asc' ? 'ascending' : 'descending') : undefined}>
          <button type="button" onClick={() => sortBy(key)} aria-label={sortableColumns[key]}>
            {sortableColumns[key]}{sort.key === key && <span class="sort-arrow" aria-hidden="true">{sort.direction === 'asc' ? '↑' : '↓'}</span>}
          </button>
        </th>)}</tr></thead>
        <tbody>{rows.map(row => <tr key={row.asin} class={row.below_own_price ? 'below' : ''}>
          <td>
            <div class="product-title">
              {!demo && row.title && /^[A-Z0-9]{10}$/.test(row.asin)
                ? <a href={`https://www.amazon.com/dp/${encodeURIComponent(row.asin)}`} target="_blank" rel="noopener noreferrer">{row.title}</a>
                : (row.title || '商品标题未读取')}
            </div>
            <span class="asin">{row.asin}</span>
          </td>
          <td class="amount">{row.price}</td>
          <td class={row.offer === '—' ? 'muted' : ''}>{row.offer}</td>
          <td class={row.comparison === '—' ? 'muted' : 'comparison'}>{row.comparison}</td>
          <td><div class={row.error ? 'row-issue' : 'row-status'}>{row.status}</div>
            <div class="check-time">{row.checked_at}</div>{row.error && <div class="row-detail">{row.error}</div>}
          </td>
        </tr>)}</tbody>
      </table>
    </div>
    {!rows.length && <div class="empty-state">
      <strong>{loading ? '正在读取当前分组…' : !snapshot && error ? '暂时无法读取巡查结果'
        : !items?.length ? '当前组还没有商品' : '没有匹配的结果'}</strong>
      <p>{loading ? '读取完成后将在这里显示结果。' : !snapshot && error ? '请使用「重新读取」再试一次。'
        : !items?.length ? '用「编辑 ASIN 名单」添加要巡查的商品。' : '清空搜索或切换筛选条件。'}</p>
      {!!items?.length && !loading && <button type="button" class="subtle" onClick={() => { setSearch(''); setFilter('all'); }}>清除筛选</button>}
    </div>}
    <footer class="table-note">只显示当日购买价。优惠单独列出，不从购买价扣减；列表价不参与对比。</footer>
  </section>;
}
