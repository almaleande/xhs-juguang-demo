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
@media (min-width:1000px){
  .brand{flex:0 0 336px}               /* 品牌块定宽（最长副标题 326px）→ 右侧起点恒定 */
  .hero-side{flex:1 1 auto;min-width:0}
  .hero-side>.pgtab{margin-right:auto;align-self:center}  /* 页签钉在左侧 → x 位置恒定 */
  .refreshbar{min-height:42px}         /* 状态条三页等高 → 页签 y 位置恒定 */
}
"""
