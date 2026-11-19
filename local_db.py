# -*- coding: utf-8 -*-
"""
本地 SQLite 存储：竞品查询快照累积库
====================================
表结构：
  batches(id PK, task_id, created_at, requested, returned)   每次查询批次
  snapshots(id PK, asin, batch_id, taken_at, data)           每行产品完整 JSON 快照
取数：latest(asins) 返回每个 ASIN 最近一次快照（解析后的 dict）
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "deep_results" / "deepdive.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id        TEXT PRIMARY KEY,
    task_id   TEXT,
    created_at TEXT,
    requested INTEGER,
    returned  INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asin     TEXT NOT NULL,
    batch_id TEXT,
    taken_at TEXT,
    data     TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_asin ON snapshots(asin);
CREATE INDEX IF NOT EXISTS idx_snap_batch ON snapshots(batch_id);
CREATE TABLE IF NOT EXISTS ads_keywords (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source   TEXT NOT NULL,
    market   TEXT,
    keyword  TEXT NOT NULL,
    month    TEXT,
    data     TEXT,
    UNIQUE(source, keyword, month)
);
CREATE INDEX IF NOT EXISTS idx_ads_kw ON ads_keywords(keyword);
CREATE TABLE IF NOT EXISTS ads_kw_full (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asin     TEXT NOT NULL,
    ad_id    TEXT NOT NULL,
    keyword  TEXT NOT NULL,
    taken_at TEXT,
    data     TEXT,
    UNIQUE(asin, ad_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_adsfull_kw ON ads_kw_full(keyword);
CREATE TABLE IF NOT EXISTS ads_groups (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asin     TEXT NOT NULL,
    ad_id    TEXT NOT NULL,
    taken_at TEXT,
    data     TEXT,
    UNIQUE(asin, ad_id)
);
CREATE TABLE IF NOT EXISTS amz_watch (
    asin         TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 1,
    current_data TEXT NOT NULL DEFAULT '{}',
    last_checked TEXT,
    last_status  TEXT,
    last_error   TEXT
);
CREATE TABLE IF NOT EXISTS amz_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asin       TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    changes    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_amz_changes_asin ON amz_changes(asin, id);
"""


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.executescript(SCHEMA)
    return c


def save_batch(batch_id, task_id, requested, rows):
    """整批快照入库；rows 为查询返回的产品 dict 列表。返回入库行数。"""
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    try:
        c.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?)",
                  (batch_id, task_id, now, requested, len(rows)))
        for r in rows:
            asin = str(r.get("asin") or "").strip().upper()
            if asin:
                c.execute("INSERT INTO snapshots(asin, batch_id, taken_at, data) VALUES (?,?,?,?)",
                          (asin, batch_id, now, json.dumps(r, ensure_ascii=False)))
        c.commit()
        return len(rows)
    finally:
        c.close()


def latest(asins=None):
    """每个 ASIN 取最新快照；asins=None 取全部。返回 [dict] 列表。"""
    c = _conn()
    try:
        if asins:
            q = ",".join("?" for _ in asins)
            cur = c.execute(
                f"SELECT s.asin, s.data FROM snapshots s "
                f"JOIN (SELECT asin, MAX(id) mx FROM snapshots WHERE asin IN ({q}) GROUP BY asin) t "
                f"ON s.asin = t.asin AND s.id = t.mx", [a.upper() for a in asins])
        else:
            cur = c.execute(
                "SELECT s.asin, s.data FROM snapshots s "
                "JOIN (SELECT asin, MAX(id) mx FROM snapshots GROUP BY asin) t "
                "ON s.asin = t.asin AND s.id = t.mx")
        return [json.loads(row[1]) for row in cur.fetchall()]
    finally:
        c.close()


def stats():
    c = _conn()
    try:
        n_snap = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        n_asin = c.execute("SELECT COUNT(DISTINCT asin) FROM snapshots").fetchone()[0]
        n_batch = c.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        return {"snapshots": n_snap, "asins": n_asin, "batches": n_batch}
    finally:
        c.close()


# ---------- 广告关键词（ads-insights） ----------

