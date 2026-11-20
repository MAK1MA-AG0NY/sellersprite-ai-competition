export interface Group {
  id: number;
  name: string;
  count: number;
}

export interface PriceRow {
  asin: string;
  title: string;
  price: string;
  offer: string;
  comparison: string;
  below_own_price: boolean;
  checked_at: string;
  status: string;
  error: string;
}

export interface Snapshot {
  group_id: number;
  group_name: string;
  asins: string[];
  max_asins: number;
  reference_price: string;
  items: PriceRow[];
  today: string;
  running_job_id: string | null;
}

export interface BulkPlan {
  groups: Group[];
  group_count: number;
  empty_group_count: number;
  membership_count: number;
  unique_asin_count: number;
  duplicates_skipped: number;
  limit: number;
  can_run: boolean;
  plan_hash: string;
}

export interface Job {
  status: 'running' | 'done' | 'error';
  done: number;
  total: number;
  detail?: string;
  failed?: number;
  group_name?: string;
}

export interface WorkspaceApi {
  groups(signal: AbortSignal): Promise<{ items: Group[]; default_group_id: number }>;
  snapshot(id: number, signal: AbortSignal): Promise<Snapshot>;
  saveGroup(name: string, id?: number): Promise<{ group: Group }>;
  deleteGroup(id: number): Promise<{ group_id: number }>;
  saveAsins(id: number, asins: string[]): Promise<unknown>;
  savePrice(id: number, price: string | null): Promise<unknown>;
  start(id: number): Promise<{ job_id: string }>;
  bulkPlan(): Promise<BulkPlan>;
  startAll(hash: string): Promise<{ job_id: string }>;
  job(id: string, signal: AbortSignal): Promise<Job>;
}

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请重试。';
}

async function request<T>(path: string, method = 'GET', data?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    method, signal, cache: 'no-store',
    ...(data === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, typeof body?.detail === 'string' ? body.detail : `请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}

export const onlineApi: WorkspaceApi = {
  groups: (signal) => request('amazon/groups', 'GET', undefined, signal),
  snapshot: (id, signal) => request(`amazon/simple?group_id=${id}`, 'GET', undefined, signal),
  saveGroup: (name, id) => request(id === undefined ? 'amazon/groups' : `amazon/groups/${id}`,
                                  id === undefined ? 'POST' : 'PATCH', { name }),
  deleteGroup: (id) => request(`amazon/groups/${id}`, 'DELETE'),
  saveAsins: (group_id, asins) => request('amazon/watch', 'POST', { group_id, asins }),
  savePrice: (group_id, price) => request('amazon/reference-price', 'POST', { group_id, price }),
  start: (group_id) => request('amazon/run', 'POST', { group_id, simple_mode: true }),
  bulkPlan: () => request('amazon/run-all/preview'),
  startAll: (plan_hash) => request('amazon/run-all', 'POST', { confirm: true, plan_hash }),
  job: (id, signal) => request(`job/${encodeURIComponent(id)}`, 'GET', undefined, signal),
};
