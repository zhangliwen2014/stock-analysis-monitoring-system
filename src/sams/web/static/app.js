/* 页面脚本：K 线图与局部刷新后的再初始化。
   CSP 是 'self'：本文件不许引用任何外部 URL，图表库在 vendor/ 里。 */
(function () {
  "use strict";

  // 红涨绿跌：与 app.css 的 .up=--danger / .down=--ok 同一对颜色
  var UP = "#b42318";
  var DOWN = "#067647";
  var TOP = 14; // legend 占的那一条
  var BAND = 70; // legend 之下、日期那一行与 dataZoom 滑块之上可用的百分比
  var SUB = 11; // 每格副图的高
  var GAP = 2; // 格与格之间
  var live = []; // 一页可以同时有 K 线与财务图：按元素各记一张，换档只换掉自己那张
  var PRICE = "#2563eb"; // 分时线：与涨跌无关，它要说的是「走到哪」，红绿会把它读成涨或跌
  var AVG = "#e8a33d"; // 均价线：交易所口径的均价，与分时线交叉处就是「强于均价/弱于均价」
  var FOCUS = "#7c3aed"; // 告警回跳的那根 bar：既不是涨也不是跌，用一条中性的竖线标它

  function sweep() {
    // 换卡后旧元素已经离开文档：实例要 dispose，不只是从 resize 名单里摘掉
    live = live.filter(function (chart) {
      var dom = chart.getDom();
      if (!dom || !dom.isConnected) {
        chart.dispose();
        return false;
      }
      return true;
    });
  }

  window.addEventListener("resize", function () {
    sweep();
    live.forEach(function (chart) { chart.resize(); });
  });

  function mount(el, option) {
    var stale = echarts.getInstanceByDom(el);
    if (stale) {
      stale.dispose();
      live = live.filter(function (chart) { return chart !== stale; });
    }
    var chart = echarts.init(el);
    chart.setOption(option);
    live.push(chart);
    return chart;
  }

  function candles(payload) {
    // 接口把两基都给了（D302）：`open/high/low/close` 是交易所原值，`*_hfq` 是乘过累计
    // 因子的那一根，`basis` 点名图上该画哪一族。均线全算在 `close_hfq` 上，所以画错一族
    // 就等于把每一次分红都画成「还没收复」
    return payload.basis === "hfq"
      ? {
          open: payload.open_hfq, close: payload.close_hfq,
          low: payload.low_hfq, high: payload.high_hfq,
        }
      : payload;
  }

  function candlestick(prices, dates) {
    // ECharts 的 candlestick 数据顺序是 [open, close, low, high]，不是 OHLC：
    // 写反会得到实体与影线互相穿过的假蜡烛
    var rows = dates.map(function (_date, i) {
      return [prices.open[i], prices.close[i], prices.low[i], prices.high[i]];
    });
    return {
      name: "K线", type: "candlestick", data: rows,
      xAxisIndex: 0, yAxisIndex: 0,
      itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
    };
  }

  function paneSeries(group, pane, prices) {
    var out = [];
    Object.keys(group).forEach(function (column) {
      var data = group[column];
      if (!data || !data.length) return;
      if (column === "volume") {
        out.push({
          name: "成交量", type: "bar", data: data,
          xAxisIndex: pane, yAxisIndex: pane,
          // 量柱跟着那根的涨跌上色：看量要看的是「放量那天是涨是跌」
          itemStyle: {
            color: function (params) {
              return prices.close[params.dataIndex] >= prices.open[params.dataIndex]
                ? UP : DOWN;
            },
          },
        });
      } else if (column === "macd_hist") {
        out.push({
          name: "MACD柱", type: "bar", data: data,
          xAxisIndex: pane, yAxisIndex: pane,
          itemStyle: { color: UP, color0: DOWN },
        });
      } else {
        out.push({
          name: column, type: "line", data: data,
          xAxisIndex: pane, yAxisIndex: pane,
          showSymbol: false, lineStyle: { width: 1 },
        });
      }
    });
    return out;
  }

  function wan(value) {
    // 量轴上写 "1,000,000,000" 没人读得出来：那一格要说的是「这天放没放量」，亿/万 才是它的单位
    var abs = Math.abs(value);
    if (abs >= 1e8) return (value / 1e8).toFixed(1) + "亿";
    if (abs >= 1e4) return (value / 1e4).toFixed(0) + "万";
    return String(value);
  }

  function priceTick(value) {
    // 上下沿是 anchor±span 的浮点结果：收的只是显示，min/max 不动——动了就不再是同一个半径
    return echarts.format.addCommas(value.toFixed(2));
  }

  function percentTick(value) {
    // 上下沿换成百分比后同样是浮点数：不收到两位小数，屏幕右上角就是一长串
    return value.toFixed(2) + "%";
  }

  function caption(payload) {
    var bits = [];
    if (payload.pending && payload.pending.length) {
      // `pending` 那一格是**日历收市日**（周五 / 月末）：说「只走到周五」是把还没发生的一天
      // 说成已经收进来了。实际盖到哪一天在 `last` 里
      var end = payload.pending[0];
      var index = (payload.dates || []).indexOf(end);
      var at = index >= 0 && payload.last ? payload.last[index] : end;
      var label = payload.period_label || payload.period || "";
      bits.push(
        at === end
          ? "最后一根" + label + "只走到 " + at + "，还没走完"
          : "最后一根" + label + "走到 " + at + "，要等 " + end + " 收市才算走完"
      );
    }
    if (payload.warmup_floor) bits.push("均线从 " + payload.warmup_floor + " 起才有数");
    if (payload.anchored_on) bits.push("序列自 " + payload.anchored_on + " 起（含预热）");
    return bits.length ? "：" + bits.join("；") : "";
  }

  function klineChart(el, payload) {
    var box = el.parentNode;
    var note = box && box.querySelector(".chart-caption");
    var dates = payload.dates || [];
    if (!dates.length) {
      if (note) note.textContent = "这一段没有数可画。";
      return;
    }
    var subNames = Object.keys(payload.sub || {});
    var mainHeight = BAND - subNames.length * (SUB + GAP);
    var grids = [{ top: TOP + "%", height: mainHeight + "%" }];
    var top = TOP + mainHeight + GAP;
    subNames.forEach(function () {
      grids.push({ top: top + "%", height: SUB + "%" });
      top += SUB + GAP;
    });
    var xAxes = [];
    var yAxes = [];
    var indices = [];
    var volAt = subNames.indexOf("vol");
    var volPane = volAt < 0 ? -1 : volAt + 1; // 没有量那一格时不许把价格轴也格式化成「亿」
    grids.forEach(function (_grid, p) {
      var last = p === grids.length - 1;
      // 日期只在最下面那一格写一次：四格各写一遍会把画布挤成只剩字
      xAxes.push({
        gridIndex: p, type: "category", data: dates, boundaryGap: true,
        // 日期与最后一格的下沿刻度会撞在同一行上（那一格的 -50 就压在轴线上）：
        // 给日期一行留出 margin，而不是把格子的数字删掉
        axisLabel: { show: last, margin: 10 },
        axisTick: { show: false }, axisLine: { show: last },
      });
      yAxes.push({
        gridIndex: p, type: "value", scale: true,
        // 每一格都要有刻度：副图没有数字就读不出「MACD 在零轴上还是下」，而那是
        // 那一格唯一要说的事。格子只有 11% 高，所以副图只切两段、字号压小
        splitNumber: p === 0 ? 5 : 2,
        axisLabel: {
          show: true, fontSize: p === 0 ? 12 : 10,
          // 格子矮，刻度是 ECharts 自己挑的（splitNumber 只是建议），挤不下就自己藏：
          // 叠成一团的数字比少一个数字更难看，也读不出「在零轴上还是下」
          hideOverlap: true,
          formatter: p === volPane ? wan : undefined,
        },
        splitLine: { show: p === 0 },
      });
      indices.push(p);
    });
    var series = [];
    var prices = candles(payload);
    if (prices.open && prices.open.length) series.push(candlestick(prices, dates));
    Object.keys(payload.main || {}).forEach(function (family) {
      series = series.concat(paneSeries(payload.main[family], 0, prices));
    });
    subNames.forEach(function (family, i) {
      series = series.concat(paneSeries(payload.sub[family], i + 1, prices));
    });
    // 告警详情带过来的那一天：找到就画竖线并把窗口收到它前后各 30 根；
    // 找不到不能假装标上了——默认窗口只有 180 天，那句话得由图注说
    var focus = (el.dataset.focus || "").trim();
    var focusAt = focus ? dates.indexOf(focus) : -1;
    if (focusAt >= 0 && series.length) {
      series[0].markLine = {
        symbol: "none", silent: true, animation: false,
        data: [{ xAxis: dates[focusAt] }],
        lineStyle: { type: "dashed", color: FOCUS },
        label: { formatter: "告警 " + dates[focusAt], position: "insideEndTop" },
      };
    }
    var insideZoom = { type: "inside", xAxisIndex: indices };
    var sliderZoom = { type: "slider", xAxisIndex: indices, height: 18, bottom: 2 };
    if (focusAt >= 0) {
      var from = Math.max(0, focusAt - 30);
      var to = Math.min(dates.length - 1, focusAt + 30);
      insideZoom.startValue = from;
      insideZoom.endValue = to;
      sliderZoom.startValue = from;
      sliderZoom.endValue = to;
    }
    mount(el, {
      // legend 不给 data：ECharts 自己按 series 的名字铺，加一种指标不必回来改这里
      legend: { type: "scroll", top: 0 },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [insideZoom, sliderZoom],
      series: series,
    });
    var captionBits = caption(payload);
    if (focus && focusAt < 0) {
      captionBits += (captionBits ? "；" : "：") + "告警那天 " + focus + " 不在当前窗口里";
    }
    if (note) note.textContent = note.textContent + captionBits;
  }

  function timeChart(el, payload) {
    var box = el.parentNode;
    var note = box && box.querySelector(".chart-caption");
    var times = payload.times || [];
    if (!times.length) {
      if (note) note.textContent = "这一段没有数可画。";
      return;
    }
    var baseline = payload.prev_close;
    // 昨收没给（源里那一格空）就退回价格区间的中点当原点：按 0 算百分比会画出一条 0% 在天外的轴
    var haveBase = typeof baseline === "number" && baseline > 0;
    var prices = payload.price || [];
    var lo = Infinity;
    var hi = -Infinity;
    prices.forEach(function (value) {
      if (typeof value !== "number") return;
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    });
    if (lo === Infinity) { lo = hi = haveBase ? baseline : 0; }
    var anchor = haveBase ? baseline : (lo + hi) / 2;
    // 两条轴共用同一个「离原点多远」：只按价格取范围时涨 2% 与跌 2% 的视觉斜率不同，
    // 而分时图给人看的正是那个斜率
    var span = Math.max(hi - anchor, anchor - lo) * 1.05 || 0.01;
    var pctSpan = haveBase ? (span / baseline) * 100 : 1;
    var grids = [
      { top: TOP + "%", height: BAND - SUB - GAP + "%" },
      // 主图下沿 + 间隙 = `14 + 70 - 11` = 73%：那一格只有 11% 高，再留 GAP 就顶到滑块
      { top: TOP + BAND - SUB + "%", height: SUB + "%" },
    ];
    var xAxes = [
      { gridIndex: 0, type: "category", data: times, boundaryGap: false,
        axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } },
      { gridIndex: 1, type: "category", data: times, boundaryGap: false,
        axisLabel: { show: true, margin: 10 }, axisTick: { show: false }, axisLine: { show: true } },
    ];
    var yAxes = [
      { gridIndex: 0, type: "value", min: anchor - span, max: anchor + span,
        axisLabel: { show: true, hideOverlap: true, formatter: priceTick }, splitLine: { show: true } },
      // 第二条轴写百分比：盯盘的人看的是「现在离昨收几个点」，那条轴就在右手边
      { gridIndex: 0, type: "value", position: "right", min: -pctSpan, max: pctSpan,
        axisLabel: { show: true, hideOverlap: true, formatter: percentTick }, splitLine: { show: false } },
      { gridIndex: 1, type: "value", axisLabel: { show: true, fontSize: 10, hideOverlap: true,
        formatter: wan }, splitLine: { show: false } },
    ];
    var series = [
      {
        name: "分时", type: "line", data: prices,
        xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
        lineStyle: { width: 1, color: PRICE },
        // 昨收那条横基准线是分时的原点：没有它，屏幕上的高低只剩「比右边那格高」
        markLine: haveBase
          ? {
              symbol: "none", silent: true, animation: false,
              data: [{ yAxis: baseline }],
              lineStyle: { type: "dashed", color: "#8a8a94" },
              label: { formatter: "昨收 " + baseline.toFixed(2), position: "insideStartTop" },
            }
          : undefined,
      },
      {
        name: "均价", type: "line", data: payload.avg || [],
        xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
        lineStyle: { width: 1, color: AVG },
      },
      {
        // 每分钟的量：服务侧已经把累计量差分过（Task 8），前端不能再 diff 一遍
        name: "成交量", type: "bar", data: payload.volume || [],
        xAxisIndex: 1, yAxisIndex: 2,
        // boundaryGap:false 下柱宽是按整段摊出来的，几个点就能摊出几百像素：4 ≈ 241 分钟铺满这一格的一分钟
        barMaxWidth: 4,
        itemStyle: {
          color: function (params) {
            var now = prices[params.dataIndex];
            var prev = params.dataIndex > 0 ? prices[params.dataIndex - 1] : anchor;
            return now >= prev ? UP : DOWN;
          },
        },
      },
    ];
    mount(el, {
      legend: { type: "scroll", top: 0 },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        { type: "slider", xAxisIndex: [0, 1], height: 18, bottom: 2 },
      ],
      series: series,
    });
    if (note && payload.date) {
      // 分时只答最近一个交易日（Task 3 的区间校验钉着），日期不写在屏上会让人以为这是「今天」
      note.textContent = note.textContent + "：" + payload.date;
    }
  }

  // 财务图：上格营收/净利双柱（亿元），下格 ROE/毛利率/净利率三线（%）。
  // 两种单位各占一条 y 轴；NULL 原样交给 ECharts（断线），不补零——补零会画出一条
  // 「利润归零」的假曲线。
  var FIN_REVENUE = "#4c78a8"; // 与涨跌无关：营收不是价格，别用红绿说它
  var FIN_PROFIT = "#f58518";
  var FIN_LINES = ["#7c3aed", "#0ea5e9", "#b279a2"];

  function yi(values) {
    return (values || []).map(function (value) {
      return value == null ? null : value / 1e8;
    });
  }

  function financeChart(el, payload) {
    var labels = payload.labels || [];
    if (!labels.length) return;
    var moneyUnit = " 亿元";
    // 两格各挂各的 series，tooltip 却要一次说全：按横轴标签把五个数拼回一行
    var rows = {};
    labels.forEach(function (label, i) {
      rows[label] = [
        ["营业总收入", payload.revenue[i] == null ? null : payload.revenue[i] / 1e8, moneyUnit],
        ["归母净利润", payload.net_profit[i] == null ? null : payload.net_profit[i] / 1e8, moneyUnit],
        ["ROE", payload.roe[i], " %"],
        ["毛利率", payload.gross_margin[i], " %"],
        ["净利率", payload.net_margin[i], " %"],
      ];
    });
    mount(el, {
      animation: false,
      legend: { type: "scroll", top: 0 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (items) {
          if (!items || !items.length) return "";
          var label = items[0].axisValue;
          var lines = [label];
          (rows[label] || []).forEach(function (cell) {
            if (cell[1] === null || cell[1] === undefined) return;
            lines.push(cell[0] + "：" + Number(cell[1]).toFixed(2) + cell[2]);
          });
          return lines.join("<br>");
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 64, right: 24, top: 34, height: "34%" },
        { left: 64, right: 24, top: "62%", height: "26%" },
      ],
      xAxis: [
        { type: "category", data: labels, gridIndex: 0,
          axisLabel: { show: false }, axisTick: { show: false } },
        { type: "category", data: labels, gridIndex: 1, axisTick: { show: false } },
      ],
      yAxis: [
        // 金额柱必须从 0 起：裁掉基线会把 8% 的营收差画成一半（scale:true 会裁）
        { type: "value", gridIndex: 0, name: "亿元", min: 0 },
        { type: "value", gridIndex: 1, name: "%", scale: true },
      ],
      series: [
        { name: "营业总收入", type: "bar", data: yi(payload.revenue),
          xAxisIndex: 0, yAxisIndex: 0, barMaxWidth: 46,
          itemStyle: { color: FIN_REVENUE } },
        { name: "归母净利润", type: "bar", data: yi(payload.net_profit),
          xAxisIndex: 0, yAxisIndex: 0, barMaxWidth: 46,
          itemStyle: { color: FIN_PROFIT } },
        { name: "ROE", type: "line", data: payload.roe,
          xAxisIndex: 1, yAxisIndex: 1, connectNulls: false, symbolSize: 5,
          lineStyle: { width: 2, color: FIN_LINES[0] }, itemStyle: { color: FIN_LINES[0] } },
        { name: "毛利率", type: "line", data: payload.gross_margin,
          xAxisIndex: 1, yAxisIndex: 1, connectNulls: false, symbolSize: 5,
          lineStyle: { width: 2, color: FIN_LINES[1] }, itemStyle: { color: FIN_LINES[1] } },
        { name: "净利率", type: "line", data: payload.net_margin,
          xAxisIndex: 1, yAxisIndex: 1, connectNulls: false, symbolSize: 5,
          lineStyle: { width: 2, color: FIN_LINES[2] }, itemStyle: { color: FIN_LINES[2] } },
      ],
    });
  }

  function navChart(el, payload) {
    // 组合净值：算不出来的那天是 null，`connectNulls:false` 让它断在那儿——
    // 用前值接起来会画出一条「平滑」的假线，而那一天本来就该看见缺口
    var days = payload.days || [];
    if (!days.length) return;
    mount(el, {
      animation: false,
      tooltip: {
        trigger: "axis",
        formatter: function (items) {
          if (!items || !items.length) return "";
          var point = items[0];
          if (point.value === null || point.value === undefined) {
            return point.axisValue + "<br>缺口：缺价或缺汇率，这天不计";
          }
          return point.axisValue + "<br>净值：" +
            Number(point.value).toLocaleString("zh-CN", { maximumFractionDigits: 2 }) +
            (payload.currency ? " " + payload.currency : "");
        },
      },
      grid: { left: 76, right: 24, top: 18, bottom: 28 },
      xAxis: { type: "category", data: days, axisTick: { show: false },
        axisLabel: { showMaxLabel: true } },
      yAxis: { type: "value", scale: true,
        axisLabel: { formatter: function (value) {
          return Math.abs(value) >= 1e4 ? (value / 1e4).toFixed(1) + "万" : value.toFixed(0);
        } } },
      series: [{
        name: "净值", type: "line", data: payload.nav, connectNulls: false,
        showSymbol: false, lineStyle: { width: 2, color: PRICE },
        itemStyle: { color: PRICE },
        areaStyle: { opacity: 0.08, color: PRICE },
      }],
    });
  }

  function samsChart(el) {
    if (typeof echarts === "undefined" || el.dataset.samsChart === "on") return;
    var url = el.dataset.chartUrl;
    if (!url) return;
    el.dataset.samsChart = "on";
    fetch(url)
      .then(function (resp) { return resp.json(); })
      .then(function (payload) {
        // 响应回来时这张卡可能已经被 htmx 换掉：画到摘除的 div 上会留一张看不见的活图
        if (!el.isConnected) return;
        var box = el.parentNode;
        var note = box && box.querySelector(".chart-caption");
        if (payload.note) {
          if (note) note.textContent = payload.note;
          el.textContent = "";
          return;
        }
        // 三份 payload 的键不一样：分时时 `payload.times`，蜡烛时 `payload.dates`，
        // 财务时 `payload.labels`——拿一套键去读另一套会得到一条空白曲线而不是报错，
        // 那比空白更难查
        if (el.dataset.chartKind === "time") timeChart(el, payload);
        else if (el.dataset.chartKind === "finance") financeChart(el, payload);
        else if (el.dataset.chartKind === "nav") navChart(el, payload);
        else klineChart(el, payload);
      })
      .catch(function () {
        if (!el.isConnected) return;
        el.textContent = "行情序列读不出来。";
      });
  }

  function boot(root) {
    sweep();
    (root || document).querySelectorAll("[data-chart-url]").forEach(samsChart);
  }

  // 收件箱的「全选本页未读」：只勾这一页真有框的行（终态行不渲染 checkbox），
  // 用一个委托处理器——htmx 换过分页/筛选后不必重新绑一遍
  document.addEventListener("change", function (event) {
    var master = event.target;
    if (!master || !master.matches || !master.matches("[data-ack-all]") || !master.form) return;
    master.form.querySelectorAll('input[name="ids"]').forEach(function (box) {
      box.checked = master.checked;
    });
  });

  // 导航搜索：Esc 收起结果（S4-C）。不清输入框——收了又清空等于替用户重来一遍；
  // 空输入另有 htmx 的 search 事件收走结果
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var results = document.getElementById("nav-search-results");
    if (!results) return;
    results.innerHTML = "";
    var box = document.querySelector(".nav-search input");
    if (box && box === document.activeElement) box.blur();
  });

  // 候选行的「填入」：一张共用监听，服务规则/建论/限额/关键词/探源那几张表单。
  // 只写**这个表单里真存在的那几格**——各页字段名不一样（code / scope / target_ref），
  // 按页各开一个 scope 就得把同一份候选表抄七遍。收起面板靠约定：交换目标那个 div
  // 的 id 以 `-candidates` 结尾（模板守卫测试钉着这条约定）。
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-pick-code]") : null;
    if (!button) return;
    var form = button.closest("form");
    if (!form) return;
    var pick = button.dataset;
    // 市场格不管是下拉还是隐藏域都跟着这一行走：选了另一市场的票却把市场留在原处，
    // 落库的是一条永远匹配不上的关键词（资讯那一张表单就是这样）
    var market = form.querySelector("[name=market]");
    if (market) market.value = pick.pickMarket;
    // 代码类字段只挑非隐藏的：`<input type="hidden" name="code">` 带着的是这一屏
    // 已经在用的过滤条件，替用户改掉它等于悄悄换了查询
    var bare = form.querySelector('input[name=code]:not([type="hidden"])');
    if (bare) bare.value = pick.pickCode;
    var limit = form.querySelector('input[name=scope]:not([type="hidden"])');
    if (limit) limit.value = pick.pickCode;
    var ref = form.querySelector('input[name=target_ref]:not([type="hidden"])');
    if (ref) ref.value = pick.pickRef;
    // 资讯那格搜的是标题：标题里出现的是名字，代码填进去什么都搜不到
    var keyword = form.querySelector('input[name=q]:not([type="hidden"])');
    if (keyword) keyword.value = pick.pickName;
    var box = button.closest("[id$=-candidates]");
    if (box) box.innerHTML = "";
  });

  document.addEventListener("DOMContentLoaded", function () { boot(document); });
  // htmx:afterSwap 会冒泡到 document，挂在 document 上就不依赖脚本排在 head 还是 body 末尾。
  document.addEventListener("htmx:afterSwap", function (event) {
    boot(event.target);
  });
})();