def save_ads(source, market, rows):
    """广告关键词×月份行入库；rows 为 关键词×月份 行。返回入库行数。
    同键(source,keyword,month)冲突时逐字段合并（已有非空值优先），
    避免"只有搜索量的趋势行"覆盖掉含购买率等指标的完整行。"""
    c = _conn()
    try:
        n = 0
        for r in rows:
            old = None
            cur = c.execute("SELECT data FROM ads_keywords WHERE source=? AND keyword=? AND month=?",
                            (source, r["keyword"], r.get("month") or "")).fetchone()
            if cur:
                try:
                    old = json.loads(cur[0])
                except Exception:
                    old = None
            if isinstance(old, dict):
                for k, v in r.items():
                    if v is not None and old.get(k) is None:
                        old[k] = v
                r = old
            c.execute("INSERT OR REPLACE INTO ads_keywords(source, market, keyword, month, data) "
                      "VALUES (?,?,?,?,?)",
                      (source, market, r["keyword"], r.get("month") or "",
                       json.dumps(r, ensure_ascii=False)))
            n += 1
        c.commit()
        return n
    finally:
        c.close()


def official_metrics():
    """keyword(小写) → 官方指标 {月购买量/购买率/需供比/转化总占比/ABA排名/商品数/月搜索量}。
    来源 = ads_keywords 全部来源（kw_miner + 导入文件）；逐字段取最新月份的非空值。"""
    c = _conn()
    try:
        cur = c.execute("SELECT keyword, month, data FROM ads_keywords ORDER BY month DESC")
        out = {}
        fields = ("月购买量", "购买率", "需供比", "转化总占比", "ABA排名", "商品数", "月搜索量")
        for kw, month, data in cur.fetchall():
            try:
                d = json.loads(data)
            except Exception:
                continue
            e = out.setdefault(str(kw).strip().lower(), {})
            for f in fields:
                if e.get(f) is None and d.get(f) is not None:
                    e[f] = d[f]
        return out
    finally:
        c.close()


def kw_miner_done(keywords):
    """返回已有关键词挖掘官方数据（月购买量非空）的关键词集合（小写）。
    用于②报告自动补齐时跳过已查询过的词，节省免费查询次数。"""
    kws = [str(k) for k in (keywords or [])]
    if not kws:
        return set()
    c = _conn()
    try:
        q = ",".join("?" for _ in kws)
        cur = c.execute(
            "SELECT DISTINCT lower(keyword) FROM ads_keywords WHERE source='kw_miner' "
            f"AND json_extract(data,'$.月购买量') IS NOT NULL AND lower(keyword) IN ({q})", kws)
        return {r[0] for r in cur.fetchall()}
    finally:
        c.close()


def ads_all():
    """全部广告关键词行，按来源分组：[{source, market, rows:[原始dict]}]"""
    c = _conn()
    try:
        cur = c.execute("SELECT source, market, data FROM ads_keywords ORDER BY source, keyword, month")
        grouped = {}
        for source, market, data in cur.fetchall():
            g = grouped.setdefault(source, {"source": source, "market": market, "rows": []})
            g["rows"].append(json.loads(data))
        return list(grouped.values())
    finally:
        c.close()


def save_ads_full(asin, result):
    """ads_full 的收割结果入库。返回入库词数。"""
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    try:
        n = 0
        for g in result.get("results", []):
            for k in g.get("keywords", []):
                kw = str(k.get("keyword") or "").strip()
                if not kw:
                    continue
                c.execute("INSERT OR REPLACE INTO ads_kw_full(asin, ad_id, keyword, taken_at, data) "
                          "VALUES (?,?,?,?,?)",
                          (asin, str(g.get("adId") or ""), kw, now,
                           json.dumps(k, ensure_ascii=False)))
                n += 1
        c.commit()
        return n
    finally:
        c.close()


def ads_kw_full_all():
    """全部已收割广告词，按 ASIN 分组：[{source_asin, rows:[原始dict+_taken_at]}]"""
    c = _conn()
    try:
        cur = c.execute("SELECT asin, taken_at, data FROM ads_kw_full ORDER BY asin, keyword")
        grouped = {}
        for asin, taken_at, data in cur.fetchall():
            row = json.loads(data)
            row["_taken_at"] = taken_at
            grouped.setdefault(asin, {"source_asin": asin, "rows": []})["rows"].append(row)
        return list(grouped.values())
    finally:
        c.close()


