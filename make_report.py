# -*- coding: utf-8 -*-
"""
美化 Excel 报告生成器
====================
输入：产品 dict 列表（competing-lookup 的 129 字段原始行）
输出：多 sheet 美化工作簿
  Sheet1 总览        精选中文列 + 条件格式（数据条/色阶）+ 超链接 + 冻结/筛选
  Sheet2 销量趋势    近 N 月销量矩阵（月 x ASIN）
  Sheet3 原始数据    全字段原样（嵌套转 JSON 字符串）
  Sheet4 说明        生成信息与官方去重声明
"""
import json
import re
from datetime import datetime, timezone, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import DataBarRule, ColorScaleRule
from openpyxl.utils import get_column_letter

CN_TZ = timezone(timedelta(hours=8))

# 精选列：(中文名, 字段, 列宽, 数字格式, 处理函数)
def _date(ms):
    return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%Y-%m-%d") if isinstance(ms, (int, float)) and ms > 0 else "-"

def _badge(r):
    parts = [r.get(k) for k in ("amazonChoice", "newRelease") if r.get(k)]
    return " / ".join(str(p) for p in parts) or "-"

def _dim(r):
    a = r.get("dimensionsTag") or r.get("dimensions") or "-"
    b = r.get("weightTag") or r.get("weight") or "-"
    return f"{a} / {b}"

CURATED = [
    ("ASIN", "asin", 13, None, None),
    ("品牌", "brand", 14, None, None),
    ("商品标题", "title", 52, None, None),
    ("价格$", "price", 10, '#,##0.00', None),
    ("评分", "rating", 7, '0.0', None),
    ("评分数", "reviews", 9, '#,##0', None),
    ("月销量(估)", "totalUnits", 11, '#,##0', None),
    ("月销售额(估)$", "totalAmount", 13, '#,##0', None),
    ("销量环比%", "totalUnitsGrowth", 10, '0.0', None),
    ("BSR排名", "bsrRank", 9, '#,##0', None),
    ("BSR类目", "bsrLabel", 22, None, None),
    ("中文类目路径", "nodeLabelPathLocale", 30, None, None),
    ("上架时间", "availableDate", 11, None, _date),
    ("上架天数", "availableDays", 9, '#,##0', None),
    ("变体数", "variations", 8, '0', None),
    ("跟卖数", "sellers", 8, '0', None),
    ("FBA费$", "fba", 9, '0.00', None),
    ("预估利润$", "profit", 10, '0.00', None),
    ("LQS", "lqs", 6, '0', None),
    ("亚马逊徽章", "_badge", 24, None, _badge),
    ("视频/A+", "_vebc", 9, None, lambda r: f"{'是' if r.get('video') == 'Y' else '否'}/{'有' if r.get('ebc') == 'Y' else '无'}"),
    ("尺寸/重量", "_dim", 28, None, _dim),
]

TH_FILL = PatternFill("solid", fgColor="FF7A1A")
TH_FONT = Font(bold=True, color="FFFFFF")
TH_BORDER = Border(bottom=Side(style="thin", color="D9D9D9"))

# 站点 → 亚马逊域名（listing 链接按任务站点生成，避免跨站 404）
MARKET_DOMAIN = {
    "US": "www.amazon.com", "CA": "www.amazon.ca", "MX": "www.amazon.com.mx",
    "BR": "www.amazon.com.br", "UK": "www.amazon.co.uk", "GB": "www.amazon.co.uk",
    "DE": "www.amazon.de", "FR": "www.amazon.fr", "IT": "www.amazon.it",
    "ES": "www.amazon.es", "JP": "www.amazon.co.jp", "AU": "www.amazon.com.au",
    "AE": "www.amazon.ae", "SG": "www.amazon.sg", "NL": "www.amazon.nl",
    "SE": "www.amazon.se", "PL": "www.amazon.pl", "TR": "www.amazon.com.tr",
    "BE": "www.amazon.com.be",
}


def market_domain(market):
    return MARKET_DOMAIN.get((market or "US").upper(), "www.amazon.com")


def _style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = TH_FILL
        cell.font = TH_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = TH_BORDER


