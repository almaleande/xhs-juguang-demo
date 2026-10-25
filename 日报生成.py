#!/usr/bin/env python3
"""从快照生成日报 - 秒开不调API"""
import json, os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.paths import SNAPSHOT_DIR, BASE_DIR as WORK_DIR

try:
    from core import guard as _guard
except Exception:
    _guard = None

date_arg = sys.argv[1] if len(sys.argv) > 1 else None
snapshot_file = f"{SNAPSHOT_DIR}/{date_arg}.json" if date_arg else f"{SNAPSHOT_DIR}/最新.json"
if not os.path.exists(snapshot_file):
    print(f"\u274c \u6ca1\u6709\u5feb\u7167: {snapshot_file}", file=sys.stderr)
    sys.exit(1)

with open(snapshot_file, encoding='utf-8') as f:
    snap = json.load(f)

target_date = snap["date"]
s = snap["summary"]

# 推送文本
today = datetime.now().strftime("%m\u6708%d\u65e5")
weekdays = ["\u5468\u4e00", "\u5468\u4e8c", "\u5468\u4e09", "\u5468\u56db", "\u5468\u4e94", "\u5468\u516d", "\u5468\u65e5"]
wd = weekdays[int(datetime.now().strftime("%w"))]

lines = [f"\U0001f4ca \u5c0f\u7ea2\u4e66\u805a\u5149\u65e5\u62a5", f"\U0001f4c5 {today} ({wd}) | {target_date}\u6570\u636e", "\u2501"*22]
sc = sorted(s["cities"].items(), key=lambda x: x[1]["fee"], reverse=True)
for city, cd in sc:
    bar = "\u2588" * max(1, int(cd["fee"] / max(1, sc[0][1]["fee"]) * 12))
    lines.append(f"{city:4s} \u00a5{cd['fee']:>5,.0f} {bar} \u79c1\u4fe1{cd['msg']}")
lines.append(f"\u2501"*22)
lines.append(f"\U0001f4b0 \u603b\u6d88\u8017: \u00a5{s['total_fee']:,.2f} | \U0001f4ac \u79c1\u4fe1: {s['total_msg']} | \u7559\u8d44: {s['total_msg_leads']}")

# 数据质量告警：区分「真没投放」与「拉取失败」
# 旧条件写成「有投放户 且 城市数为 0」，而实际异常场景是「有户、有城市、值全 0」，恰好绕过告警
_issues = []
if _guard is not None:
    try:
        _issues = _guard.check(snap, _guard.load_previous(target_date))
    except Exception:
        _issues = []
if _issues:
    lines.append("")
    for _i in _issues:
        _icon = {"fatal": "\u26d4", "warn": "\u26a0\ufe0f", "info": "\u00b7"}.get(_i["level"], "\u00b7")
        lines.append(f"{_icon} {_i['msg']}")

# 聚合创意数据
all_creatives = []
for aid, adv in snap["advertisers"].items():
    city = adv["city"]
    for c in adv["creatives"]:
        c["_city"] = city
        all_creatives.append(c)

sorted_cr = sorted(all_creatives, key=lambda x: float(x.get('fee', 0) or 0), reverse=True)
creative_count = len(all_creatives)

# 全局汇总指标
total_imp = sum(int(c.get('impression',0) or 0) for c in all_creatives)
total_clk = sum(int(c.get('click',0) or 0) for c in all_creatives)
total_inter = sum(int(c.get('like',0)or 0)+int(c.get('comment',0)or 0)+int(c.get('collect',0)or 0)+int(c.get('share',0)or 0)+int(c.get('follow',0)or 0) for c in all_creatives)
top_ctr = round(total_clk/total_imp*100, 1) if total_imp > 0 else 0
top_ir = round(total_inter/total_imp*100, 1) if total_imp > 0 else 0
top_mlr = round(s['total_msg_leads']/s['total_msg']*100, 1) if s['total_msg'] > 0 else 0
top_mlc = round(s['total_fee']/s['total_msg_leads'], 0) if s['total_msg_leads'] > 0 else 0
yen = "\u00a5"
top_mlc_str = f"{yen}{top_mlc:.0f}" if top_mlc > 0 else "-"

