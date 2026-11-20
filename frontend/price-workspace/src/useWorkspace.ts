import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { ApiError, errorText } from './api';
import type { Group, Job, Snapshot, WorkspaceApi } from './api';

export interface Notice { text: string; warning?: boolean }

export function useWorkspace(api: WorkspaceApi) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState('');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState<Job | null>(null);
  const [pollError, setPollError] = useState('');
  const selected = useRef<number | null>(null);
  const activeRead = useRef<AbortController | null>(null);

  const load = useCallback(async (preferred = selected.current, reloadGroups = true) => {
    activeRead.current?.abort();
    const controller = new AbortController();
    const { signal } = controller;
    activeRead.current = controller;
    setLoading(true);
    setReadError('');
    if (preferred !== null) {
      selected.current = preferred;
      setSelectedId(preferred);
    }
    try {
      let id = preferred;
      if (reloadGroups || id === null) {
        const catalog = await api.groups(signal);
        if (signal.aborted) return;
        setGroups(catalog.items);
        if (!catalog.items.some(group => group.id === id)) id = catalog.default_group_id;
      }
      if (id === null) throw new Error('没有可读取的竞品组。');
      selected.current = id;
      setSelectedId(id);
      const result = await api.snapshot(id, signal);
      if (signal.aborted) return;
      if (id !== result.group_id) throw new Error('返回的巡查结果与所选分组不符。');
      setSnapshot(result);
      setGroups(list => list.map(group => group.id === id
        ? { ...group, name: result.group_name, count: result.asins.length } : group));
      // A read that races a job's completion must not stop its terminal poll.
      if (result.running_job_id) setJobId(result.running_job_id);
    } catch (error) {
      if (!signal.aborted) setReadError(errorText(error));
    } finally {
      if (!signal.aborted) {
        activeRead.current = null;
        setLoading(false);
      }
    }
  }, [api]);

  useEffect(() => {
    void load();
    return () => activeRead.current?.abort();
  }, [load]);

  useEffect(() => {
    setProgress(null);
    setPollError('');
    if (!jobId) return;
    const id = jobId;
    let stopped = false;
    let timer: number | undefined;
    let activePoll: AbortController | undefined;

    function finish(message: Notice) {
      stopped = true;
      setNotice(message);
      setJobId(null);
      void load();
    }

    async function poll() {
      window.clearTimeout(timer);
      if (stopped || document.hidden || activePoll) return;
      const controller = new AbortController();
      activePoll = controller;
      try {
        const job = await api.job(id, controller.signal);
        if (stopped || controller.signal.aborted) return;
        setPollError('');
        setProgress(job);
        if (job.status !== 'running') {
          const text = job.detail || (job.status === 'done' ? '巡查完成，正在刷新结果。' : '巡查未完成，请检查结果后重试。');
          const failed = job.failed ?? 0;
          finish({ text: failed > 0 ? `${text}（${failed} 项失败）` : text, warning: job.status !== 'done' || failed > 0 });
        }
      } catch (error) {
        if (stopped || controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          finish({ text: '任务记录已不存在，正在重新读取巡查状态。', warning: true });
        } else {
          setPollError(`暂时无法读取巡查进度：${errorText(error)}`);
        }
      } finally {
        activePoll = undefined;
        if (!stopped && !document.hidden) timer = window.setTimeout(poll, controller.signal.aborted ? 0 : 1800);
      }
    }

    function visibilityChanged() {
      if (document.hidden) {
        window.clearTimeout(timer);
        activePoll?.abort();
      } else void poll();
    }

    document.addEventListener('visibilitychange', visibilityChanged);
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      activePoll?.abort();
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [api, jobId, load]);

  function followJob(id: string) {
    setNotice(null);
    setJobId(id);
  }

  return { groups, selectedId, snapshot: snapshot?.group_id === selectedId ? snapshot : null,
    loading, readError, load, notice, setNotice, jobId, progress, pollError, followJob };
}
