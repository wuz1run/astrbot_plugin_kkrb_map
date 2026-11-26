"""KKRB 交易行：按需刷新、共用访客会话，并尊重源站拒绝与限流。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any

import httpx

from .kkrb import BASE_URL

MARKET_PAGE = "/?viewpage=view%2Fmarket%2Fall_item_price_curve"
SUGGEST_ENDPOINT = "/getIPCSuggestions"
CURVE_ENDPOINT = "/getQueryItemPriceCurveData"
CACHE_SECONDS = 30 * 60


class MarketError(RuntimeError):
    """可显示给用户的行情错误。"""


class _SourceRefusal(MarketError):
    def __init__(self, message: str, pause: float = float("inf")) -> None:
        super().__init__(message)
        self.pause = pause


def _retry_seconds(header: str) -> float:
    try:
        seconds = int(header)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return CACHE_SECONDS
    return max(seconds, CACHE_SECONDS)


@dataclass(slots=True)
class MarketResult:
    data: list[dict[str, Any]] | dict[str, list[dict[str, Any]]]
    version: str
    stale: bool = False


class MarketClient:
    def __init__(self, timeout: float) -> None:
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
        self._client: httpx.AsyncClient | None = None
        self._ready = False
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[tuple, tuple[float, MarketResult]] = OrderedDict()
        self._next_attempt: dict[str, float] = {}
        self._last_error: dict[str, str] = {}
        self._blocked_until = 0.0
        self._blocked_reason = ""
        self._query_quota_exhausted = False

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._ready = False

    @staticmethod
    def _check_http(response: httpx.Response) -> None:
        status = response.status_code
        if status == 429:
            raise _SourceRefusal("源站限流，已暂停刷新", _retry_seconds(response.headers.get("Retry-After", "")))
        if status in (401, 403, 451) or response.is_redirect:
            raise _SourceRefusal(f"源站拒绝交易行访问（HTTP {status}），已停止刷新")
        if not response.is_success:
            raise MarketError(f"源站暂时不可用（HTTP {status}）")

    async def _post(self, client: httpx.AsyncClient, endpoint: str, data: dict | None = None) -> dict:
        csrf = client.cookies.get("csrf_token")
        if not csrf:
            raise _SourceRefusal("源站未提供正常访客会话，已停止刷新")
        response = await client.post(endpoint, data=data, headers={"X-CSRF-Token": csrf})
        self._check_http(response)
        try:
            payload = response.json()
        except ValueError as exc:
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise _SourceRefusal("源站未返回交易行接口数据，已停止刷新") from exc
            raise MarketError("源站行情响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise MarketError("源站行情响应格式已变更")
        code = payload.get("code")
        if code in (-101, -103, 303) or (endpoint in ("/getMenu", "/checkUAStatus") and code != 1):
            raise _SourceRefusal(f"源站未允许交易行访问（业务码 {code}），已停止刷新")
        if code != 1:
            if endpoint == CURVE_ENDPOINT and payload.get("remainingQueries") == 0:
                self._query_quota_exhausted = True
                raise MarketError("源站单物品查询额度已用完")
            raise MarketError(f"源站未返回行情（业务码 {code}）")
        return payload

    async def _fetch(self, endpoint: str, data: dict | None) -> MarketResult:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"User-Agent": "astrbot_plugin_kkrb_map/1.1", "Accept": "application/json, text/html"},
                timeout=self._timeout,
                trust_env=False,
            )
        client = self._client
        try:
            if not self._ready:
                self._check_http(await client.get(MARKET_PAGE))
                await self._post(client, "/getMenu", {"globalData": "false"})
                await self._post(client, "/checkUAStatus")
                self._ready = True
            payload = await self._post(client, endpoint, data)
        except httpx.RequestError as exc:
            raise MarketError(f"无法连接源站（{type(exc).__name__}）") from exc
        if endpoint == CURVE_ENDPOINT and payload.get("remainingQueries") == 0:
            self._query_quota_exhausted = True
        rows = payload.get("data")
        groups = rows.values() if isinstance(rows, dict) else (rows,)
        if any(not isinstance(group, list) or any(not isinstance(row, dict) for row in group) for group in groups):
            raise MarketError("源站行情数据格式已变更")
        return MarketResult(rows, str(payload.get("version") or ""))

    async def get(self, endpoint: str, *, data: dict | None = None, allow_stale: bool = True) -> MarketResult:
        key = endpoint, tuple(sorted(data.items())) if data else ()
        async with self._lock:
            now = monotonic()
            entry = self._cache.get(key)
            cached = None
            if entry is not None:
                updated, cached = entry
                self._cache.move_to_end(key)
                if now - updated < CACHE_SECONDS:
                    return cached

            reason = ""
            if now < self._blocked_until:
                reason = self._blocked_reason
            elif endpoint == CURVE_ENDPOINT and self._query_quota_exhausted:
                reason = "源站单物品查询额度已用完，已停止刷新"
            elif now < self._next_attempt.get(endpoint, 0.0):
                reason = self._last_error.get(endpoint, "刷新间隔为 30 分钟，请稍后再试")
            if reason:
                if cached is not None and allow_stale and endpoint != SUGGEST_ENDPOINT:
                    return MarketResult(cached.data, cached.version, stale=True)
                raise MarketError(reason)

            self._next_attempt[endpoint] = now + CACHE_SECONDS
            try:
                result = await self._fetch(endpoint, data)
            except MarketError as exc:
                self._last_error[endpoint] = str(exc)
                if isinstance(exc, _SourceRefusal):
                    self._blocked_until = monotonic() + exc.pause
                    self._blocked_reason = str(exc)
                if cached is not None and allow_stale and endpoint != SUGGEST_ENDPOINT:
                    return MarketResult(cached.data, cached.version, stale=True)
                raise
            self._last_error.pop(endpoint, None)
            self._cache[key] = monotonic(), result
            if len(self._cache) > 40:
                self._cache.popitem(last=False)
            return result