def save_groups(asin, result):
    """xds/stat 的组结构入库（含每周 keywordsCount 时间线）。返回组数。"""
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    try:
        n = 0
        for g in result.get("groups", []):
            ad_id = str(g.get("adId") or "")
            if not ad_id:
                continue
            c.execute("INSERT OR REPLACE INTO ads_groups(asin, ad_id, taken_at, data) "
                      "VALUES (?,?,?,?)",
                      (asin, ad_id, now, json.dumps(g, ensure_ascii=False)))
            n += 1
        c.commit()
        return n
    finally:
        c.close()


def groups_for(asin):
    """某 ASIN 已入库的组结构列表。
    防御过滤：接口历史响应里混有"发散 ASIN"的广告组（组自身 asin 字段 != 查询 ASIN），
    即使已被旧版入库也不参与收割，防止他品关键词错记到本 ASIN 名下。"""
    c = _conn()
    try:
        cur = c.execute("SELECT ad_id, data FROM ads_groups WHERE asin=? ORDER BY id", (asin,))
        out = []
        asin_u = str(asin).strip().upper()
        for ad_id, data in cur.fetchall():
            g = json.loads(data)
            own = str(g.get("asin") or "").strip().upper()
            if own and own != asin_u:
                continue   # 发散 ASIN 的组，丢弃
            g.setdefault("adId", ad_id)
            out.append(g)
        return out
    finally:
        c.close()


def purge_foreign_ads():
    """清除历史误抓的发散 ASIN 数据：
    1) ads_groups 中 组自身 asin != 存储 asin 的行；
    2) ads_kw_full 中 ad_id 归属（按组自身 asin 判定）!= 存储 asin 的关键词行；
    3) ads_kw_full 中 行级 asin（该词实际投放商品，组合组内会混投他品）!= 存储 asin 的行。
    返回 {'groups_deleted': n, 'keywords_deleted': n}。"""
    c = _conn()
    try:
        # ad_id -> 组自身 asin（以 ads_groups 数据里的 asin 字段为准）
        own_map = {}
        for ad_id, data in c.execute("SELECT ad_id, data FROM ads_groups"):
            try:
                g = json.loads(data)
            except Exception:
                continue
            own = str(g.get("asin") or "").strip().upper()
            if own:
                own_map[ad_id] = own
        n_kw = 0
        for sasin, ad_id, kw in c.execute("SELECT asin, ad_id, keyword FROM ads_kw_full").fetchall():
            g_asin = own_map.get(str(ad_id))
            if g_asin and g_asin != str(sasin).strip().upper():
                c.execute("DELETE FROM ads_kw_full WHERE asin=? AND ad_id=? AND keyword=?",
                          (sasin, ad_id, kw))
                n_kw += 1
        # 第二层：组合/店铺型广告组内混投的其他产品——关键词行自身 asin 字段
        # = 该词实际投放的商品 ASIN，与存储 ASIN 不符的行同样是发散噪声
        n_kw2 = c.execute(
            "SELECT COUNT(*) FROM ads_kw_full WHERE "
            "json_extract(data,'$.asin') IS NOT NULL AND json_extract(data,'$.asin') != '' AND "
            "UPPER(json_extract(data,'$.asin')) != UPPER(asin)").fetchone()[0]
        c.execute(
            "DELETE FROM ads_kw_full WHERE "
            "json_extract(data,'$.asin') IS NOT NULL AND json_extract(data,'$.asin') != '' AND "
            "UPPER(json_extract(data,'$.asin')) != UPPER(asin)")
        n_kw += n_kw2
        n_g = c.execute(
            "SELECT COUNT(*) FROM ads_groups WHERE "
            "json_extract(data,'$.asin') IS NOT NULL AND "
            "UPPER(json_extract(data,'$.asin')) != UPPER(asin)").fetchone()[0]
        c.execute(
            "DELETE FROM ads_groups WHERE "
            "json_extract(data,'$.asin') IS NOT NULL AND "
            "UPPER(json_extract(data,'$.asin')) != UPPER(asin)")
        c.commit()
        return {"groups_deleted": n_g, "keywords_deleted": n_kw}
    finally:
        c.close()