def _parse_trend(v):
    """salesTrend 字段: JSON字符串/字符串对列表 → {月: 销量}"""
    if not v:
        return {}
    try:
        d = json.loads(v) if isinstance(v, str) else v
    except Exception:
        return {}
    if isinstance(d, list):  # [{"dk": "202509", "sales": 231}, ...]
        return {x.get("dk"): x.get("sales") for x in d if isinstance(x, dict)}
    if isinstance(d, dict):
        return d
    return {}


def build_report(rows, task_id, out_path, market="US"):
    domain = market_domain(market)
    rows = [r for r in rows if r.get("asin")]
    wb = Workbook()

    # ---------- Sheet1 总览 ----------
    ws = wb.active
    ws.title = "总览"
    headers = [c[0] for c in CURATED] + ["商品链接", "主图"]
    ws.append(headers)
    _style_header(ws, len(headers))
    for i, c in enumerate(CURATED, 1):
        ws.column_dimensions[get_column_letter(i)].width = c[2]
    ws.column_dimensions[get_column_letter(len(CURATED) + 1)].width = 12
    ws.column_dimensions[get_column_letter(len(CURATED) + 2)].width = 12
    link_font = Font(color="1a6cd8", underline="single")

    for ri, r in enumerate(rows, start=2):
        vals = []
        for _, key, _, fmt, fn in CURATED:
            v = fn(r) if fn else r.get(key)
            vals.append(v if v is not None else "-")
        ws.append(vals)
        # 链接列
        asin = str(r.get("asin"))
        lc = ws.cell(row=ri, column=len(CURATED) + 1, value="打开 ↗")
        lc.hyperlink = f"https://{domain}/dp/{asin}"
        lc.font = link_font
        ic = ws.cell(row=ri, column=len(CURATED) + 2, value="查看图")
        if r.get("bigImageUrl"):
            ic.hyperlink = r["bigImageUrl"]
        ic.font = link_font

    n = len(rows) + 1
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{n}"
    # 数字格式
    for j, (_, key, _, fmt, _) in enumerate(CURATED, 1):
        if fmt:
            for ri in range(2, n + 1):
                ws.cell(row=ri, column=j).number_format = fmt
    # 条件格式：月销量数据条、销量环比色阶
    units_col = get_column_letter([c[1] for c in CURATED].index("totalUnits") + 1)
    ws.conditional_formatting.add(
        f"{units_col}2:{units_col}{n}",
        DataBarRule(start_type="min", end_type="max", color="FF9C5B", showValue=True))
    growth_col = get_column_letter([c[1] for c in CURATED].index("totalUnitsGrowth") + 1)
    ws.conditional_formatting.add(
        f"{growth_col}2:{growth_col}{n}",
        ColorScaleRule(start_type="min", start_color="F8696B",
                       mid_type="num", mid_value=0, mid_color="FFEB84",
                       end_type="max", end_color="63BE7B"))
    # 标题换行
    tcol = [c[1] for c in CURATED].index("title") + 1
    for ri in range(2, n + 1):
        ws.cell(row=ri, column=tcol).alignment = Alignment(wrap_text=True, vertical="top")

    # ---------- Sheet2 销量趋势 ----------
    wt = wb.create_sheet("销量趋势(月)")
    trends = {str(r.get("asin")): _parse_trend(r.get("salesTrend")) for r in rows}
    months = sorted({m for t in trends.values() for m in t}, reverse=True)[:18][::-1]
    wt.append(["月份"] + [str(r.get("asin")) for r in rows])
    for m in months:
        wt.append([m] + [trends.get(str(r.get("asin")), {}).get(m, "") for r in rows])
    _style_header(wt, len(rows) + 1)
    wt.freeze_panes = "B2"
    for c in range(2, len(rows) + 2):
        wt.column_dimensions[get_column_letter(c)].width = 12

    # ---------- Sheet3 原始数据 ----------
    wo = wb.create_sheet("原始数据")
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    wo.append(keys)
    _style_header(wo, len(keys))
    for r in rows:
        wo.append([json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                   for v in (r.get(k) for k in keys)])

    # ---------- Sheet4 说明 ----------
    wi = wb.create_sheet("说明")
    notes = [
        f"生成时间: {datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S} (北京时间)",
        f"数据来源任务: {task_id or '-'}",
        f"产品行数: {len(rows)} (每行为一个产品/父体)",
        "",
        "官方说明引用: '导出时会自动对变体去重。可能导致导出的产品总数与页面显示数量不一致'",
        "→ 返回行按产品(变体组代表)去重，行数少于请求的 ASIN 数属正常现象。",
        "",
        "Sheet 总览: 精选中文字段 + 条件格式；蓝色'打开↗'可跳转亚马逊页面",
        "Sheet 销量趋势(月): 各产品近18个月销量矩阵（来自 salesTrend 字段）",
        "Sheet 原始数据: 竞品查询接口返回的全部字段，未做任何删改",
        "月销量/销售额为卖家精灵估算值，仅供参考",
    ]
    for line in notes:
        wi.append([line])
    wi.column_dimensions["A"].width = 110

    wb.save(out_path)
    return out_path


