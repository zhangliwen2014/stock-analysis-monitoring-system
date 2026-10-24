/* 页面脚本：K 线图与局部刷新后的再初始化。
   CSP 是 'self'：本文件不许引用任何外部 URL，图表库在 vendor/ 里。 */
(function () {
  "use strict";

  function samsChart(el) {
    if (typeof echarts === "undefined" || el.dataset.samsChart === "on") return;
    var url = el.dataset.chartUrl;
    if (!url) return;
    el.dataset.samsChart = "on";
    fetch(url)
      .then(function (resp) { return resp.json(); })
      .then(function (payload) {
        var chart = echarts.init(el);
        if (payload.note) {
          var p = document.createElement("p");
          p.className = "muted";
          p.textContent = payload.note;
          el.parentNode.insertBefore(p, el);
        }
        chart.setOption({
          tooltip: { trigger: "axis" },
          // 三条线同一个基：接口给的 close_hfq 与算在 close_hfq 上的 MA20/MA60。
          // 画裸 close 会让一次分红把两条均线永久顶在价格线上方，看上去像涨不回去
          legend: { data: ["收盘（后复权）", "MA20", "MA60"] },
          xAxis: { type: "category", data: payload.dates },
          yAxis: { type: "value", scale: true },
          dataZoom: [{ type: "inside" }],
          series: [
            { name: "收盘（后复权）", type: "line", data: payload.close_hfq, showSymbol: false },
            { name: "MA20", type: "line", data: payload.ma20, showSymbol: false },
            { name: "MA60", type: "line", data: payload.ma60, showSymbol: false },
          ],
        });
        window.addEventListener("resize", function () { chart.resize(); });
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