# ---------- Amazon US 手动巡查：当前有效值 + 仅变化历史 ----------

AMZ_FIELDS = ("title", "price", "reference_price", "currency", "coupon", "promotions", "availability",
              "offer_types", "offer_evidence")
AMZ_CHECK_METADATA_FIELDS = ("offer_observed_at", "offer_read_status")


def _amz_init_extended(c):
    """Lazily initialize Amazon observations, own-product, analysis, and scheduler storage."""
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS amz_observations ("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, asin TEXT NOT NULL, observed_at TEXT NOT NULL, data TEXT NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_amz_observations_asin_time ON amz_observations(asin, observed_at DESC, id DESC)")
        c.execute("CREATE TABLE IF NOT EXISTS amz_group_products ("
                  "group_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', asin TEXT, spec_notes TEXT NOT NULL DEFAULT '', "
                  "manual_price TEXT, protected_floor TEXT, currency TEXT NOT NULL DEFAULT 'USD', "
                  "price_source TEXT NOT NULL DEFAULT 'manual', updated_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS amz_analyses ("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, created_at TEXT NOT NULL, "
                  "model TEXT NOT NULL, kind TEXT NOT NULL, observation_time TEXT NOT NULL, input_json TEXT NOT NULL, "
                  "output_json TEXT NOT NULL, is_stale INTEGER NOT NULL DEFAULT 0)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_amz_analyses_group ON amz_analyses(group_id, id DESC)")
        c.execute("CREATE TABLE IF NOT EXISTS amz_schedules ("
                  "group_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, local_time TEXT NOT NULL DEFAULT '08:00', "
                  "timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', updated_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS amz_schedule_runs ("
                  "group_id INTEGER NOT NULL, run_date TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', "
                  "job_id TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(group_id, run_date))")


def _amz_init_groups(c):
    """仅 Amazon 分组接口惰性建表；默认组存在后不再从 enabled 重建成员。"""
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS amz_groups ("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS amz_group_asins ("
                  "group_id INTEGER, asin TEXT, PRIMARY KEY(group_id, asin))")
        # 单条条件 INSERT 串行化首次初始化，避免并发连接各建一个默认组。
        created = c.execute(
            "INSERT INTO amz_groups(name, created_at) SELECT ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM amz_groups)",
            ("默认分组", datetime.now().astimezone().isoformat(timespec="seconds")))
        if created.rowcount:
            c.execute("INSERT INTO amz_group_asins(group_id, asin) "
                      "SELECT ?, asin FROM amz_watch WHERE enabled=1", (created.lastrowid,))
    _amz_init_extended(c)


def _amz_group(c, group_id=None):
    args = [] if group_id is None else [group_id]
    where = "" if group_id is None else " WHERE g.id=?"
    row = c.execute(
        "SELECT g.id, g.name, COUNT(m.asin) FROM amz_groups g "
        "LEFT JOIN amz_group_asins m ON m.group_id=g.id" + where +
        " GROUP BY g.id ORDER BY g.id LIMIT 1", args).fetchone()
    if not row:
        raise ValueError("Amazon 分组不存在")
    return {"id": row[0], "name": row[1], "count": row[2]}


def _amz_group_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
        raise ValueError("分组名称须为 1–60 个字符")
    return name.strip()


def _amz_sync_enabled(c):
    """enabled 仅兼容旧读取方；分组成员表才是名单的唯一来源。"""
    c.execute("UPDATE amz_watch SET enabled=EXISTS("
              "SELECT 1 FROM amz_group_asins m WHERE m.asin=amz_watch.asin)")


