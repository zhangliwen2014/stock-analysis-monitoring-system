/* 页面脚本：K 线图与局部刷新后的再初始化。
   CSP 是 'self'：本文件不许引用任何外部 URL，图表库在 vendor/ 里。 */
(function () {
  "use strict";

  // 红涨绿跌：与 app.css 的 .up=--danger / .down=--ok 同一对颜色
  var UP = "#b42318";
  var DOWN = "#067647";
  var TOP = 14; // legend 占的那一条
  var BAND = 74; // legend 之下、dataZoom 滑块之上可用的百分比
  var SUB = 11; // 每格副图的高
  var GAP = 2; // 格与格之间
  var live = null; // 只留一张活的图：换档是整块重画，旧的不 dispose 会把 resize 挂满

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
          showSymbol: false, lineWidth: 1,
        });
      }
    });
    return out;
  }

  function caption(payload) {
    var bits = [];
    if (payload.pending && payload.pending.length) {
      bits.push("最后一根" + (payload.period || "") + "只走到 " + payload.pending[0] + "，还没走完");
    }
    if (payload.warmup_floor) bits.push("均线从 " + payload.warmup_floor + " 起才有数");
    if (payload.anchored_on) bits.push("序列自 " + payload.anchored_on + " 起（含预热）");
    return bits.length ? "：" + bits.join("；") : "";
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
        grids.forEach(function (_grid, p) {
          var last = p === grids.length - 1;
          // 日期只在最下面那一格写一次：四格各写一遍会把画布挤成只剩字
          xAxes.push({
            gridIndex: p, type: "category", data: dates, boundaryGap: true,
            axisLabel: { show: last }, axisTick: { show: false }, axisLine: { show: last },
          });
          yAxes.push({
            gridIndex: p, type: "value", scale: true,
            axisLabel: { show: p === 0 }, splitLine: { show: p === 0 },
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