if sorted_cr:
    lines.append(f"\n\U0001f3c6 \u521b\u610fTOP5")
    for i, c in enumerate(sorted_cr[:5], 1):
        name = (c.get('creativity_name', '') or '')[:12]
        fee = float(c.get('fee', 0) or 0)
        msg = int(c.get('message_consult', 0) or 0)
        ml = int(c.get('msg_leads_num', 0) or 0)
        lines.append(f"  #{i} {name:12s} \u00a5{fee:>5,.0f} \u79c1\u4fe1{msg} \u7559\u8d44{ml}")

    warns = [c for c in sorted_cr if float(c.get('fee',0) or 0) > 100 and int(c.get('message_consult',0) or 0) < 5][:3]
    if warns:
        lines.append(f"\n\u26a0\ufe0f \u9ad8\u6d88\u8017\u4f4e\u8f6c\u5316:")
        for c in warns:
            lines.append(f"  {(c.get('creativity_name','') or '')[:10]:10s} \u00a5{float(c.get('fee',0) or 0):.0f}/\u79c1\u4fe1{int(c.get('message_consult',0) or 0)}")

    goods = [c for c in sorted_cr if float(c.get('fee',0) or 0) < 200 and int(c.get('message_consult',0) or 0) >= 1][:3]
    if goods:
        lines.append(f"\n\U0001f4a1 \u9ad8\u6027\u4ef7\u6bd4:")
        for c in goods:
            lines.append(f"  {(c.get('creativity_name','') or '')[:10]:10s} \u00a5{float(c.get('fee',0) or 0):.0f}/\u79c1\u4fe1{int(c.get('message_consult',0) or 0)}")

push_text = "\n".join(lines)
print(push_text)

# ====== HTML ======
city_names = sorted(set(c["_city"] for c in all_creatives if c["_city"]))

# 计算各城市创意数
city_counts = {}
for c in all_creatives:
    city_counts[c["_city"]] = city_counts.get(c["_city"], 0) + 1

city_btns = '<button class="f-btn active" data-city="all">\u5168\u90e8 <span class="badge">{}</span></button>'.format(len(all_creatives))
for city in sorted(sc, key=lambda x: x[1]["fee"], reverse=True):
    cn = city[0]
    count = city_counts.get(cn, 0)
    city_btns += f'<button class="f-btn" data-city="{cn}">{cn} <span class="badge">{count}</span></button>'

# 构建城市详情表
city_details = {}
for c in all_creatives:
    c_city = c['_city']
    if c_city not in city_details:
        city_details[c_city] = {"fee": 0, "impression": 0, "interaction": 0, "msg": 0, "ml": 0, "click": 0}
    cd = city_details[c_city]
    cd["fee"] += float(c.get('fee',0) or 0)
    cd["impression"] += int(c.get('impression',0) or 0)
    cd["interaction"] += int(c.get('like',0)or 0)+int(c.get('comment',0)or 0)+int(c.get('collect',0)or 0)+int(c.get('share',0)or 0)+int(c.get('follow',0)or 0)
    cd["msg"] += int(c.get('message_consult',0) or 0)
    cd["ml"] += int(c.get('msg_leads_num',0) or 0)
    cd["click"] += int(c.get('click',0) or 0)

# 构建账户详情表
acct_details = []
for aid, adv in snap["advertisers"].items():
    acct_fee = 0; acct_imp = 0; acct_clk = 0; acct_inter = 0; acct_msg = 0; acct_ml = 0
    for c in adv.get("creatives", []):
        acct_fee += float(c.get('fee',0) or 0)
        acct_imp += int(c.get('impression',0) or 0)
        acct_clk += int(c.get('click',0) or 0)
        acct_inter += int(c.get('like',0)or 0)+int(c.get('comment',0)or 0)+int(c.get('collect',0)or 0)+int(c.get('share',0)or 0)+int(c.get('follow',0)or 0)
        acct_msg += int(c.get('message_consult',0) or 0)
        acct_ml += int(c.get('msg_leads_num',0) or 0)
    # 所有账户都展示，消耗为0也保留
    acct_details.append({
        "name": adv.get("name", f"广告主{aid}"),
        "city": adv.get("city", "其他"),
        "fee": acct_fee, "imp": acct_imp, "clk": acct_clk,
        "inter": acct_inter, "msg": acct_msg, "ml": acct_ml
    })