def amz_groups():
    """按创建顺序返回分组及各自成员数，不复制 ASIN 的当前值或历史。"""
    c = _conn()
    try:
        _amz_init_groups(c)
        rows = c.execute(
            "SELECT g.id, g.name, COUNT(m.asin) FROM amz_groups g "
            "LEFT JOIN amz_group_asins m ON m.group_id=g.id GROUP BY g.id ORDER BY g.id").fetchall()
        return [{"id": i, "name": name, "count": count} for i, name, count in rows]
    finally:
        c.close()


def amz_get_group(group_id=None):
    """None 取现存首组；不依赖默认组固定为 id=1。"""
    c = _conn()
    try:
        _amz_init_groups(c)
        return _amz_group(c, group_id)
    finally:
        c.close()


def amz_create_group(name):
    name = _amz_group_name(name)
    c = _conn()
    try:
        _amz_init_groups(c)
        with c:
            c.execute("BEGIN IMMEDIATE")
            created = c.execute("INSERT INTO amz_groups(name, created_at) VALUES (?, ?)",
                                (name, datetime.now().astimezone().isoformat(timespec="seconds")))
            return _amz_group(c, created.lastrowid)
    finally:
        c.close()


def amz_rename_group(group_id, name):
    name = _amz_group_name(name)
    c = _conn()
    try:
        _amz_init_groups(c)
        with c:
            c.execute("BEGIN IMMEDIATE")
            group = _amz_group(c, group_id)
            c.execute("UPDATE amz_groups SET name=? WHERE id=?", (name, group["id"]))
            return _amz_group(c, group["id"])
    finally:
        c.close()


def amz_delete_group(group_id):
    """只删除分类及成员，至少保留一组，避免下次连接再次触发旧名单迁移。"""
    c = _conn()
    try:
        _amz_init_groups(c)
        with c:
            # 检查与删除共持写锁，两个并发删除也不能删空分组。
            c.execute("BEGIN IMMEDIATE")
            group = _amz_group(c, group_id)
            if c.execute("SELECT COUNT(*) FROM amz_groups").fetchone()[0] <= 1:
                raise ValueError("至少保留一个 Amazon 分组")
            c.execute("DELETE FROM amz_group_asins WHERE group_id=?", (group["id"],))
            c.execute("DELETE FROM amz_groups WHERE id=?", (group["id"],))
            _amz_sync_enabled(c)
    finally:
        c.close()


def _amz_watch_items(c, group_id):
    rows = c.execute(
        "SELECT w.asin, w.current_data, w.last_checked, w.last_status, w.last_error "
        "FROM amz_watch w JOIN amz_group_asins m ON m.asin=w.asin "
        "WHERE m.group_id=? ORDER BY w.rowid", (group_id,)).fetchall()
    return [{"asin": a, "url": f"https://www.amazon.com/dp/{a}",
             "current": json.loads(data), "last_checked": checked,
             "last_status": status, "last_error": error}
            for a, data, checked, status, error in rows]


def amz_watchlist(group_id=None):
    c = _conn()
    try:
        _amz_init_groups(c)
        return _amz_watch_items(c, _amz_group(c, group_id)["id"])
    finally:
        c.close()


def amz_get_watch(asin):
    if not asin:
        return None
    c = _conn()
    try:
        _amz_init_groups(c)
        row = c.execute("SELECT current_data, last_checked, last_status, last_error FROM amz_watch WHERE asin=?", (asin,)).fetchone()
        if not row:
            return None
        return {"asin": asin, "current": json.loads(row[0]), "last_checked": row[1],
                "last_status": row[2], "last_error": row[3], "market": "US", "delivery_zip": "10001"}
    finally:
        c.close()


def amz_save_watchlist(asins, group_id=None):
    """只替换当前组成员；跨组共用原基线/历史。ASIN 由调用方校验。"""
    asins = list(dict.fromkeys(asins))
    c = _conn()
    try:
        _amz_init_groups(c)
        with c:
            c.execute("BEGIN IMMEDIATE")
            group = _amz_group(c, group_id)
            c.execute("DELETE FROM amz_group_asins WHERE group_id=?", (group["id"],))
            c.executemany("INSERT INTO amz_watch(asin) VALUES (?) ON CONFLICT(asin) DO NOTHING",
                          [(a,) for a in asins])
            c.executemany("INSERT INTO amz_group_asins(group_id, asin) VALUES (?, ?)",
                          [(group["id"], a) for a in asins])
            _amz_sync_enabled(c)
            return _amz_watch_items(c, group["id"])
    finally:
        c.close()