# ---------- 广告分析报告（ads-insights 关键词漏斗结果） ----------

ADS_COLS = [
    ("分层", "_tier", 9, None, None),
    ("关键词", "keyword", 30, None, None),
    ("月搜索量(最新)", "月搜索量", 13, '#,##0', None),
    ("近3月均值", "近3月搜索量均值", 11, '#,##0', None),
    ("搜索增长率", "搜索增长率", 11, '0.0%', None),
    ("月购买量", "月购买量", 10, '#,##0', None),
    ("购买率", "购买率", 9, '0.0%', None),
    ("需供比", "需供比", 8, '0.0', None),
    ("商品数", "商品数", 9, '#,##0', None),
    ("PPC价格", "PPC价格", 9, None, None),
    ("点击量", "点击量", 10, '#,##0', None),
    ("点击总占比", "点击总占比", 11, '0.0%', None),
    ("转化总占比", "转化总占比", 11, '0.0%', None),
    ("ABA排名", "ABA排名", 11, '#,##0', None),
    ("覆盖竞品数", "_coverage", 10, '0', None),
    ("综合分", "_score", 9, '0.0', None),
    ("来源", "_sources", 30, None, lambda r: ", ".join(r.get("_sources") or [])),
    ("所属类目", "所属类目", 26, None, None),
]


def _safe_sheet_name(name, used):
    """Excel Sheet 名合法化：去非法字符、限长、去重（used 为已用小写名集合）。"""
    s = re.sub(r"[\[\]:*?/\\]", "_", str(name or "")).strip() or "来源"
    s = s[:28]
    if s.lower() not in used:
        used.add(s.lower())
        return s
    i = 2
    while True:
        t = f"{s[:26]}~{i}"
        if t.lower() not in used:
            used.add(t.lower())
            return t
        i += 1


def _fill_kw_sheet(ws, rows, cols):
    """通用关键词表写入：表头+列宽+数据+格式+冻结+筛选。返回末行号。"""
    ws.append([c[0] for c in cols])
    _style_header(ws, len(cols))
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = c[2]
    for r in rows:
        vals = []
        for _, key, _, fmt, fn in cols:
            v = fn(r) if fn else r.get(key)
            vals.append(v if v is not None else "-")
        ws.append(vals)
    n = len(rows) + 1
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(n, 1)}"
    for j, (_, key, _, fmt, _) in enumerate(cols, 1):
        if fmt:
            for ri in range(2, n + 1):
                ws.cell(row=ri, column=j).number_format = fmt
        if key == "keyword":
            for ri in range(2, n + 1):
                ws.cell(row=ri, column=j).alignment = Alignment(wrap_text=True, vertical="top")
    return n


def _score_colorscale(ws, key, cols, n):
    idx = [c[1] for c in cols].index(key) + 1
    col = get_column_letter(idx)
    ws.conditional_formatting.add(
        f"{col}2:{col}{max(n, 2)}",
        ColorScaleRule(start_type="min", start_color="FFEB84",
                       end_type="max", end_color="63BE7B"))