acct_details.sort(key=lambda x: x["fee"], reverse=True)
acct_table_rows = ""
for a in acct_details:
    ctr = round(a["clk"]/a["imp"]*100, 1) if a["imp"] > 0 else 0
    ir = round(a["inter"]/a["imp"]*100, 1) if a["imp"] > 0 else 0
    mr = round(a["ml"]/a["msg"]*100, 1) if a["msg"] > 0 else 0
    mc = round(a["fee"]/a["msg"], 0) if a["msg"] > 0 else 0
    lc = round(a["fee"]/a["ml"], 0) if a["ml"] > 0 else 0
    yen = "\u00a5"
    msg_cost_cell = f"{yen}{mc:.0f}" if a["msg"] > 0 else "-"
    lead_cost_cell = f"{yen}{lc:.0f}" if a["ml"] > 0 else "-"
    acct_table_rows += f"<tr data-city=\"{a['city']}\"><td class=\"name-col\" title=\"{a['name']}\">{a['name'][:22]}</td><td>{a['city']}</td><td class=\"num\">{yen}{a['fee']:,.0f}</td><td class=\"num\">{a['clk']:,}</td><td class=\"num\">{a['inter']:,}</td><td class=\"num\">{a['msg']}</td><td class=\"num\">{a['ml']}</td><td class=\"num\">{ctr}%</td><td class=\"num\">{ir}%</td><td class=\"num\">{mr}%</td><td class=\"num\">{msg_cost_cell}</td><td class=\"num\">{lead_cost_cell}</td></tr>"

city_table_rows = ""
for c_name, c_data in sorted(city_details.items(), key=lambda x: x[1]["fee"], reverse=True):
    fee = c_data["fee"]
    imp = c_data["impression"]
    clk = c_data["click"]
    inter = c_data["interaction"]
    msg = c_data["msg"]
    ml = c_data["ml"]
    ctr = round(clk/imp*100, 1) if imp > 0 else 0
    inter_rate = round(inter/imp*100, 1) if imp > 0 else 0
    ml_rate = round(ml/msg*100, 1) if msg > 0 else 0
    msg_cost = round(fee/msg, 0) if msg > 0 else 0
    ml_cost = round(fee/ml, 0) if ml > 0 else 0
    msg_cost_cell = f"{yen}{msg_cost:.0f}" if msg > 0 else "-"
    ml_cost_cell = f"{yen}{ml_cost:.0f}" if ml > 0 else "-"
    city_table_rows += f"<tr><td><strong>{c_name}</strong></td><td class=\"num\">{yen}{fee:,.0f}</td><td class=\"num\">{clk:,}</td><td class=\"num\">{inter:,}</td><td class=\"num\">{msg}</td><td class=\"num\">{ml}</td><td class=\"num\">{ctr}%</td><td class=\"num\">{inter_rate}%</td><td class=\"num\">{ml_rate}%</td><td class=\"num\">{msg_cost_cell}</td><td class=\"num\">{ml_cost_cell}</td></tr>"

city_rows = ""
for rank, (city, cd) in enumerate(sc, 1):
    city_rows += f"<tr><td class=\"rank\">{rank}</td><td><strong>{city}</strong></td><td class=\"num\">\u00a5{cd['fee']:,.0f}</td><td class=\"num\">{cd['msg']}</td><td class=\"num\">{cd['msg_leads']}</td><td class=\"num\">{cd['creative_count']}</td></tr>"

def calc_ir(c):
    imp = int(c.get('impression', 0) or 0)
    inter = int(c.get('like',0)or 0)+int(c.get('comment',0)or 0)+int(c.get('collect',0)or 0)+int(c.get('share',0)or 0)+int(c.get('follow',0)or 0)
    return round(inter/imp*100, 1) if imp > 0 else 0

