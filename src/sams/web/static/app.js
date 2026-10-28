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
  var live = null; // 只留一张活的图：换档是整块重画，旧的不 dispose 会把 resize 挂满
  var PRICE = "#2563eb"; // 分时线：与涨跌无关，它要说的是「走到哪」，红绿会把它读成涨或跌
  var AVG = "#e8a33d"; // 均价线：交易所口径的均价，与分时线交叉处就是「强于均价/弱于均价」

  window.addEventListener("resize", function () {
    if (live) live.resize();
  });

  function candlestick(payload, dates) {
    // ECharts 的 candlestick 数据顺序是 [open, close, low, high]，不是 OHLC：
    // 写反会得到实体与影线互相穿过的假蜡烛
    var rows = dates.map(function (_date, i) {
      return [payload.open[i], payload.close[i], payload.low[i], payload.high[i]];
    });
    return {
      name: "K线", type: "candlestick", data: rows,
      xAxisIndex: 0, yAxisIndex: 0,
      itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
    };
  }

  function paneSeries(group, pane, payload) {
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
              return payload.close[params.dataIndex] >= payload.open[params.dataIndex]
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

  function caption(payload) {
    var bits = [];
    if (payload.pending && payload.pending.length) {
      bits.push("最后一根" + (payload.period_label || payload.period || "") + "只走到 " + payload.pending[0] + "，还没走完");
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
    if (payload.open && payload.open.length) series.push(candlestick(payload, dates));
    Object.keys(payload.main || {}).forEach(function (family) {
      series = series.concat(paneSeries(payload.main[family], 0, payload));
    });
    subNames.forEach(function (family, i) {
      series = series.concat(paneSeries(payload.sub[family], i + 1, payload));
    });
    if (live) live.dispose();
    var chart = echarts.init(el);
    live = chart;
    chart.setOption({
      // legend 不给 data：ECharts 自己按 series 的名字铺，加一种指标不必回来改这里
      legend: { type: "scroll", top: 0 },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: "inside", xAxisIndex: indices },
        { type: "slider", xAxisIndex: indices, height: 18, bottom: 2 },
      ],
      series: series,
    });
    if (note) note.textContent = note.textContent + caption(payload);
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
        axisLabel: { show: true, hideOverlap: true }, splitLine: { show: true } },
      // 第二条轴写百分比：盯盘的人看的是「现在离昨收几个点」，那条轴就在右手边
      { gridIndex: 0, type: "value", position: "right", min: -pctSpan, max: pctSpan,
        axisLabel: { show: true, hideOverlap: true, formatter: "{value}%" }, splitLine: { show: false } },
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
        itemStyle: {
          color: function (params) {
            var now = prices[params.dataIndex];
            var prev = params.dataIndex > 0 ? prices[params.dataIndex - 1] : anchor;
            return now >= prev ? UP : DOWN;
          },
        },
      },
    ];
    if (live) live.dispose();
    var chart = echarts.init(el);
    live = chart;
    chart.setOption({
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

  function samsChart(el) {
    if (typeof echarts === "undefined" || el.dataset.samsChart === "on") return;
    var url = el.dataset.chartUrl;
    if (!url) return;
    el.dataset.samsChart = "on";
    fetch(url)
      .then(function (resp) { return resp.json(); })
      .then(function (payload) {
        var box = el.parentNode;
        var note = box && box.querySelector(".chart-caption");
        if (payload.note) {
          if (note) note.textContent = payload.note;
          el.textContent = "";
          return;
        }
        // 两份 payload 的键不一样：分时时 `payload.times`，蜡烛时 `payload.dates`，
        // 拿一套键去读另一套会得到一条空白曲线而不是报错——那比空白更难查
        if (el.dataset.chartKind === "time") timeChart(el, payload);
        else klineChart(el, payload);
      })
      .catch(function () {
        el.textContent = "行情序列读不出来。";
      });
  }

  function boot(root) {
    (root || document).querySelectorAll("[data-chart-url]").forEach(samsChart);
  }

  document.addEventListener("DOMContentLoaded", function () { boot(document); });
  // htmx:afterSwap 会冒泡到 document，挂在 document 上就不依赖脚本排在 head 还是 body 末尾。
  document.addEventListener("htmx:afterSwap", function (event) {
    boot(event.target);
  });
})();