def _fill_block_sheet(ws, blocks, cols):
    """分区域写入：每个 block 前插入加粗区域标题行，标明广告词来源。返回末行号。
    blocks: [{label, rows}]"""
    for j, c in enumerate(cols, 1):
        ws.cell(row=1, column=j, value=c[0])
    _style_header(ws, len(cols))
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = c[2]
    sec_font, sec_fill = Font(bold=True), PatternFill("solid", fgColor="EDEDED")
    ri = 1
    for b in blocks or []:
        rows = b.get("rows") or []
        ri += 1
        ws.cell(row=ri, column=1, value=f"—— 广告词来自 {b.get('label')}（{len(rows)} 词）——")
        for j in range(1, len(cols) + 1):
            cc = ws.cell(row=ri, column=j)
            cc.font, cc.fill = sec_font, sec_fill
        for r in rows:
            ri += 1
            for j, (_, key, _, fmt, fn) in enumerate(cols, 1):
                v = fn(r) if fn else r.get(key)
                cc = ws.cell(row=ri, column=j, value=v if v is not None else "-")
                if fmt:
                    cc.number_format = fmt
                if key == "keyword":
                    cc.alignment = Alignment(wrap_text=True, vertical="top")
    n = ri
    ws.freeze_panes = "C2"
    if n > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{n}"
    return n