def amz_record(asin, data, checked_at=None, note=""):
    """一次事务更新有效值与差异。None/缺失字段不覆盖；空串表示确认未展示。"""
    checked_at = checked_at or datetime.now().astimezone().isoformat(timespec="seconds")
    all_fields = AMZ_FIELDS + AMZ_CHECK_METADATA_FIELDS
    patch = {k: data[k] for k in all_fields if k in data and data[k] is not None}
    if not patch:
        raise ValueError("本次没有可保存的有效字段")
    c = _conn()
    try:
        _amz_init_extended(c)
        with c:
            row = c.execute("SELECT current_data FROM amz_watch WHERE asin=?", (asin,)).fetchone()
            if not row:
                raise ValueError("ASIN 不在巡查名单中或未绑定为可采集本品")
            old = json.loads(row[0])
            changes = {k: {"old": old.get(k), "new": v}
                       for k, v in patch.items() if k in AMZ_FIELDS and old.get(k) != v} if old else {}
            status = "baseline" if not old else "changed" if changes else "unchanged"
            merged = {**old, **patch}
            if changes:
                c.execute("INSERT INTO amz_changes(asin, checked_at, changes) VALUES (?,?,?)",
                          (asin, checked_at, json.dumps(changes, ensure_ascii=False)))
            # Success/check metadata advances even when the business observation is unchanged.
            if merged != old:
                c.execute("UPDATE amz_watch SET current_data=? WHERE asin=?",
                          (json.dumps(merged, ensure_ascii=False), asin))
            c.execute("UPDATE amz_watch SET last_checked=?, last_status=?, last_error=? WHERE asin=?",
                      (checked_at, status, note[:500], asin))
            snapshot = {key: merged.get(key) for key in (
                "title", "price", "reference_price", "currency", "coupon", "promotions",
                "availability", "offer_types", "offer_evidence", "offer_observed_at", "offer_read_status")}
            snapshot.update({"market": "US", "delivery_zip": "10001", "check_status": status})
            c.execute("INSERT INTO amz_observations(asin, observed_at, data) VALUES (?,?,?)",
                      (asin, checked_at, json.dumps(snapshot, ensure_ascii=False)))
            c.execute("UPDATE amz_analyses SET is_stale=1 WHERE group_id IN "
                      "(SELECT group_id FROM amz_group_asins WHERE asin=? UNION "
                      "SELECT group_id FROM amz_group_products WHERE asin=?) AND created_at<?",
                      (asin, asin, checked_at))
        return {"asin": asin, "status": status, "changes": changes, "message": note}
    finally:
        c.close()


def amz_record_error(asin, error):
    """只覆盖检查元信息，绝不把访问失败写成价格/优惠变化。"""
    c = _conn()
    try:
        with c:
            c.execute("UPDATE amz_watch SET last_checked=?, last_status='error', last_error=? WHERE asin=?",
                      (datetime.now().astimezone().isoformat(timespec="seconds"), str(error)[:500], asin))
    finally:
        c.close()


def amz_changes(asin=None, limit=100, group_id=None):
    """不传 group_id 保持全局历史语义；传入后按该组当前成员过滤历史。"""
    c = _conn()
    try:
        args = [asin] if asin else []
        filters = ["asin=?"] if asin else []
        if group_id is not None:
            _amz_init_groups(c)
            group = _amz_group(c, group_id)
            filters.append("(asin IN (SELECT asin FROM amz_group_asins WHERE group_id=?) "
                           "OR asin=(SELECT asin FROM amz_group_products WHERE group_id=?))")
            args.extend([group["id"], group["id"]])
        where = " WHERE " + " AND ".join(filters) if filters else ""
        rows = c.execute("SELECT id, asin, checked_at, changes FROM amz_changes" + where +
                         " ORDER BY id DESC LIMIT ?", args + [max(1, min(int(limit), 200))]).fetchall()
        return [{"id": i, "asin": a, "checked_at": checked, "changes": json.loads(data)}
                for i, a, checked, data in rows]
    finally:
        c.close()