cr_rows = ""
for i, c in enumerate(sorted_cr, 1):
    fee = float(c.get('fee',0) or 0)
    msg = int(c.get('message_consult',0) or 0)
    ml = int(c.get('msg_leads_num',0) or 0)
    imp = int(c.get('impression',0) or 0)
    clk = int(c.get('click',0) or 0)
    ctr = c.get('ctr', '0%')
    mc = round(fee/msg, 0) if msg > 0 else 0
    ri = calc_ir(c)
    score = round(min(fee/100,100)*0.3 + min(msg*5,100)*0.35 + min(ml*10,100)*0.25 + min(ri*20,100)*0.1, 1)
    city = c.get('_city', '')

    cover = ""
    if c.get('creativity_image') and c.get('note_id'):
        cover = f"""<div class="cover-wrap">
  <img class="cover-img" src="{c['creativity_image']}" onerror="this.style.display='none'"
       data-full="{c['creativity_image']}">
  <div class="cover-preview"><img src="{c['creativity_image']}"></div>
</div>
<span class="note-link" title="演示素材，不提供原文链接">演示</span>"""
    else:
        cover = "-"

    if mc>0 and mc<50:
        eff = '<span class="tag tag-green">\U0001f7e2 \u4f18</span>'
        row_class = "row-good"
    elif mc>200:
        eff = '<span class="tag tag-red">\U0001f534 \u5dee</span>'
        row_class = "row-bad"
    else:
        eff = '<span class="tag tag-yellow">\U0001f7e1 \u4e2d</span>' if msg>0 else '<span class="tag tag-gray">⚪ -</span>'
        row_class = ""

    name = (c.get('creativity_name','') or '')[:25]
    cr_rows += f"""<tr data-city="{city}" class="{row_class}">
  <td class="rank">{i}</td>
  <td class="cover-cell">{cover}</td>
  <td class="name-col" title="{name}">{name}</td>
  <td>{city}</td>
  <td class="num">\u00a5{fee:,.0f}</td>
  <td class="num">{imp:,}</td>
  <td class="num">{clk:,}</td>
  <td class="num">{ctr}</td>
  <td class="num highlight">{msg}</td>
  <td class="num">{'%.0f'%mc if mc>0 else '-'}</td>
  <td class="num">{ml}</td>
  <td class="num">{ri}%</td>
  <td class="num score">{score}</td>
  <td>{eff}</td>
</tr>"""

warn_r = ""
for c in sorted_cr:
    if float(c.get('fee',0)or 0)>100 and int(c.get('message_consult',0)or 0)<5:
        warn_r += f"<tr><td>{(c.get('creativity_name','') or '')[:20]}</td><td class=\"num\">\u00a5{float(c.get('fee',0)or 0):,.0f}</td><td class=\"num warn\">{int(c.get('message_consult',0)or 0)}</td><td>{c.get('_city','') or ''}</td></tr>"
        if len(warn_r) > 500: break

good_r = ""
for c in sorted_cr:
    if float(c.get('fee',0)or 0)<200 and int(c.get('message_consult',0)or 0)>=1:
        good_r += f"<tr><td>{(c.get('creativity_name','') or '')[:20]}</td><td class=\"num\">\u00a5{float(c.get('fee',0)or 0):,.0f}</td><td class=\"num good\">{int(c.get('message_consult',0)or 0)}</td><td>{c.get('_city','') or ''}</td></tr>"
        if len(good_r) > 500: break

maxf = sc[0][1]["fee"] if sc else 1
bars = "".join(f'<div class="bar-row"><span class="bar-label">{c}</span><div class="bar" style="width:{d["fee"]/maxf*100:.0f}%"><span class="bar-val">\u00a5{d["fee"]:,.0f}</span></div></div>' for c, d in sc)

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>\u5c0f\u7ea2\u4e66\u805a\u5149\u65e5\u62a5 {target_date}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f0f2f5;padding:24px;color:#1a1a2e;}}
.container{{max-width:1500px;margin:0 auto;}}

