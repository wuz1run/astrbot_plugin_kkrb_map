"""三角洲行动每日地图密码与交易行查询插件。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from PIL import Image

from .cards import PASSWORD_TEMPLATE, QQ_OFFICIAL, command_menu, password_card
from .kkrb import KkrbError, MapPassword, fetch_passwords, render, today
from .market import MarketClient, MarketError
from .market_reports import REGIONS, REPORTS, popular_choices, query_market

PLUGIN_NAME = "astrbot_plugin_kkrb_map"


@star.register(PLUGIN_NAME, "Wuzirun", "查询三角洲行动每日地图密码与交易行行情（数据来自 kkrb.net）", "v1.2.0", "")
class KkrbMapPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}
        self.data_dir: Path = star.StarTools.get_data_dir(PLUGIN_NAME)
        self.cache_file: Path = self.data_dir / "cache.json"
        self._lock = asyncio.Lock()
        self._market = MarketClient(timeout=float(self.config.get("request_timeout", 15)))

    async def terminate(self) -> None:
        await self._market.close()

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _read_cache(self) -> dict | None:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not raw.get("date") or not raw.get("items"):
            return None
        return raw

    def _write_cache(self, date: str, items: list[MapPassword]) -> None:
        payload = {
            "date": date,
            "items": [item.to_dict() for item in items],
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"[kkrb_map] 写入缓存失败：{exc}")

    async def _get_today(self) -> tuple[list[MapPassword], str, bool]:
        """返回 (数据, 日期, 是否来自旧缓存)。当天已抓过就不再联网。"""
        date = today()
        cached = self._read_cache()
        if cached and cached.get("date") == date:
            return [MapPassword.from_dict(x) for x in cached["items"]], date, False

        async with self._lock:
            # 等锁期间可能已被其它会话刷新
            cached = self._read_cache()
            if cached and cached.get("date") == date:
                return [MapPassword.from_dict(x) for x in cached["items"]], date, False

            try:
                items = await fetch_passwords(
                    timeout=float(self.config.get("request_timeout", 15)),
                    proxy=(str(self.config.get("proxy") or "").strip() or None),
                )
            except KkrbError as exc:
                logger.warning(f"[kkrb_map] 抓取失败：{exc}")
                if cached and self.config.get("fallback_on_error", True):
                    return (
                        [MapPassword.from_dict(x) for x in cached["items"]],
                        str(cached["date"]),
                        True,
                    )
                raise

            self._write_cache(date, items)
            return items, date, False

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("地图密码", alias={"三角洲密码"})
    async def map_password(self, event: AstrMessageEvent):
        if not self.config.get("command_enable", True):
            return
        try:
            items, date, stale = await self._get_today()
        except KkrbError as exc:
            yield event.plain_result(f"地图密码获取失败：{exc}")
            return

        source = bool(self.config.get("include_source", True))
        if items:
            data, options = password_card(items, date, stale, source)
            try:
                path = await asyncio.wait_for(
                    self.html_render(PASSWORD_TEMPLATE, data, return_url=False, options=options),
                    timeout=15,
                )
                with Image.open(path) as image:
                    image.verify()
            except Exception as exc:
                logger.warning(f"[kkrb_map] 密码卡片渲染失败，改发文字：{exc}")
            else:
                yield event.image_result(path)
                return

        text = render(items, date, source=source)
        if stale:
            text += "\n（源站暂时不可用，返回的是上次成功抓取的结果）"
        yield event.plain_result(text).use_t2i(False)

    def _menu(self, event: AstrMessageEvent, title: str, rows: list[list[tuple[str, str]]], note: str = ""):
        interactive = event.get_platform_name() in QQ_OFFICIAL
        prefixes = self.context.get_config(event.unified_msg_origin).get("wake_prefix", ["/"])
        prefix = next((value for value in prefixes if value), "/")
        text = command_menu(title, rows, prefix=prefix, interactive=interactive, note=note)
        return event.plain_result(text).use_t2i(False).use_markdown(interactive)

    @filter.command("三角洲", alias={"三角洲菜单"})
    async def delta_menu(self, event: AstrMessageEvent):
        entries = []
        if self.config.get("command_enable", True):
            entries.append(("地图密码", "地图密码"))
        if self.config.get("market_enable", False):
            entries.append(("交易行", "交易行"))
        note = "" if entries else "地图密码和交易行均未启用。"
        yield self._menu(event, "三角洲行动 · 快捷菜单", [entries] if entries else [], note)

    def _trade_menu(self, event: AstrMessageEvent):
        reports = [(label, f"交易行 {label}") for label in REPORTS]
        rows = [
            [("选热门物品", "交易行 选物品"), ("自定义查价 …", "查价 "), ("名称联想 …", "交易行 联想 ")],
            *(reports[i:i + 3] for i in range(0, len(reports), 3)),
            [("返回主菜单", "三角洲")],
        ]
        return self._menu(event, "交易行 · 快捷菜单", rows)

    async def _item_menu(self, event: AstrMessageEvent, argument: str):
        region = argument or "国服"
        if region not in REGIONS:
            return event.plain_result("用法：/交易行 选物品 [国服|国际服]")
        try:
            names, note = await popular_choices(
                self._market, region,
                source=bool(self.config.get("include_source", True)),
                allow_stale=bool(self.config.get("fallback_on_error", True)),
            )
        except MarketError as exc:
            return event.plain_result(f"热门物品获取失败：{exc}")
        rows = [
            [(f"{name} · 查价", f"查价 {region} {name}"), ("3 天曲线", f"交易行 曲线 {region} 3天 {name}")]
            for name in names
        ]
        other = "国际服" if region == "国服" else "国服"
        rows.append([(f"切换{other}", f"交易行 选物品 {other}"), ("返回交易行", "交易行")])
        return self._menu(event, f"交易行 · 热门选物（{region}）", rows, note)

    async def _market_reply(self, text: str, *, quote: bool = False) -> str:
        if not self.config.get("market_enable", False):
            return "交易行未启用，请在获得数据使用许可后开启 market_enable。"
        try:
            return await query_market(
                self._market, text, quote=quote,
                source=bool(self.config.get("include_source", True)),
                allow_stale=bool(self.config.get("fallback_on_error", True)),
            )
        except MarketError as exc:
            return f"交易行查询失败：{exc}"

    @filter.command("交易行")
    async def market_query(self, event: AstrMessageEvent):
        command = event.get_message_str().split(maxsplit=1)
        text = command[1] if len(command) == 2 else ""
        if self.config.get("market_enable", False):
            if not text:
                yield self._trade_menu(event)
                return
            action = text.split(maxsplit=1)
            if action[0] == "选物品":
                yield await self._item_menu(event, action[1] if len(action) == 2 else "")
                return
        yield event.plain_result(await self._market_reply(text))

    @filter.command("查价")
    async def market_quote(self, event: AstrMessageEvent):
        command = event.get_message_str().split(maxsplit=1)
        yield event.plain_result(await self._market_reply(command[1] if len(command) == 2 else "", quote=True))