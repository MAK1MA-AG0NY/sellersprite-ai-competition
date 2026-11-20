import { ApiError } from './api';
import type { BulkPlan, Group, PriceRow, WorkspaceApi } from './api';
import { usdPrice } from './model';

// Fictional products. This adapter never reads a database or calls a network API.
const example = (code: number, title: string, price: string, offer: string, status = '今日已检查',
                 checked_at = '11-19 09:42', error = ''): PriceRow => ({
  asin: `B0DEMO${String(code).padStart(4, '0')}`, title, price, offer, status, checked_at, error,
  comparison: '—', below_own_price: false,
});
const examples = [
  example(1, 'Northline Trail 500 · 便携式储能电源', '$249.00', '页面券：减 $20'),
  example(2, 'Fieldwell Camp 600 · 户外备用电源', '$279.99', '今日未发现明确优惠', '今日已检查', '11-19 09:44'),
  example(3, 'Ridgeline S600 · 户外储能电源', '$299.00', '本次优惠未读取', '今日已检查', '11-19 09:45'),
  example(4, 'Harbor North 720 · 便携式储能电源', '$329.00', '页面券：减 $30', '今日已检查', '11-19 09:46'),
  example(5, 'Pinecrest Base 800 · 磷酸铁锂储能电源', '$379.99', '今日未发现明确优惠', '今日已检查', '11-19 09:48'),
  example(6, 'Summit Yard 500 · 应急储能电源', '—', '缺货时优惠不可确认', '今日缺货 / 不可购买', '11-19 09:49'),
  example(7, 'Outpost Mini 450 · 户外便携电源', '—', '采集失败，优惠未确认', '采集失败', '11-19 09:50', '示例：未能读取商品页。'),
  example(8, 'Riverbank Go 550 · 便携式储能电源', '—', '—', '今天未巡查', '11-18 16:18 · 非今天'),
  example(9, 'Fieldwell Fold 100 · 折叠式太阳能板', '$109.00', '页面券：减 $10'),
  example(10, 'Northline Sun 120 · 便携式太阳能板', '$129.00', '今日未发现明确优惠'),
  example(11, 'Harbor North Fold 160 · 折叠式太阳能板', '$159.99', '本次优惠未读取'),
  example(12, 'Pinecrest Armor Lite · 户外三防手机', '$139.99', '今日未发现明确优惠'),
  example(13, 'Ridgeline Scout · 户外三防手机', '$189.00', '页面券：减 $15'),
];
const catalog = new Map(examples.map(row => [row.asin, row]));

export function createDemoApi(): WorkspaceApi {
  let nextId = 4;
  let revision = 0;
  let jobSequence = 0;
  let active: { id: string; started: number; total: number; name: string } | null = null;
  const groups = [
    { id: 1, name: '便携电站', asins: examples.slice(0, 8).map(row => row.asin), price: '299.00' },
    { id: 2, name: '太阳能板', asins: examples.slice(8, 11).map(row => row.asin), price: '129.00' },
    { id: 3, name: '三防手机', asins: examples.slice(11).map(row => row.asin), price: '169.00' },
  ];
  type DemoGroup = typeof groups[number];
  const summary = ({ id, name, asins }: DemoGroup): Group => ({ id, name, count: asins.length });
  function lookup(id: number): DemoGroup {
    const found = groups.find(group => group.id === id);
    if (!found) throw new ApiError(404, '分组不存在。');
    return found;
  }
  const start = (name: string, total: number) => {
    const id = `demo-${++jobSequence}`;
    active = { id, name, total, started: performance.now() };
    return { job_id: id };
  };
  function plan(): BulkPlan {
    const withAsins = groups.filter(group => group.asins.length);
    const all = withAsins.flatMap(group => group.asins);
    const unique = new Set(all).size;
    return { groups: withAsins.map(summary), group_count: withAsins.length, empty_group_count: groups.length - withAsins.length,
      membership_count: all.length, unique_asin_count: unique, duplicates_skipped: all.length - unique,
      limit: 200, can_run: unique > 0 && unique <= 200, plan_hash: `demo-plan-${revision}` };
  }
  function checkedRow(asin: string, reference: string): PriceRow {
    const row = catalog.get(asin) ?? { asin, price: '—', title: '', offer: '—', status: '今天未巡查',
      checked_at: '—', error: '', comparison: '—', below_own_price: false };
    const price = usdPrice(row);
    const delta = price === null || !reference ? null : Math.round(Number(reference) * 100) - Math.round(price * 100);
    const comparison = delta === null ? (!reference ? '未设本品价' : '—') : delta === 0 ? '与本品相同'
      : `${delta > 0 ? '低于' : '高于'}本品 $${(Math.abs(delta) / 100).toFixed(2)}`;
    return { ...row, comparison: row.status.includes('缺货') ? '不比较（缺货）' : row.error ? '无法判断' : comparison,
      below_own_price: delta !== null && delta > 0 };
  }

  return {
    async groups() { return { items: groups.map(summary), default_group_id: groups[0].id }; },
    async snapshot(id) {
      const group = lookup(id);
      return { group_id: id, group_name: group.name, asins: group.asins, max_asins: 20,
        reference_price: group.price, items: group.asins.map(asin => checkedRow(asin, group.price)),
        today: '2026-11-19', running_job_id: active?.id ?? null };
    },
    async saveGroup(name, id) {
      const group = id === undefined ? { id: nextId++, name, asins: [], price: '' } : lookup(id);
      group.name = name;
      if (id === undefined) groups.push(group);
      revision += 1;
      return { group: summary(group) };
    },
    async deleteGroup(id) {
      lookup(id);
      if (groups.length <= 1) throw new ApiError(400, '至少保留一个竞品组。');
      groups.splice(groups.findIndex(group => group.id === id), 1);
      revision += 1;
      return { group_id: groups[0].id };
    },
    async saveAsins(id, asins) { lookup(id).asins = asins; revision += 1; },
    async savePrice(id, price) { lookup(id).price = price ? Number(price).toFixed(2) : ''; },
    async start(id) { const group = lookup(id); return start(group.name, group.asins.length); },
    async bulkPlan() { return plan(); },
    async startAll(hash) {
      const current = plan();
      if (hash !== current.plan_hash || !current.can_run) throw new ApiError(409, '计划已变更，请重新预览。');
      return start('全部分组', current.unique_asin_count);
    },
    async job(id) {
      if (!active || active.id !== id) throw new ApiError(404, '任务不存在。');
      const { started, total, name } = active;
      const done = Math.min(total, Math.floor(total * (performance.now() - started) / 3300));
      if (done >= total) {
        active = null;
        return { status: 'done', done, total, group_name: name, detail: '模拟巡查完成；未访问 Amazon，示例价格不变。' };
      }
      return { status: 'running', done, total, group_name: name, detail: '正在模拟巡查，不发送网络请求。' };
    },
  };
}