def amz_get_product(group_id):
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        row = c.execute("SELECT name, asin, spec_notes, manual_price, protected_floor, currency, price_source, updated_at "
                        "FROM amz_group_products WHERE group_id=?", (group["id"],)).fetchone()
        if not row:
            return {"group_id": group["id"], "name": "", "asin": None, "spec_notes": "",
                    "manual_price": None, "protected_floor": None, "currency": "USD",
                    "price_source": "manual", "updated_at": None}
        return {"group_id": group["id"], "name": row[0], "asin": row[1], "spec_notes": row[2],
                "manual_price": row[3], "protected_floor": row[4], "currency": row[5],
                "price_source": row[6], "updated_at": row[7]}
    finally:
        c.close()


def amz_save_product(group_id, product):
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with c:
            if product.get("price_source", "manual") == "observed" and product.get("asin"):
                # A bound own ASIN gets a shared observation row but is deliberately
                # not inserted into amz_group_asins (the competitor membership table).
                c.execute("INSERT INTO amz_watch(asin) VALUES (?) ON CONFLICT(asin) DO NOTHING", (product["asin"],))
                _amz_sync_enabled(c)
            c.execute("INSERT INTO amz_group_products(group_id, name, asin, spec_notes, manual_price, protected_floor, "
                      "currency, price_source, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(group_id) DO UPDATE SET name=excluded.name, asin=excluded.asin, "
                      "spec_notes=excluded.spec_notes, manual_price=excluded.manual_price, "
                      "protected_floor=excluded.protected_floor, currency=excluded.currency, "
                      "price_source=excluded.price_source, updated_at=excluded.updated_at",
                      (group["id"], product.get("name", ""), product.get("asin"), product.get("spec_notes", ""),
                       product.get("manual_price"), product.get("protected_floor"), product.get("currency", "USD"),
                       product.get("price_source", "manual"), now))
        return amz_get_product(group["id"])
    finally:
        c.close()


def amz_save_reference_price(group_id, manual_price):
    """Update only the simple USD benchmark, preserving legacy product metadata."""
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with c:
            existing = c.execute("SELECT 1 FROM amz_group_products WHERE group_id=?", (group["id"],)).fetchone()
            if existing:
                c.execute("UPDATE amz_group_products SET manual_price=?, currency='USD', updated_at=? WHERE group_id=?",
                          (manual_price, now, group["id"]))
            else:
                c.execute("INSERT INTO amz_group_products(group_id,name,asin,spec_notes,manual_price,protected_floor,"
                          "currency,price_source,updated_at) VALUES (?, '', NULL, '', ?, NULL, 'USD', 'manual', ?)",
                          (group["id"], manual_price, now))
        return amz_get_product(group["id"])
    finally:
        c.close()


def amz_observations(group_id=None, asin=None, limit=100, offset=0):
    c = _conn()
    try:
        _amz_init_extended(c)
        args = []
        filters = []
        if asin:
            filters.append("o.asin=?"); args.append(asin)
        if group_id is not None:
            _amz_init_groups(c)
            group = _amz_group(c, group_id)
            filters.append("(o.asin IN (SELECT asin FROM amz_group_asins WHERE group_id=?) "
                           "OR o.asin=(SELECT asin FROM amz_group_products WHERE group_id=?))")
            args.extend([group["id"], group["id"]])
        where = " WHERE " + " AND ".join(filters) if filters else ""
        total = c.execute("SELECT COUNT(*) FROM amz_observations o" + where, args).fetchone()[0]
        rows = c.execute("SELECT o.id, o.asin, o.observed_at, o.data FROM amz_observations o" + where +
                         " ORDER BY o.id DESC LIMIT ? OFFSET ?", args + [max(1, min(int(limit), 200)), max(0, int(offset))]).fetchall()
        items = [{"id": row[0], "asin": row[1], "observed_at": row[2], "data": json.loads(row[3])} for row in rows]
        return {"items": items, "total": total}
    finally:
        c.close()


