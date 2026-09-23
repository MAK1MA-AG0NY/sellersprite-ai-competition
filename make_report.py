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


# ---------- HTML 报告（阅读/决策视图，自包含单文件，零外部依赖） ----------

import html as _html_mod


def _esc(v):
    return _html_mod.escape(str(v if v is not None else "-"))


def _spark(trend, w=110, h=26, color="#ff7a1a"):
    """月销量迷你趋势线（SVG polyline，按自身最大值归一）。"""
    pts = [(m, v) for m, v in sorted((trend or {}).items()) if isinstance(v, (int, float))]
    vals = [v for _, v in pts][-18:]
    if len(vals) < 2:
        return ""
    hi, lo = max(vals), min(vals)
    rng = (hi - lo) or 1
    step = w / (len(vals) - 1)
    coords = " ".join("%.1f,%.1f" % (i * step, h - 2 - (v - lo) / rng * (h - 4))
                      for i, v in enumerate(vals))
    return ('<svg width="%d" height="%d"><polyline points="%s" fill="none" stroke="%s" '
            'stroke-width="1.5"/></svg>' % (w, h, coords, color))


def build_html_report(rows, task_id, out_path, market="US"):
    """美化报告 HTML 版：统计卡片 + 总体销量趋势图 + 可排序/筛选/缩略图的交互表格。
    自包含单文件（内联 CSS/JS/SVG），xlsx 保留负责原始字段与后续计算。"""
    from datetime import datetime
    domain = market_domain(market)
    rows = [r for r in rows if r.get("asin")]
    rows.sort(key=lambda r: -(r.get("totalUnits") or 0))
    now = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")

    prices = sorted(float(r["price"]) for r in rows if isinstance(r.get("price"), (int, float)))
    med_price = prices[len(prices) // 2] if prices else 0
    total_units = sum(r.get("totalUnits") or 0 for r in rows if isinstance(r.get("totalUnits"), (int, float)))
    brands = sorted({str(r.get("brand") or "-") for r in rows})
    avg_rating = (sum(r.get("rating") for r in rows if isinstance(r.get("rating"), (int, float)))
                  / max(sum(1 for r in rows if isinstance(r.get("rating"), (int, float))), 1))

    def _trend(r):
        return _parse_trend(r.get("salesTrend"))

    # 总体趋势图：月销 Top10 的绝对值折线
    top10 = rows[:10]
    all_months = sorted({m for r in top10 for m in _trend(r)})[-18:]
    CW, CH = 900, 260
    hi = max([v for r in top10 for v in _trend(r).values() if isinstance(v, (int, float))] or [1])
    lines, labels = [], []
    for i, r in enumerate(top10):
        t = _trend(r)
        pts = []
        for j, m in enumerate(all_months):
            v = t.get(m)
            if isinstance(v, (int, float)):
                x = 40 + j * ((CW - 60) / max(len(all_months) - 1, 1))
                y = CH - 24 - v / max(hi, 1) * (CH - 44)
                pts.append("%.1f,%.1f" % (x, y))
        color = "hsl(%d,65%%,45%%)" % (i * 360 / max(len(top10), 1))
        if pts:
            lines.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.6"/>' % (" ".join(pts), color))
        labels.append('<span style="color:%s">■</span> %s' % (color, _esc(r.get("asin"))))

    def _cell(v, fmt=None):
        if isinstance(v, (int, float)):
            return '<td data-v="%s">%s</td>' % (v, "{:,}".format(v) if fmt == "int" else v)
        return '<td>%s</td>' % _esc(v)

    def _fmt_int(v):
        return "{:,}".format(v) if isinstance(v, (int, float)) else "-"

    trs = []
    for r in rows:
        t = _trend(r)
        asin, brand, title = _esc(r.get("asin")), _esc(r.get("brand")), _esc(r.get("title"))
        price = r.get("price")
        rating = r.get("rating")
        units, amount = r.get("totalUnits"), r.get("totalAmount")
        growth, bsr = r.get("totalUnitsGrowth"), r.get("bsrRank")
        days, vari, lqs = r.get("availableDays"), r.get("variations"), r.get("lqs")
        url = f"https://{domain}/dp/{r.get('asin')}"
        img = (f'<img src="{_esc(r.get("bigImageUrl"))}" loading="lazy" width="52" '
               f'style="border-radius:6px">') if r.get("bigImageUrl") else "-"
        rev_html = (f' <small style="color:#8a94a6">({r.get("reviews"):,})</small>'
                    ) if isinstance(r.get("reviews"), (int, float)) else ""
        badge = " / ".join(str(r.get(k)) for k in ("amazonChoice", "newRelease") if r.get(k)) or ""
        trs.append(f'''<tr data-brand="{brand}"><td>{img}</td>'''
                  f'''<td><a href="{url}" target="_blank" style="color:#e8620a;font-weight:600">{asin}</a></td>'''
                  f'''<td>{brand}</td><td style="max-width:340px">{title}</td>'''
                  f'''<td data-v="{price or 0}">{price if price is not None else "-"}</td>'''
                  f'''<td data-v="{rating or 0}">{rating if rating is not None else "-"}{rev_html}</td>'''
                  f'''<td data-v="{units or 0}">{_fmt_int(units)}</td>'''
                  f'''<td data-v="{amount or 0}">{_fmt_int(round(amount)) if isinstance(amount, (int, float)) else "-"}</td>'''
                  f'''<td data-v="{growth or 0}">{growth if growth is not None else "-"}%</td>'''
                  f'''<td data-v="{bsr or 0}">{_fmt_int(bsr)}</td>'''
                  f'''<td data-v="{days or 0}">{days if days is not None else "-"}</td>'''
                  f'''<td data-v="{vari or 0}">{vari if vari is not None else "-"}</td>'''
                  f'''<td data-v="{lqs or 0}">{lqs if lqs is not None else "-"}</td>'''
                  f'''<td>{_spark(t)}</td><td>{_esc(badge)}</td><td>{_esc(r.get("bsrLabel"))}</td></tr>''')
    trs_html = "\n".join(trs)

    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>竞品分析报告 __TASKID__</title>
<style>
 body { font-family:"Segoe UI","Microsoft YaHei",sans-serif; background:#f5f6fa; color:#26303e; margin:0; }
 header { background:linear-gradient(120deg,#ff8c2e,#f05a00); color:#fff; padding:20px 30px; }
 header h1 { font-size:20px; margin:0; } header p { opacity:.85; font-size:12.5px; margin:6px 0 0; }
 .wrap { max-width:1220px; margin:0 auto; padding:20px; }
 .cards { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:18px; }
 .card { background:#fff; border-radius:12px; padding:14px 20px; border:1px solid #e8ebf1;
         box-shadow:0 1px 4px rgba(38,48,62,.06); min-width:150px; }
 .card b { font-size:20px; display:block; } .card span { color:#8a94a6; font-size:12px; }
 .panel { background:#fff; border-radius:14px; padding:18px 20px; border:1px solid #e8ebf1; margin-bottom:18px; }
 .panel h2 { font-size:15px; margin:0 0 12px; }
 .legend { font-size:12px; color:#556; line-height:1.9; }
 .ctl { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
 input, select { padding:7px 10px; border:1px solid #e8ebf1; border-radius:8px; font-size:13px; }
 table { width:100%; border-collapse:collapse; font-size:12.5px; }
 th { background:#ff7a1a; color:#fff; padding:8px 9px; cursor:pointer; user-select:none; white-space:nowrap; }
 th:hover { background:#e8620a; }
 td { padding:7px 9px; border-bottom:1px solid #eef0f5; vertical-align:middle; }
 tr:hover td { background:#fff7ef; }
 .note { color:#8a94a6; font-size:12px; margin-top:10px; }
</style></head><body>
<header><h1>竞品分析报告 · __TASKID__</h1>
<p>生成时间 __NOW__ (北京时间) · 站点 __MKT__ · 数据来源：卖家精灵竞品查询 · 月销/销售额为平台估算值</p></header>
<div class="wrap">
 <div class="cards">
  <div class="card"><b>__NCARDS__ 个</b><span>竞品 ASIN</span></div>
  <div class="card"><b>__TUNITS__</b><span>合计月销量(估)</span></div>
  <div class="card"><b>$__MED__</b><span>价格中位数</span></div>
  <div class="card"><b>__NRAND__ 个</b><span>品牌数</span></div>
  <div class="card"><b>__RATING__</b><span>平均评分</span></div>
 </div>
 <div class="panel"><h2>销量趋势 · 月销 Top10（绝对值）</h2>
  <svg width="100%" viewBox="0 0 __CW__ __CH__" preserveAspectRatio="xMidYMid meet">__LINES__</svg>
  <div class="legend">__LABELS__</div></div>
 <div class="panel"><h2>竞品明细（点击表头排序）</h2>
  <div class="ctl">
   <input id="q" placeholder="搜索 ASIN / 标题 / 品牌…" style="flex:1;min-width:200px">
   <select id="bf"><option value="">全部品牌</option>__BOPT__</select>
  </div>
  <div style="overflow-x:auto;"><table id="t"><thead><tr>
   <th>图</th><th data-k="1" data-n="0">ASIN</th><th data-k="2" data-n="0">品牌</th><th>标题</th>
   <th data-k="4" data-n="1">价格$</th><th data-k="5" data-n="1">评分</th>
   <th data-k="6" data-n="1">月销量↓</th><th data-k="7" data-n="1">月销额$</th>
   <th data-k="8" data-n="1">环比%</th><th data-k="9" data-n="1">BSR</th>
   <th data-k="10" data-n="1">上架天数</th><th data-k="11" data-n="1">变体</th>
   <th data-k="12" data-n="1">LQS</th><th>趋势</th><th>徽章</th><th>BSR类目</th>
  </tr></thead><tbody>
__TRS__
  </tbody></table></div>
  <div class="note">129 字段原始数据请用 xlsx 版（⑤ Excel 报告）；价格/销量为卖家精灵估算值。逐 ASIN 趋势线见"趋势"列（近18月，按自身幅度归一）。</div>
 </div>
</div>
<script>
var tb = document.getElementById('t').tBodies[0];
function sortBy(ci, numeric) {
  var rows = Array.prototype.slice.call(tb.rows);
  var dir = tb.getAttribute('data-dir-' + ci) === '1' ? -1 : 1;
  tb.setAttribute('data-dir-' + ci, dir === 1 ? '1' : '0');
  rows.sort(function(a, b) {
    var x = a.cells[ci], y = b.cells[ci];
    if (numeric) { x = parseFloat(x.getAttribute('data-v')) || 0; y = parseFloat(y.getAttribute('data-v')) || 0; }
    else { x = x.textContent.trim(); y = y.textContent.trim(); }
    return (x > y ? 1 : x < y ? -1 : 0) * dir;
  });
  rows.forEach(function(r) { tb.appendChild(r); });
}
Array.prototype.forEach.call(document.querySelectorAll('th[data-k]'), function(th) {
  th.addEventListener('click', function() { sortBy(+th.dataset.k, th.dataset.n === '1'); });
});
function applyFilter() {
  var q = document.getElementById('q').value.toLowerCase();
  var b = document.getElementById('bf').value;
  Array.prototype.forEach.call(tb.rows, function(r) {
    r.style.display = (r.textContent.toLowerCase().indexOf(q) >= 0 &&
                       (!b || r.getAttribute('data-brand') === b)) ? '' : 'none';
  });
}
document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('bf').addEventListener('change', applyFilter);
</script></body></html>"""

    page = (page.replace("__TASKID__", _esc(task_id or "-"))
                .replace("__NOW__", now)
                .replace("__MKT__", _esc(market))
                .replace("__NCARDS__", str(len(rows)))
                .replace("__TUNITS__", "{:,}".format(total_units))
                .replace("__MED__", "%.2f" % med_price)
                .replace("__NRAND__", str(len(brands)))
                .replace("__RATING__", "%.2f" % avg_rating)
                .replace("__CW__", str(CW)).replace("__CH__", str(CH))
                .replace("__LINES__", "\n".join(lines))
                .replace("__LABELS__", " ".join(labels))
                .replace("__BOPT__", "".join("<option>%s</option>" % _esc(b) for b in brands))
                .replace("__TRS__", trs_html))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path


# ---------- AI 分析包（Markdown，紧凑上下文，供外部 AI 阅读） ----------

def build_ai_pack(rows, kw_rows, out_path, scope_label="ALL",
                  top_products=200, top_keywords=150):
    """生成预聚合的 Markdown 数据包：
    统计摘要 + 产品层(按月销Top N) + 关键词层(标准词按综合分Top M) + 品牌聚合。
    全文控制在 ~1-3 万 token，可直接粘贴给外部 AI。"""
    from datetime import datetime
    from collections import defaultdict
    rows = sorted([r for r in rows if r.get("asin")],
                  key=lambda r: -(r.get("totalUnits") or 0))
    prod_rows = rows[:max(top_products, 1)]

    lines = []
    L = lines.append
    L("# 竞品分析数据包（供 AI 阅读）")
    L("")
    L(f"- 生成时间: {datetime.now(CN_TZ):%Y-%m-%d %H:%M} (北京时间)")
    L(f"- 数据范围: {scope_label} · 产品层取月销量 Top {len(prod_rows)}（共 {len(rows)} 个 ASIN）")
    L("- 数据来源: 卖家精灵（竞品查询快照 + 广告关键词 + 关键词挖掘官方 ARA 数据），月销为平台估算值")
    L("- ⚠️ 关键词层数据口径【务必遵守】：月搜索量/月购买量/购买率/需供比/点击量 均为"
      "**整个关键词的市场级数据（ARA 口径）**，不代表任何竞品 ASIN 在该词上的点击率/转化率/销量；"
      "竞品 ASIN 级信号只有两个：①覆盖竞品数=哪些竞品投了这个词（词出现在其广告组），"
      "②广告排名=该竞品广告在该词下的搜索位次。AI 分析时不得把购买率/点击量归因到具体竞品的商品表现")
    L("")

    # 一、统计摘要
    prices = sorted(float(r["price"]) for r in rows if isinstance(r.get("price"), (int, float)))
    total_units = sum(r.get("totalUnits") or 0 for r in rows if isinstance(r.get("totalUnits"), (int, float)))
    tiers = defaultdict(int)
    for k in kw_rows:
        tiers[k.get("_tier") or "?"] += 1
    L("## 一、统计摘要")
    L("")
    L(f"- ASIN 总数 {len(rows)}，合计月销量(估) {total_units:,}")
    if prices:
        L(f"- 价格: 中位 ${prices[len(prices)//2]:.2f} / 最低 ${prices[0]:.2f} / 最高 ${prices[-1]:.2f}")
    L(f"- 品牌数 {len({str(r.get('brand') or '-') for r in rows})}")
    L(f"- 关键词漏斗: {', '.join(f'{k} {v}个' for k, v in sorted(tiers.items()))}")
    L("")

    # 二、产品层
    L(f"## 二、产品层（月销 Top {len(prod_rows)}，按月销量降序）")
    L("")
    L("| ASIN | 品牌 | 价格$ | 评分 | 评分数 | 月销量 | 月销额$ | 环比% | BSR | 上架天数 | 变体 | LQS |")
    L("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in prod_rows:
        price = r.get("price")
        price = f"{price:.2f}" if isinstance(price, (int, float)) else "-"
        rating = r.get("rating")
        rating = f"{rating:.1f}" if isinstance(rating, (int, float)) else "-"
        units = "{:,}".format(r.get("totalUnits")) if isinstance(r.get("totalUnits"), (int, float)) else "-"
        amount = "{:,.0f}".format(r.get("totalAmount")) if isinstance(r.get("totalAmount"), (int, float)) else "-"
        growth = r.get("totalUnitsGrowth")
        growth = f"{growth:+.1f}%" if isinstance(growth, (int, float)) else "-"
        bsr = "{:,}".format(r.get("bsrRank")) if isinstance(r.get("bsrRank"), (int, float)) else "-"
        title_short = str(r.get("title") or "")[:60].replace("|", "/")
        L(f"| {r.get('asin')} | {r.get('brand') or '-'} | {price} | {rating} | "
          f"{r.get('reviews') or '-'} | {units} | {amount} | {growth} | {bsr} | "
          f"{r.get('availableDays') or '-'} | {r.get('variations') or '-'} | {r.get('lqs') or '-'} |")
        L(f"  （{title_short}）")

    # 三、关键词层
    std = [k for k in kw_rows if k.get("_tier") != "品牌词"]
    std.sort(key=lambda k: -(k.get("_score") or 0))
    std = std[:max(top_keywords, 1)]
    L("")
    L(f"## 三、关键词层（标准词，按综合分 Top {len(std)}；品牌词仅列名）")
    L("")
    L("| 关键词 | 分层 | 月搜索量(词级) | 覆盖竞品数(ASIN级) | 综合分 | 购买率(词级) | "
      "月购买量(词级) | 需供比(词级) | PPC$(词级) | 最佳广告排名(ASIN级) |")
    L("|---|---|---|---|---|---|---|---|---|---|")
    for k in std:
        pr = k.get("购买率")
        pr = f"{pr*100:.2f}%" if isinstance(pr, (int, float)) else "-"
        pb = "{:,}".format(int(k["月购买量"])) if isinstance(k.get("月购买量"), (int, float)) else "-"
        sd = f"{k.get('需供比'):.1f}" if isinstance(k.get("需供比"), (int, float)) else "-"
        ppc = k.get("PPC价格")
        ppc = f"{ppc:.2f}" if isinstance(ppc, (int, float)) else "-"
        rank = k.get("广告排名")
        rank = str(int(rank)) if isinstance(rank, (int, float)) else "-"
        L(f"| {k.get('keyword')} | {k.get('_tier')} | {k.get('月搜索量') or '-'} | "
          f"{k.get('_coverage')} | {k.get('_score')} | {pr} | {pb} | {sd} | {ppc} | {rank} |")
    brand_kws = sorted({k.get("keyword") for k in kw_rows if k.get("_tier") == "品牌词"})
    if brand_kws:
        L("")
        L(f"品牌词（{len(brand_kws)} 个，已单独分层）: " + "、".join(brand_kws[:80])
          + ("…" if len(brand_kws) > 80 else ""))

    # 四、品牌聚合
    agg = defaultdict(lambda: [0, 0, []])
    for r in rows:
        b = str(r.get("brand") or "-")
        agg[b][0] += 1
        if isinstance(r.get("totalUnits"), (int, float)):
            agg[b][1] += r["totalUnits"]
        if isinstance(r.get("price"), (int, float)):
            agg[b][2].append(r["price"])
    top_brands = sorted(agg.items(), key=lambda x: -x[1][1])[:15]
    L("")
    L("## 四、品牌聚合（按总月销量 Top 15）")
    L("")
    L("| 品牌 | 产品数 | 合计月销量 | 价格区间$ |")
    L("|---|---|---|---|")
    for b, (n, u, ps) in top_brands:
        pr = f"${min(ps):.0f}-{max(ps):.0f}" if ps else "-"
        L(f"| {b} | {n} | {u:,} | {pr} |")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


# ---------- 广告分析报告（ads-insights 关键词漏斗结果） ----------

ADS_COLS = [
    ("分层", "_tier", 9, None, None),
    ("关键词", "keyword", 30, None, None),
    ("月搜索量(词级)", "月搜索量", 13, '#,##0', None),
    ("近3月均值(词级)", "近3月搜索量均值", 11, '#,##0', None),
    ("搜索增长率(词级)", "搜索增长率", 11, '0.0%', None),
    ("月购买量(词级)", "月购买量", 10, '#,##0', None),
    ("购买率(词级)", "购买率", 9, '0.0%', None),
    ("需供比(词级)", "需供比", 8, '0.0', None),
    ("商品数(词级)", "商品数", 9, '#,##0', None),
    ("PPC价格(词级)", "PPC价格", 9, None, None),
    ("点击量(词级)", "点击量", 10, '#,##0', None),
    ("点击总占比(词级)", "点击总占比", 11, '0.0%', None),
    ("转化总占比(词级)", "转化总占比", 11, '0.0%', None),
    ("ABA排名(词级)", "ABA排名", 11, '#,##0', None),
    ("覆盖竞品数(ASIN级)", "_coverage", 10, '0', None),
    ("广告排名(ASIN级)", "广告排名", 12, '0', None),
    ("各竞品广告排名(ASIN级)", "_ranks", 32, None,
     lambda r: "; ".join(f"{k}:{v}" for k, v in
                         sorted((r.get("_ranks") or {}).items(), key=lambda x: x[1]))),
    ("综合分", "_score", 9, '0.0', None),
    ("来源(ASIN级)", "_sources", 30, None, lambda r: ", ".join(r.get("_sources") or [])),
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
        "⚠️ 数据口径【重要】：标注'词级'的列 = 整个关键词的市场级数据（卖家精灵关键词挖掘/ARA 口径），"
        "与来源ASIN无关——不可解读为某竞品在该词上的点击率/转化率/销量；"
        "补齐的官方指标（月购买量/购买率/需供比等）同样是词级市场数据",
        "ASIN级列【仅4个】: 覆盖竞品数/来源 = 哪些竞品投了这个词（词出现在其广告组）；"
        "广告排名(ASIN级) = 最佳竞品广告在该词下的搜索位次；各竞品广告排名 = 逐竞品明细（格式 ASIN:位次）",
        "指标口径: 导入文件与⑧关键词补齐(kw_miner)为官方值（含月购买量/购买率）；⚡抓取行中 "
        "ABA排名≈searchesRank、需供比=搜索量/商品数、点击总占比=点击量/搜索量、"
        "转化总占比≈cvsShareRate、搜索增长率=trends月环比，均为估算；"
        "⚡抓取的搜索量/点击量为收割周期快照（约周级），数值≠官方月度值，勿直接对比",
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
    ("来源ASIN(ASIN级)", "来源ASIN", 13, None, None),
    ("广告排名(ASIN级)", "广告排名", 10, '0', None),
    ("搜索量(词级)", "searches", 11, '#,##0', None),
    ("点击量(词级)", "clicks", 10, '#,##0', None),
    ("展示量(词级)", "impressions", 11, '#,##0', None),
    ("bid(词级)", "bid", 8, '0.00', None),
    ("exactPpc(词级)", "exactPpc", 10, '0.00', None),
    ("broadPpc(词级)", "broadPpc", 10, '0.00', None),
    ("phrasePpc(词级)", "phrasePpc", 10, '0.00', None),
    ("CPR(精确)", "cprExact", 9, '0.0', None),
    ("月购买量*(词级)", "月购买量", 11, '#,##0', None),
    ("购买率*(词级)", "购买率", 9, '0.00%', None),
    ("需供比*(词级)", "需供比", 9, '0.0', None),
    ("adId(ASIN级)", "adId", 26, None, None),
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
        "广告排名(ASIN)15% + 市场点击热度10% + 市场定价10% + 供需空间10%，批内归一，缺因子按中性值0.5计）",
        "⚠️ 数据口径【重要】：搜索量/点击量/展示量/月购买量*/购买率*/需供比* 均为【词级市场数据】"
        "（ARA/关键词挖掘口径），与来源ASIN无关——不可解读为该竞品在该词上的点击率/转化率；"
        "ASIN级信号仅：来源ASIN（该竞品投了这个词）+ 广告排名（该竞品广告在该词的位次）+ adId",
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