def build_ads_report(kept, meta, out_path, page_groups=None):
    """kept: funnel 输出的聚合词行列表；meta: {'min_vol','total_kw','dropped','sources',...}
    page_groups: [{title, blocks:[{label, rows}]}] —— 分页按"我分析的ASIN"，页内按来源分区域；
    为 None 时退化为每来源一页（无任务映射时的兜底）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "总览"
    n = _fill_kw_sheet(ws, kept, ADS_COLS)
    vol_col = get_column_letter([c[1] for c in ADS_COLS].index("月搜索量") + 1)
    ws.conditional_formatting.add(
        f"{vol_col}2:{vol_col}{n}",
        DataBarRule(start_type="min", end_type="max", color="B487D8", showValue=True))
    _score_colorscale(ws, "_score", ADS_COLS, n)

    # ---------- 来源分页 ----------
    src_cols = [c for c in ADS_COLS if c[1] != "_sources"]   # 分页里"来源"列冗余，去掉
    used_names = {"总览", "说明", "搜索量趋势(月)"}
    page_sums = []
    if page_groups:
        for g in page_groups:
            blocks = g.get("blocks") or []
            w = wb.create_sheet(_safe_sheet_name(g.get("title") or "来源", used_names))
            nn = _fill_block_sheet(w, blocks, src_cols)
            _score_colorscale(w, "_score", src_cols, nn)
            page_sums.append((g.get("title"),
                              sum(len(b.get("rows") or []) for b in blocks)))
    else:
        src_counts = {}
        for r in kept:
            for s in (r.get("_sources") or []):
                src_counts[s] = src_counts.get(s, 0) + 1
        for src in sorted(src_counts, key=lambda s: -src_counts[s]):
            rows = [r for r in kept if src in (r.get("_sources") or [])]
            w = wb.create_sheet(_safe_sheet_name(src, used_names))
            nn = _fill_kw_sheet(w, rows, src_cols)
            _score_colorscale(w, "_score", src_cols, nn)
            page_sums.append((src, len(rows)))

    # Sheet 搜索量趋势矩阵（搜索量）
    wt = wb.create_sheet("搜索量趋势(月)")
    months = sorted({m for r in kept for m in (r.get("trend") or {})})[-18:]
    wt.append(["月份"] + [r["keyword"] for r in kept])
    for m in months:
        wt.append([m] + [(r.get("trend") or {}).get(m, "") for r in kept])
    _style_header(wt, len(kept) + 1)
    wt.freeze_panes = "B2"

    # Sheet 说明
    wi = wb.create_sheet("说明")
    for line in [
        f"生成时间: {datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S} (北京时间)",
        f"搜索量阈值: {meta.get('min_vol')}（最新月低于该值的词已过滤，共滤掉 {meta.get('dropped')} 个）",
        f"过滤前关键词总数: {meta.get('total_kw')}  保留: {len(kept)}",
        f"范围: {meta.get('scope') or '全部历史数据'}",
        "分页: " + "、".join(f"{t}({c}词)" for t, c in page_sums),
        "页内区域: 每个灰色区域标题标明该区块的广告词来自哪个竞品ASIN/来源",
        "分层: 核心词=≥3个来源都在投 / 共同词=2个来源 / 机会词=仅1个来源（结合总览'来源'列判断）",
        "综合分 = 月搜索量 × (1+覆盖竞品数) × min(需供比,10) / 1000，仅用于排序参考",
        "指标口径: 导入文件与⑧关键词补齐(kw_miner)为官方值（含月购买量/购买率）；⚡抓取行中 "
        "ABA排名≈searchesRank、需供比=搜索量/商品数、点击总占比=点击量/搜索量、"
        "转化总占比≈cvsShareRate、搜索增长率=trends月环比，均为估算",
        f"数据来源: {meta.get('sources') or '卖家精灵广告洞察-关键词历史导出（月粒度）'}；指标为平台估算值"
        "（⚡自动抓取部分为收割时点画像，月份=收割月）",
    ]:
        wi.append([line])
    wi.column_dimensions["A"].width = 110

    wb.save(out_path)
    return out_path


# ---------- 高相关词清单（③ 清洗结果，按来源ASIN分页） ----------

CLEAN_COLS = [
    ("分层", "分层", 9, None, None),
    ("关键词", "keyword", 30, None, None),
    ("价值分", "价值分", 9, '0.000', None),
    ("来源ASIN", "来源ASIN", 13, None, None),
    ("广告排名", "广告排名", 10, '0', None),
    ("搜索量", "searches", 11, '#,##0', None),
    ("点击量", "clicks", 10, '#,##0', None),
    ("展示量", "impressions", 11, '#,##0', None),
    ("bid", "bid", 8, '0.00', None),
    ("exactPpc", "exactPpc", 10, '0.00', None),
    ("broadPpc", "broadPpc", 10, '0.00', None),
    ("phrasePpc", "phrasePpc", 10, '0.00', None),
    ("CPR(精确)", "cprExact", 9, '0.0', None),
    ("月购买量*", "月购买量", 11, '#,##0', None),
    ("购买率*", "购买率", 9, '0.00%', None),
    ("需供比*", "需供比", 9, '0.0', None),
    ("adId", "adId", 26, None, None),
]


def build_clean_report(kept, out_path):
    """③ 高相关词清单导出：总览（价值分排序）+ 每个来源ASIN一个独立 Sheet。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "总览"
    n = _fill_kw_sheet(ws, kept, CLEAN_COLS)
    _score_colorscale(ws, "价值分", CLEAN_COLS, n)

    groups = {}
    for r in kept:
        groups.setdefault(str(r.get("来源ASIN") or "?"), []).append(r)
    used_names = set()
    for src in sorted(groups, key=lambda s: -len(groups[s])):
        w = wb.create_sheet(_safe_sheet_name(src, used_names))
        cols = [c for c in CLEAN_COLS if c[1] != "来源ASIN"]   # 分页里该列冗余
        _fill_kw_sheet(w, groups[src], cols)
        _score_colorscale(w, "价值分", cols, len(groups[src]) + 1)

    wi = wb.create_sheet("说明")
    for line in [
        f"生成时间: {datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S} (北京时间)",
        f"总词数: {len(kept)}（价值函数 v2 多因子加权：购买体量20% + 需求规模20% + 购买转化15% + "
        "广告排名15% + 广告点击10% + 市场定价10% + 供需空间10%，批内归一，缺因子按中性值0.5计）",
        "带 * 列为官方 ARA 数据（关键词挖掘⑦/②补齐 或 导入文件），已参与评分；"
        "未补齐的词按其余因子归一评分，不吃亏",
        f"来源分页: {len(groups)} 个来源ASIN各一个 Sheet（按词数降序）："
        + "、".join(f"{s}({len(v)}词)" for s, v in sorted(groups.items(), key=lambda x: -len(x[1]))),
        "分层: 品牌词=命中本地库品牌表（数据驱动标记，不剔除）；标准=其余",
        "同一关键词若多个竞品在投，会分别出现在各自的来源 Sheet 中",
    ]:
        wi.append([line])
    wi.column_dimensions["A"].width = 110

    wb.save(out_path)
    return out_path
