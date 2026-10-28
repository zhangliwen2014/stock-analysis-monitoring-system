"""静态资产都在本地：CSP 是 'self'，模板与脚本里不许出现任何外部 URL。"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "src" / "sams" / "web"
VENDOR = WEB / "static" / "vendor"


def test_vendored_assets_exist_and_are_real():
    """钉版本的库随仓库走：体积太小多半是下载失败留下的空壳。"""
    assert (VENDOR / "htmx.min.js").stat().st_size > 10_000
    assert (VENDOR / "echarts.min.js").stat().st_size > 500_000


def test_app_js_has_no_external_urls():
    text = (WEB / "static" / "app.js").read_text()
    assert "http://" not in text and "https://" not in text


def test_templates_reference_no_external_urls():
    """模板从 Task 3 起陆续落地；这条从那时起真正拦截外部引用。

    本任务执行时 templates/ 还不存在，rglob 对缺失目录会抛异常，
    所以先判目录再扫——目录一旦建起来，这条断言立刻生效。
    """
    root = WEB / "templates"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.html")):
        text = path.read_text()
        assert "http://" not in text, path
        assert "https://" not in text, path


def test_app_js_dispatches_on_the_chart_kind_the_template_writes():
    """`data-chart-kind` 不再是写了没人读的装饰（§5.3 的实现时订正）。

    这一条只钉「脚本按 kind 分了两条路」这个事实：分时的画法对不对，
    由 `tmp/harness` 那条无头渲染断言看像素，不靠文本匹配假装看过图。
    """
    text = (WEB / "static" / "app.js").read_text()
    assert "dataset.chartKind" in text
    assert "function timeChart" in text
    assert "function klineChart" in text


def test_timeshare_volume_bars_are_capped_instead_of_re_gapping_the_axis():
    """量轴的 `boundaryGap` 必须留在 false：换成 true 也能治住那根几百像素的柱子，
    但柱子会挪开它自己那一分钟，而分时线是要贴住左右两边的。
    """
    text = (WEB / "static" / "app.js").read_text()
    assert "boundaryGap: false" in text
    assert "barMaxWidth" in text