/* Header */
.header h1{{font-size:24px;color:#1a1a2e;}}
.header .sub{{color:#888;font-size:14px;margin-top:4px;}}

/* Summary cards */
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:20px;}}
.card{{background:white;border-radius:16px;padding:18px;box-shadow:0 2px 12px rgba(0,0,0,0.06);text-align:center;}}
.card .num{{font-size:24px;font-weight:800;color:#1a1a2e;text-align:center;}}
.card .label{{font-size:12px;color:#999;margin-top:4px;}}
.card.accent{{background:linear-gradient(135deg,#e74c3c,#c0392b);color:white;}}
.card.accent .num,.card.accent .label{{color:white;}}

/* Bar chart */
.bar-row{{display:flex;align-items:center;margin-bottom:4px;gap:8px;}}
.bar-label{{width:70px;font-size:13px;font-weight:600;text-align:right;flex-shrink:0;}}
.bar{{height:24px;border-radius:12px;background:linear-gradient(90deg,#e74c3c,#ff6b6b);min-width:4px;display:flex;align-items:center;justify-content:flex-end;padding-right:8px;}}
.bar .bar-val{{color:white;font-size:11px;font-weight:600;}}

/* Section */
.section{{background:white;border-radius:16px;padding:24px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,0.06);}}
.section h2{{font-size:16px;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid #f0f0f0;}}

/* City filter buttons */
.filter-bar{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;}}
.f-btn{{padding:4px 12px;border-radius:14px;border:1px solid #ddd;background:white;font-size:12px;cursor:pointer;color:#555;transition:all .15s;}}
.f-btn:hover{{border-color:#e74c3c;color:#e74c3c;}}
.f-btn.active{{background:#e74c3c;color:white;border-color:#e74c3c;}}
.f-btn .badge{{font-size:10px;opacity:0.7;margin-left:2px;}}

/* Table wrap */
.table-wrap{{overflow-x:auto;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
th{{background:#f8f9fa;padding:8px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #eee;white-space:nowrap;cursor:pointer;user-select:none;position:sticky;top:0;z-index:10;}}
th:hover{{background:#e8eaf0;}}
th.sort-asc::after{{content:"▲";font-size:9px;}}
th.sort-desc::after{{content:"▼";font-size:9px;}}
td{{padding:8px;border-bottom:1px solid #f5f5f5;}}
tr:hover{{background:#f8f9ff;}}

/* Row coloring */
.row-good{{background:#f0faf4;}}
.row-bad{{background:#fef2f2;}}

/* Rank & score */
.rank{{font-weight:700;color:#e74c3c;text-align:center;width:28px;}}
.num{{text-align:right;font-variant-numeric:tabular-nums;}}
.score{{font-weight:700;color:#e74c3c;}}
.highlight{{font-weight:700;color:#e74c3c;}}
.warn{{color:#e74c3c;font-weight:700;}}
.good{{color:#27ae60;font-weight:700;}}
.name-col{{max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}

/* Cover image with hover preview */
.cover-cell{{width:80px;text-align:center;}}
.cover-wrap{{position:relative;display:inline-block;}}
.cover-img{{width:60px;height:80px;object-fit:cover;border-radius:6px;display:block;cursor:pointer;}}
.cover-preview{{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:999;background:#000;border-radius:12px;padding:4px;box-shadow:0 8px 40px rgba(0,0,0,0.5);}}
.cover-preview img{{max-width:70vw;max-height:80vh;border-radius:8px;display:block;}}
.cover-wrap:hover .cover-preview{{display:block !important;}}
.note-link{{display:block;font-size:11px;color:#999;text-decoration:none;margin-top:2px;}}
.note-link:hover{{color:#e74c3c;}}

/* Tags */
.tag{{display:inline-block;padding:1px 6px;border-radius:8px;font-size:10px;font-weight:600;}}
.tag-green{{background:#e8f8f0;color:#27ae60;}}
.tag-red{{background:#fde8e8;color:#e74c3c;}}
.tag-yellow{{background:#fff8e8;color:#f39c12;}}
.tag-gray{{background:#f0f0f0;color:#999;}}

/* Responsive */
@media(max-width:768px){{body{{padding:10px;}}.header h1{{font-size:18px;}}.header .sub{{font-size:12px;}}.card .num{{font-size:16px;}}.card{{padding:10px;}}.summary{{grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:12px;}}.summary .card:last-child:nth-child(3n-1){{grid-column:span 2;}}table{{font-size:10px;}}.section{{padding:12px;}}.section h2{{font-size:14px;}}.filter-bar{{gap:4px;}}.f-btn{{padding:6px 8px;font-size:11px;min-height:36px;}}.name-col{{max-width:100px;}}.cover-img{{width:36px;height:48px;}}th,td{{padding:4px 2px;}}th{{font-size:10px;}}.table-wrap{{-webkit-overflow-scrolling:touch;}}.bar-label{{width:50px;font-size:11px;}}.bar{{height:18px;}}.bar .bar-val{{font-size:9px;}}.note-link{{font-size:9px;}}thead{{position:sticky;top:0;z-index:10;}}thead th{{position:sticky;top:0;z-index:10;}}
</style>
</head>
<body>
<div class="container">

<div class="header">
<h1>\U0001f4ca \u5c0f\u7ea2\u4e66\u805a\u5149\u65e5\u62a5</h1>
<div class="sub">{today} ({wd}) | {target_date} | {len(sc)}城 {creative_count}条创意</div>
</div>

<div class="summary">
<div class="card accent"><div class="num">\u00a5{s['total_fee']:,.0f}</div><div class="label">\u603b\u6d88\u8017</div></div>
<div class="card"><div class="num">{s['total_msg']}</div><div class="label">\u79c1\u4fe1</div></div>
<div class="card"><div class="num">{s['total_msg_leads']}</div><div class="label">\u7559\u8d44</div></div>
<div class="card"><div class="num">{len(sc)}</div><div class="label">\u57ce\u5e02</div></div>
<div class="card"><div class="num">{creative_count}</div><div class="label">\u521b\u610f</div></div>
<div class="card"><div class="num">{top_ctr}%</div><div class="label">\u70b9\u51fb\u7387</div></div>
<div class="card"><div class="num">{top_ir}%</div><div class="label">\u4e92\u52a8\u7387</div></div>
<div class="card"><div class="num">{top_mlr}%</div><div class="label">\u7559\u8d44\u7387</div></div>
<div class="card"><div class="num">{top_mlc_str}</div><div class="label">留资成本</div></div>
</div>

<div class="section">
<h2>\U0001f3d9\ufe0f \u57ce\u5e02\u6d88\u8017</h2>
<div class="chart">{bars}</div>
</div>

<div class="section">
<h2>\U0001f4ca \u57ce\u5e02\u6c47\u603b</h2>
<div class="table-wrap">
<table>
<thead>
<tr>
<th>\u57ce\u5e02</th>
<th data-sort="1">\u6d88\u8017</th>
<th data-sort="1">\u70b9\u51fb</th>
<th data-sort="1">\u4e92\u52a8</th>
<th data-sort="1">\u79c1\u4fe1</th>
<th data-sort="1">\u7559\u8d44</th>
<th data-sort="1">\u70b9\u51fb\u7387</th>
<th data-sort="1">\u4e92\u52a8\u7387</th>
<th data-sort="1">\u7559\u8d44\u7387</th>
<th data-sort="1">\u79c1\u4fe1\u6210\u672c</th>
<th data-sort="1">\u7559\u8d44\u6210\u672c</th>
</tr>
</thead>
<tbody>
{city_table_rows}
</tbody>
</table>
</div>
</div>

<div class="section">
<h2>\U0001f464 \u8d26\u6237\u6c47\u603b <span class="badge">{len(acct_details)}\u4e2a</span></h2>
<div class="table-wrap">
<table>
<thead>
<tr>
<th>\u8d26\u6237</th>
<th>\u6240\u5c5e\u57ce\u5e02</th>
<th data-sort="1">\u6d88\u8017</th>
<th data-sort="1">\u70b9\u51fb</th>
<th data-sort="1">\u4e92\u52a8</th>
<th data-sort="1">\u79c1\u4fe1</th>
<th data-sort="1">\u7559\u8d44</th>
<th data-sort="1">\u70b9\u51fb\u7387</th>
<th data-sort="1">\u4e92\u52a8\u7387</th>
<th data-sort="1">\u7559\u8d44\u7387</th>
<th data-sort="1">\u79c1\u4fe1\u6210\u672c</th>
<th data-sort="1">\u7559\u8d44\u6210\u672c</th>
</tr>
</thead>
<tbody>
{acct_table_rows}
</tbody>
</table>
</div>
</div>

<div class="section">
<h2>\U0001f3c6 \u521b\u610f\u6392\u884c <span class="badge">{len(sorted_cr)}\u6761</span></h2>
<div class="filter-bar">{city_btns}</div>
<div class="table-wrap">
<table>
<thead>
<tr>
<th data-sort="0">#</th>
<th>\u5c01\u9762</th>
<th>\u7b14\u8bb0</th>
<th>\u57ce\u5e02</th>
<th data-sort="1" class="sort-desc">\u6d88\u8017</th>
<th data-sort="1">\u5c55\u73b0</th>
<th data-sort="1">\u70b9\u51fb</th>
<th data-sort="1">CTR</th>
<th data-sort="1">\u79c1\u4fe1</th>
<th data-sort="1">\u79c1\u4fe1\u6210\u672c</th>
<th data-sort="1">\u7559\u8d44</th>
<th data-sort="1">\u4e92\u52a8\u7387</th>
<th data-sort="1">\u8bc4\u5206</th>
<th>\u6548\u7387</th>
</tr>
</thead>
<tbody>
{cr_rows}
</tbody>
</table>
</div>
</div>

<div class="section">
<h2>\u26a0\ufe0f \u9ad8\u6d88\u8017\u4f4e\u8f6c\u5316</h2>
<table><thead><tr><th>\u7b14\u8bb0</th><th>\u6d88\u8017</th><th>\u79c1\u4fe1</th><th>\u57ce\u5e02</th></tr></thead><tbody>
{warn_r or '<tr><td colspan="4" style="text-align:center;color:#999;">无</td></tr>'}
</tbody></table>
</div>

<div class="section">
<h2>\U0001f4a1 \u9ad8\u6027\u4ef7\u6bd4</h2>
<table><thead><tr><th>\u7b14\u8bb0</th><th>\u6d88\u8017</th><th>\u79c1\u4fe1</th><th>\u57ce\u5e02</th></tr></thead><tbody>
{good_r or '<tr><td colspan="4" style="text-align:center;color:#999;">暂无</td></tr>'}
</tbody></table>
</div>

</div>

<script>
// City filter
document.querySelectorAll('.f-btn').forEach(btn => {{
  btn.addEventListener('click', function() {{
    document.querySelectorAll('.f-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    var city = this.dataset.city;
    document.querySelectorAll('tbody tr').forEach(row => {{
      row.style.display = (city === 'all' || row.dataset.city === city) ? '' : 'none';
    }});
  }});
}});

// Table sorting
document.querySelectorAll('th[data-sort]').forEach((th, idx) => {{
  th.addEventListener('click', function() {{
    var col = this.cellIndex;
    var table = this.closest('table');
    var tbody = table.querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var isDesc = this.classList.contains('sort-desc');
    table.querySelectorAll('th').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
    this.classList.add(isDesc ? 'sort-asc' : 'sort-desc');
    rows.sort(function(a, b) {{
      var av = a.cells[col]?.innerText.trim().replace(/[\u00a5,%]/g, '') || '0';
      var bv = b.cells[col]?.innerText.trim().replace(/[\u00a5,%]/g, '') || '0';
      var an = parseFloat(av), bn = parseFloat(bv);
      return isNaN(an) ? av.localeCompare(bv, 'zh') : (isDesc ? bn - an : an - bn);
    }});
    rows.forEach(function(r) {{ tbody.appendChild(r); }});
  }});
}});
</script>
</body>
</html>"""

# 输出文件名：参数2 = 自定义名，否则按月/日报命名
OUTPUT_NAME = sys.argv[2] if len(sys.argv) > 2 else ("小红书" + date_arg + "月报" if date_arg and len(date_arg) == 7 else "小红书聚光日报")
OUTPUT_HTML = f"{WORK_DIR}/reports/{OUTPUT_NAME}.html"
with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"\n\U0001f4c4 HTML\u5df2\u751f\u6210: {OUTPUT_HTML}", file=sys.stderr)