def amz_save_analysis(group_id, model, kind, observation_time, input_value, output_value):
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with c:
            cursor = c.execute("INSERT INTO amz_analyses(group_id, created_at, model, kind, observation_time, input_json, output_json) "
                               "VALUES (?,?,?,?,?,?,?)",
                               (group["id"], now, str(model)[:100], str(kind)[:30], str(observation_time)[:100],
                                json.dumps(input_value, ensure_ascii=False), json.dumps(output_value, ensure_ascii=False)))
        return {"id": cursor.lastrowid, "group_id": group["id"], "created_at": now, "model": model,
                "kind": kind, "observation_time": observation_time, "input": input_value,
                "output": output_value, "is_stale": False}
    finally:
        c.close()


def amz_analyses(group_id, limit=20):
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        rows = c.execute("SELECT id, created_at, model, kind, observation_time, input_json, output_json, is_stale "
                         "FROM amz_analyses WHERE group_id=? ORDER BY id DESC LIMIT ?",
                         (group["id"], max(1, min(int(limit), 50)))).fetchall()
        return [{"id": row[0], "created_at": row[1], "model": row[2], "kind": row[3],
                 "observation_time": row[4], "input": json.loads(row[5]), "output": json.loads(row[6]),
                 "is_stale": bool(row[7])} for row in rows]
    finally:
        c.close()


def amz_get_schedule(group_id):
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        row = c.execute("SELECT enabled, local_time, timezone, updated_at FROM amz_schedules WHERE group_id=?",
                        (group["id"],)).fetchone()
        if not row:
            return {"group_id": group["id"], "enabled": False, "local_time": "08:00",
                    "timezone": "Asia/Shanghai", "updated_at": None}
        return {"group_id": group["id"], "enabled": bool(row[0]), "local_time": row[1],
                "timezone": row[2], "updated_at": row[3]}
    finally:
        c.close()


def amz_save_schedule(group_id, enabled, local_time):
    c = _conn()
    try:
        _amz_init_groups(c)
        group = _amz_group(c, group_id)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with c:
            c.execute("INSERT INTO amz_schedules(group_id, enabled, local_time, timezone, updated_at) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(group_id) DO UPDATE SET enabled=excluded.enabled, local_time=excluded.local_time, "
                      "timezone=excluded.timezone, updated_at=excluded.updated_at",
                      (group["id"], int(bool(enabled)), local_time, "Asia/Shanghai", now))
        return amz_get_schedule(group["id"])
    finally:
        c.close()


def amz_schedule_run(group_id, run_date, status, message="", job_id=None):
    c = _conn()
    try:
        _amz_init_groups(c)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with c:
            c.execute("INSERT INTO amz_schedule_runs(group_id, run_date, status, message, job_id, updated_at) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(group_id, run_date) DO UPDATE SET status=excluded.status, "
                      "message=excluded.message, job_id=excluded.job_id, updated_at=excluded.updated_at",
                      (group_id, run_date, status, str(message)[:500], job_id, now))
        return {"group_id": group_id, "run_date": run_date, "status": status, "message": str(message)[:500], "job_id": job_id}
    finally:
        c.close()


def amz_schedule_run_for(group_id, run_date):
    c = _conn()
    try:
        _amz_init_extended(c)
        row = c.execute("SELECT status, message, job_id, updated_at FROM amz_schedule_runs WHERE group_id=? AND run_date=?",
                        (group_id, run_date)).fetchone()
        return None if not row else {"group_id": group_id, "run_date": run_date, "status": row[0],
                                     "message": row[1], "job_id": row[2], "updated_at": row[3]}
    finally:
        c.close()


def amz_schedule_runs(limit=100):
    c = _conn()
    try:
        _amz_init_extended(c)
        rows = c.execute("SELECT group_id, run_date, status, message, job_id, updated_at FROM amz_schedule_runs "
                         "ORDER BY run_date DESC, group_id LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
        return [{"group_id": r[0], "run_date": r[1], "status": r[2], "message": r[3],
                 "job_id": r[4], "updated_at": r[5]} for r in rows]
    finally:
        c.close()
