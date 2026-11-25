"""三角洲行动每日地图密码插件。

数据按北京时间每天最多抓取一次并缓存在插件数据目录，
群友发送 `/地图密码` 时直接读缓存，避免频繁请求源站。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .kkrb import KkrbError, MapPassword, fetch_passwords, render, today

PLUGIN_NAME = "astrbot_plugin_kkrb_map"


@star.register(PLUGIN_NAME, "Wuzirun", "查询三角洲行动每日地图密码（数据来自 kkrb.net）", "v1.0.1", "")
class KkrbMapPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}
        self.data_dir: Path = star.StarTools.get_data_dir(PLUGIN_NAME)
        self.cache_file: Path = self.data_dir / "cache.json"
        self._lock = asyncio.Lock()

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

        text = render(items, date, source=bool(self.config.get("include_source", True)))
        if stale:
            text += "\n（源站暂时不可用，返回的是上次成功抓取的结果）"
        yield event.plain_result(text)