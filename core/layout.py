# -*- coding: utf-8 -*-
"""顶部恒定层：三页共用的一段 CSS，唯一来源。

目的
----
投放总览 / 关键词 / 投放诊断 三页顶部的「品牌区 + 页签 + 状态条 + 吸顶区间条」
必须落在同一像素位置，切换页签时不得有任何错落
（用户诉求：「感觉就在本页面切换卡片一样」）。

注入点（三处模板都留 `__TOP_STABLE_CSS__` 占位符）
---------------------------------------------------
  - 看板模板_v2.html     → 看板生成.py（repl）
  - 看板模板_关键词.html  → 看板生成.py（repl，另注入 v2 共享 CSS）
  - 看板模板_诊断.html    → core/diagrender.py（正式页）
                          + 看板服务.py._diag_css()（未采集引导页）

🔴 要调整顶部对齐，改这里，不要在各页 CSS 里单独改。
   各页若确需不同值，才在自身模板里覆盖（并在此处注明原因）。
"""

TOP_STABLE_CSS = """
/* ===== 顶部恒定层（单一来源：core/layout.py · TOP_STABLE_CSS）=====
   三页逐像素同构，切页不跳动。勿在各页 CSS 里另写一份。 */
.hero .kpis{min-height:127px}          /* KPI 块三页等高 → hero 总高恒定 → 吸顶条 y 恒定 */
.secnav{min-height:58px}               /* 吸顶条三页等高 → 区间档位 y 恒定 */
.hero-row1{align-items:center}
.brand{min-width:0;max-width:100%;padding-left:16px;border-left:3px solid #C8A96A}
.brand h1{display:block;line-height:1.3}
.brand .b-name{display:block;font-size:28px;letter-spacing:.3px;text-wrap:balance}
.brand .b-desc{display:block;margin-top:9px;font-size:12.5px;line-height:1.5;
  font-weight:400;letter-spacing:1px;color:var(--gold-deep)}
@media (min-width:1000px){
  .brand{flex:0 0 336px}               /* 标题块定宽 → 右侧起点恒定 */
  .hero-side{flex:1 1 auto;min-width:0}
  .hero-side>.pgtab{margin-right:auto;align-self:center}  /* 页签钉在左侧 → x 位置恒定 */
  .refreshbar{min-height:42px}         /* 状态条三页等高 → 页签 y 位置恒定 */
}
@media (min-width:1200px){
  /* 桌面端同排：标题 / 页签 / 操作按钮 / 两层紧凑状态块。 */
  .hero-row1{display:grid;grid-template-columns:310px minmax(0,1fr);gap:20px;min-height:88px}
  .brand{width:100%}
  .brand .b-name{white-space:nowrap}
  .hero-side{flex-wrap:nowrap;gap:10px}
  .hero-side>.pgtab{margin-right:0}
  .hero-side>.btn,.hero-side>.btn-allr{flex:none}
  .hero-side>.refreshbar{flex:1 1 0;min-width:0;min-height:72px;display:grid;
    grid-template-columns:7px auto auto minmax(0,1fr);gap:6px 8px;padding:7px 10px;align-items:center}
  .hero .refreshbar>.dot{grid-column:1;grid-row:1}
  .hero .refreshbar>.rf-txt{grid-column:2 / -1;grid-row:1;min-width:0;
    font-size:11px;line-height:1.5;white-space:normal;overflow-wrap:anywhere}
  .hero .refreshbar>#btnRefresh{grid-column:1 / 3;grid-row:2}
  .hero .refreshbar>#btnBal{grid-column:3;grid-row:2}
  .hero .refreshbar>.rf-sel{grid-column:4;grid-row:2;justify-self:end;max-width:100%}
}
@media (max-width:900px){
  .hero-side{width:100%;max-width:100%;min-width:0;justify-content:flex-start}
  .hero .refreshbar{min-width:0;max-width:100%;flex-wrap:wrap}
  .hero .refreshbar .rf-txt{min-width:0;flex:1 1 180px;white-space:normal;overflow-wrap:anywhere}
}
@media (max-width:520px){
  .brand{padding-left:12px}
  .brand .b-name{font-size:22px;letter-spacing:0}
  .brand .b-desc{margin-top:7px;font-size:11px;letter-spacing:.5px}
}
"""
