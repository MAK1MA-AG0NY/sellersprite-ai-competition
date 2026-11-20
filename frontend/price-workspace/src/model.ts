import type { PriceRow } from './api';

export type Filter = 'all' | 'below' | 'unpriced';
export type SortKey = 'title' | 'price' | 'offer' | 'comparison' | 'checked';
export interface Sort { key: SortKey; direction: 'asc' | 'desc' }

export const sortableColumns: Record<SortKey, string> = {
  title: '商品 / ASIN', price: '今日购买价', offer: '今日优惠', comparison: '对比本品', checked: '检查时间 / 状态',
};

export function usdPrice(row: PriceRow): number | null {
  if (/非 USD|币种未确认|未读|缺货|不可购买/.test(row.status)) return null;
  const price = row.price.trim().replaceAll(',', '');
  return /^\$\d+(?:\.\d+)?$/.test(price) ? Number(price.slice(1)) : null;
}

function sortValue(row: PriceRow, key: SortKey): string | number | null {
  switch (key) {
    case 'title': return row.title.trim() || row.asin;
    case 'price': return usdPrice(row);
    case 'offer': return row.offer === '—' ? null : row.offer;
    case 'comparison': return /未设本品价|无法判断|不比较|^—$/.test(row.comparison) ? null : Number(row.below_own_price);
    case 'checked': return row.checked_at === '—' ? null : `${row.checked_at} ${row.status}`;
  }
}

const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });

export function displayRows(rows: PriceRow[], filter: Filter, search: string, sort: Sort): PriceRow[] {
  const query = search.trim().toLowerCase();
  return rows.filter(row => {
    if (filter === 'below' && !row.below_own_price) return false;
    if (filter === 'unpriced' && usdPrice(row) !== null) return false;
    return !query || row.asin.toLowerCase().includes(query) || row.title.toLowerCase().includes(query);
  }).map(row => ({ row, value: sortValue(row, sort.key) })).sort((a, b) => {
    if (a.value === null || b.value === null) return Number(a.value === null) - Number(b.value === null);
    const result = typeof a.value === 'number' && typeof b.value === 'number'
      ? a.value - b.value : collator.compare(String(a.value), String(b.value));
    return result * (sort.direction === 'asc' ? 1 : -1);
  }).map(({ row }) => row);
}

export function parseAsinDraft(text: string, limit: number): string[] {
  const asins = [...new Set(text.trim().toUpperCase().split(/[,，;；\s]+/).filter(Boolean))];
  const invalid = asins.find(asin => !/^[A-Z0-9]{10}$/.test(asin));
  if (invalid) throw new Error(`「${invalid.slice(0, 50)}」不是有效 ASIN。请输入 10 位字母或数字，不要粘贴链接。`);
  if (asins.length > limit) throw new Error(`每组最多 ${limit} 个不同的 ASIN。`);
  return asins;
}

export function parsePriceDraft(text: string): string | null {
  const value = text.trim();
  if (!value) return null;
  if (value.length > 30 || !/^\d+(?:\.\d{1,2})?$/.test(value) || Number(value) <= 0 || Number(value) > 1_000_000) {
    throw new Error('请输入大于 0 且不超过 1,000,000 的 USD 金额，最多两位小数。');
  }
  return value;
}
