"""把交易行响应转换成适合群聊的查询结果。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .market import CURVE_ENDPOINT, SUGGEST_ENDPOINT, MarketClient, MarketError, MarketResult

DAYS = {"3天": "72", "7天": "168", "15天": "360"}
REGIONS = {"国服": "cn", "国际服": "en"}
PAGE_SIZE = 8


@dataclass(frozen=True, slots=True)
class _Report:
    endpoint: str
    title: str
    fields: tuple[tuple[str, str], ...]
    rank: str = ""
    regional: bool = False
    name: str = "itemName"


REPORTS = {
    "热门": _Report("/getHIPriceCurveData", "热门查询物品", (("currectPrice", "现价"),), regional=True, name="objectName"),
    "单格价值": _Report("/getPropPerSlotPriceData", "收集品及消耗品单格价值", (
        ("itemTypeName", "类别"), ("itemGrade", "等级"), ("itemSize", "占格"), ("currectPrice", "现价"), ("singleSlotPrice", "单格"),
    ), "singleSlotPrice"),
    "涨幅": _Report("/getPropMarketGrowthData", "收集物资 24 小时涨幅前十", (
        ("itemPriceNow", "现价"), ("itemPriceAgo", "24小时前"), ("itemPriceChangePercentage", "涨幅"),
    )),
    "子弹盈亏": _Report("/getAmmoPLData", "子弹盈亏", (
        ("currectPrice", "现价"), ("yesterdayLowestPrice", "昨低"), ("yesterdayHighestPrice", "昨高"),
        ("yesterdayPriceOffset", "昨日理论盈亏"), ("thisWeekHighestPrice", "本周最高"), ("thisWeekPriceOffset", "本周最高位盈亏"),
    ), "yesterdayPriceOffset"),
    "活动需求": _Report("/getActivityRequiredItemInfoData", "活动物品需求", (
        ("activityName", "活动"), ("activityTime", "活动时间"), ("currectPrice", "现价"), ("activitySuggestedPrice", "理想售价"),
    )),
    "兑换利润": _Report("/getTradeAmmoData", "军需处兑换子弹利润", (
        ("requiredLevel", "等级"), ("perCount", "数量"), ("totalMaterialValue", "材料成本"), ("totalPrice", "总价值"),
        ("profit", "利润"),
    ), "profit", True),
    "制造材料": _Report("/getSwatMaterialPriceData", "高价格浮动制造材料", (
        ("currectPrice", "现价"), ("yesterdayLowestPrice", "昨低"), ("yesterdayLowestTime", "低点时间"),
        ("yesterdayHighestPrice", "昨高"), ("yesterdayHighestTime", "高点时间"),
    ), regional=True),
    "自选包": _Report("/getAmmoPackageData", "子弹自选包利润", (
        ("packageName", "自选包"), ("itemCount", "数量"), ("totalPrice", "总价值"), ("profit", "利润"),
    ), "profit", True),
}

HELP = (
    "交易行用法\n"
    "/查价 [国服|国际服] <完整物品名>\n"
    "/交易行 曲线 [国服|国际服] [3天|7天|15天] <完整物品名>\n"
    "/交易行 联想 <关键词>\n"
    f"/交易行 <{'|'.join(REPORTS)}> [国服|国际服] [关键词] [第2页]\n"
    "国际服仅适用于查价、曲线、热门、兑换利润、制造材料和自选包。"
)


def _number(value: object) -> bool:
    return type(value) in (int, float)


def _value(value: object) -> str:
    return f"{value:,.0f}" if _number(value) else str(value)


def _timestamp(value: str) -> str:
    if re.fullmatch(r"[0-9]{12}([0-9]{2})?", value):
        try:
            fmt = "%Y%m%d%H%M%S" if len(value) == 14 else "%Y%m%d%H%M"
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return value


def _rows(result: MarketResult, region: str = "") -> list[dict]:
    if isinstance(result.data, list):
        if not region:
            return result.data
    elif region in result.data:
        return result.data[region]
    raise MarketError("源站没有返回所选区服的行情数据")


def _finish(lines: list[str], result: MarketResult, source: bool, updated: str = "") -> str:
    stamp = result.version or updated
    if stamp:
        lines.append(f"数据时间：{_timestamp(stamp)}")
    if result.stale:
        lines.append("（未能刷新，以上为上次成功查询的数据）")
    if source:
        lines.append("数据来源：kkrb.net")
    return "\n".join(lines)


def _report_lines(report: _Report, rows: list[dict], region: str, keyword: str, page: int) -> list[str]:
    lines = [f"{report.title}（{region}）"]
    if keyword:
        needle = keyword.casefold()
        rows = [row for row in rows if any(needle in str(row.get(key, "")).casefold() for key in (
            report.name, "itemTypeName", "activityName", "packageName",
        ))]
    if not rows:
        return [*lines, "没有匹配的物品。"]
    if report.rank:
        def rank(row: dict) -> float:
            val = row.get(report.rank)
            return val if _number(val) else float("-inf")
        rows = sorted(rows, key=rank, reverse=True)
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    if page > pages:
        return [*lines, f"共 {pages} 页，没有第 {page} 页。"]
    offset = (page - 1) * PAGE_SIZE
    for index, row in enumerate(rows[offset:offset + PAGE_SIZE], offset + 1):
        fields = [f"{label} {_value(row[key])}" for key, label in report.fields if row.get(key) not in (None, "")]
        lines.append(f"{index}. {row.get(report.name) or '未知物品'}：{' · '.join(fields) or '暂无报价'}")
    if pages > 1:
        lines.append(f"第 {page}/{pages} 页 · 共 {len(rows)} 项")
    return lines


def _quote(result: MarketResult, region: str, name: str, period: str, history: bool, source: bool) -> str:
    match = next((row for row in _rows(result) if str(row.get("objectName", "")).casefold() == name.casefold()), None)
    if match is None:
        return _finish([f"未找到「{name}」，请使用完整物品名或用 /交易行 联想 查找。"], result, source)
    price = match.get("currectPrice")
    lines = [f"{match['objectName']}（{region}）", f"当前单价：{_value(price) if _number(price) and price > 0 else '暂无报价'}"]
    prices = match.get("prices")
    points = sorted((stamp, val) for stamp, val in prices.items() if _number(val) and val > 0) if isinstance(prices, dict) else []
    if history and points:
        low = min(val for _, val in points)
        high = max(val for _, val in points)
        steps = "▁▂▃▄▅▆▇█"
        count = min(24, len(points))
        samples = [points[i * (len(points) - 1) // max(count - 1, 1)][1] for i in range(count)]
        trend = "".join(steps[min(7, int((val - low) * 7 / (high - low)))] if high > low else steps[3] for val in samples)
        lines.extend((f"近 {period}：最低 {_value(low)} · 最高 {_value(high)}", trend))
    elif history:
        lines.append("暂无有效历史报价。")
    return _finish(lines, result, source, points[-1][0] if points else "")


async def query_market(
    client: MarketClient, text: str, *, quote: bool = False, source: bool, allow_stale: bool,
) -> str:
    parts = text.split()
    if not parts:
        return "用法：/查价 [国服|国际服] <完整物品名>" if quote else HELP
    action = "查价" if quote else parts.pop(0)
    if action == "联想":
        if not parts:
            return "用法：/交易行 联想 <关键词>"
        result = await client.get(SUGGEST_ENDPOINT, data={"itemName": " ".join(parts), "itemInfo": "true"})
        names = [row["objectName"] for row in _rows(result) if isinstance(row.get("objectName"), str)]
        return _finish(["物品联想", *names[:10]] if names else ["没有匹配的物品。"], result, source)

    region = parts.pop(0) if parts and parts[0] in REGIONS else "国服"
    if action in {"查价", "曲线"}:
        period = parts.pop(0) if action == "曲线" and parts and parts[0] in DAYS else "3天"
        if not parts and action == "查价":
            return "用法：/查价 [国服|国际服] <完整物品名>"
        if not parts or (action == "曲线" and re.fullmatch(r"\d+天", parts[0])):
            return "曲线用法：/交易行 曲线 [国服|国际服] [3天|7天|15天] <完整物品名>"
        name = " ".join(parts)
        result = await client.get(CURVE_ENDPOINT, allow_stale=allow_stale, data={
            "type": "query_all_item_curve", "time": DAYS[period], "itemName": name, "itemInfo": "true",
            "globalData": "true" if REGIONS[region] == "en" else "false",
        })
        return _quote(result, region, name, period, action == "曲线", source)

    report = REPORTS.get(action)
    if report is None:
        return HELP
    if not report.regional and region != "国服":
        return f"{report.title}没有国际服数据。"
    match = re.fullmatch(r"第([1-9][0-9]{0,5})页", parts[-1]) if parts else None
    page = 1
    if match:
        page = int(match[1])
        parts.pop()
    result = await client.get(report.endpoint, allow_stale=allow_stale)
    rows = _rows(result, REGIONS[region] if report.regional else "")
    return _finish(_report_lines(report, rows, region, " ".join(parts), page), result, source)


async def popular_choices(client: MarketClient, region: str, *, source: bool, allow_stale: bool) -> tuple[list[str], str]:
    result = await client.get(REPORTS["热门"].endpoint, allow_stale=allow_stale)
    names = [
        name for row in _rows(result, REGIONS[region])
        if isinstance(name := row.get("objectName"), str) and name.strip()
    ]
    return names, _finish([] if names else ["暂无热门物品。"], result, source)
