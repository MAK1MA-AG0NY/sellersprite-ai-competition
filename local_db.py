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

AMZ_FIELDS = ("title", "price", "reference_price", "currency", "coupon", "promotions", "availability")


def amz_watchlist():
    c = _conn()
    try:
        rows = c.execute(
            "SELECT asin, current_data, last_checked, last_status, last_error "
            "FROM amz_watch WHERE enabled=1 ORDER BY rowid").fetchall()
        return [{"asin": a, "url": f"https://www.amazon.com/dp/{a}",
                 "current": json.loads(data), "last_checked": checked,
                 "last_status": status, "last_error": error}
                for a, data, checked, status, error in rows]
    finally:
        c.close()


def amz_save_watchlist(asins):
    """替换启用名单，不删除已移除商品的有效基线及差异历史。ASIN 由调用方校验。"""
    c = _conn()
    try:
        with c:
            c.execute("UPDATE amz_watch SET enabled=0")
            c.executemany(
                "INSERT INTO amz_watch(asin) VALUES (?) "
                "ON CONFLICT(asin) DO UPDATE SET enabled=1", [(a,) for a in asins])
    finally:
        c.close()
    return amz_watchlist()


def amz_record(asin, data, checked_at=None, note=""):
    """一次事务更新有效值与差异。None/缺失字段不覆盖；空串表示确认未展示。"""
    checked_at = checked_at or datetime.now().astimezone().isoformat(timespec="seconds")
    patch = {k: data[k] for k in AMZ_FIELDS if k in data and data[k] is not None}
    if not patch:
        raise ValueError("本次没有可保存的有效字段")
    c = _conn()
    try:
        with c:
            row = c.execute("SELECT current_data FROM amz_watch WHERE asin=?", (asin,)).fetchone()
            if not row:
                raise ValueError("ASIN 不在巡查名单中")
            old = json.loads(row[0])
            changes = {k: {"old": old.get(k), "new": v}
                       for k, v in patch.items() if old.get(k) != v} if old else {}
            status = "baseline" if not old else "changed" if changes else "unchanged"
            if changes:
                c.execute("INSERT INTO amz_changes(asin, checked_at, changes) VALUES (?,?,?)",
                          (asin, checked_at, json.dumps(changes, ensure_ascii=False)))
            if status != "unchanged":
                c.execute("UPDATE amz_watch SET current_data=? WHERE asin=?",
                          (json.dumps({**old, **patch}, ensure_ascii=False), asin))
            c.execute("UPDATE amz_watch SET last_checked=?, last_status=?, last_error=? WHERE asin=?",
                      (checked_at, status, note[:500], asin))
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


def amz_changes(asin=None, limit=100):
    c = _conn()
    try:
        args = [asin] if asin else []
        where = " WHERE asin=?" if asin else ""
        rows = c.execute("SELECT id, asin, checked_at, changes FROM amz_changes" + where +
                         " ORDER BY id DESC LIMIT ?", args + [max(1, min(int(limit), 200))]).fetchall()
        return [{"id": i, "asin": a, "checked_at": checked, "changes": json.loads(data)}
                for i, a, checked, data in rows]
    finally:
        c.close()
