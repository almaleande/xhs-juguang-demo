# -*- coding: utf-8 -*-
"""顶部总览 KPI：三页共用的**唯一口径来源**。

用户要求：投放总览 / 关键词 / 投放诊断 三页顶部的「总览信息」，
指标口径必须与**投放总览**一致（不只是样式）。

口径（6 张卡，顺序固定）
------------------------
  1. 消耗      fee          金        src = 该页口径说明（创意口径 / 词口径 / 计划级口径）
  2. 点击率    clk ÷ imp
  3. 开口率    msg ÷ clk
  4. 留资率    ml  ÷ msg    绿
  5. 客资成本  fee ÷ ml     红
  6. 点击成本  fee ÷ clk

注入点（三处模板都留 `__TOP_KPI_JS__` 占位符，位于 <script> 内）
  - 看板模板_v2.html      → 看板生成.py
  - 看板模板_关键词.html   → 看板生成.py
  - 看板模板_诊断.html     → core/diagrender.py

🔴 要改顶部口径，只改这里；三页自动一致。
   页内自带的 money/num0 语义不同，故此处内置 _kpiMoney/_kpiMoney2/_kpiNum0，
   保证三页数字格式逐字相同（与总览原实现一致）。
"""

TOP_KPI_JS = """
/* ===== 顶部总览 KPI（单一来源：core/kpi_spec.py · TOP_KPI_JS）=====
   口径：消耗 / 点击率 / 开口率 / 留资率 / 客资成本 / 点击成本 —— 与投放总览一致。
   三页共用本函数，勿在各页另写一套卡片数组。 */
function _kpiMoney(v){ if(v==null) return "—"; return "¥" + Math.round(v).toLocaleString("en-US"); }
function _kpiMoney2(v){ if(v==null) return "—"; return "¥" + Number(v).toFixed(2); }
function _kpiNum0(v){ return v==null ? "" : Number(v).toLocaleString("en-US"); }

function topKpiCards(S, feeSrc){
  S = S || {};
  var fee = Number(S.fee)||0, imp = Number(S.imp)||0, clk = Number(S.clk)||0;
  var msg = Number(S.msg)||0, ml = Number(S.ml)||0;
  var ctr = (S.ctr!=null) ? S.ctr : (imp ? clk/imp*100 : null);
  var mlc = (S.mlc!=null) ? S.mlc : (ml ? fee/ml : null);
  var cpc = (S.cpc!=null) ? S.cpc : (clk ? fee/clk : null);
  return [
    {label:"消耗",     val:_kpiMoney(fee),                     cls:"gold",
     src:(feeSrc || "创意口径")},
    {label:"点击率",   val:(ctr!=null ? ctr.toFixed(2)+"%" : "—"), cls:"",
     src:"点击 "+_kpiNum0(S.clk)+" · 展现 "+_kpiNum0(S.imp)},
    {label:"开口率",   val:(clk && msg ? (msg/clk*100).toFixed(2)+"%" : "—"), cls:"",
     src:"开口 "+_kpiNum0(S.msg)},
    {label:"留资率",   val:(msg && S.ml!=null ? (ml/msg*100).toFixed(2)+"%" : "—"), cls:"green",
     src:"客资 "+_kpiNum0(S.ml)},
    {label:"客资成本", val:(mlc!=null ? _kpiMoney(mlc) : "—"), cls:"red",
     src:"消耗 ÷ 留资"},
    {label:"点击成本", val:(cpc!=null ? _kpiMoney2(cpc) : "—"), cls:"",
     src:"消耗 ÷ 点击"}
  ];
}

function renderTopKpi(elId, S, feeSrc){
  var el = document.getElementById(elId);
  if(!el) return;
  el.innerHTML = topKpiCards(S, feeSrc).map(function(c){
    return '<div class="kpi"><div class="kpi-label">'+c.label+'</div>'
      + '<div class="kpi-val '+(c.cls||"")+'">'+c.val+'</div>'
      + '<div class="kpi-src">'+c.src+'</div>'
      + '<div class="kpi-mom flat">—</div></div>';
  }).join("");
}

/* 无数据（未采集）时的骨架：同标签、同版式、一律中性灰 */
function renderTopKpiSkeleton(elId){
  var labels = ["消耗","点击率","开口率","留资率","客资成本","点击成本"];
  var el = document.getElementById(elId);
  if(!el) return;
  el.innerHTML = labels.map(function(l){
    return '<div class="kpi"><div class="kpi-label">'+l+'</div>'
      + '<div class="kpi-val">—</div><div class="kpi-src">待采集</div>'
      + '<div class="kpi-mom flat">—</div></div>';
  }).join("");
}
"""
