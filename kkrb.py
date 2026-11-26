"""kkrb.net 每日地图密码抓取。

该站点没有公开 API：所有 AJAX 请求都依赖页面下发的
`csrf_token`（放在 cookie 里）并在请求头带 `X-CSRF-Token`，
且需要先访问页面、再调一次 `getMenu` 建立会话状态。
这里把这些握手步骤封装成一个无第三方依赖（除 httpx）的函数。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import httpx

from .upstream import ProxyConfigError, source_client

BASE_URL = "https://www.kkrb.net"
PAGE_PATH = "/?viewpage=view%2Foverview"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
CN_TZ = dt.timezone(dt.timedelta(hours=8))

# 站点前端的 mapNameLists，代码 -> 中文名
MAP_NAMES: dict[str, str] = {
    "db": "零号大坝",
    "cgxg": "长弓溪谷",
    "bks": "巴克什",
    "htjd": "航天基地",
    "cxjy": "潮汐监狱",
    "az3": "AZ3",
    "az3r6": "AZ3彩六联动房",
}


class KkrbError(RuntimeError):
    """抓取失败（网络、鉴权握手或返回码异常）。"""


class _SourceRefusal(KkrbError):
    """站点明确拒绝，不重试。"""


@dataclass(slots=True)
class MapPassword:
    code: str
    name: str
    password: str
    updated: str
    overridden: bool

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "password": self.password,
            "updated": self.updated,
            "overridden": self.overridden,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "MapPassword":
        return cls(
            code=str(raw["code"]),
            name=str(raw["name"]),
            password=str(raw["password"]),
            updated=str(raw.get("updated", "")),
            overridden=bool(raw.get("overridden", False)),
        )


def today() -> str:
    """按北京时间取日期，作为每日缓存键。"""
    return dt.datetime.now(CN_TZ).strftime("%Y-%m-%d")


def _format_updated(raw: str) -> str:
    """站点给的是 yyyyMMddHHmmss，转成可读时间；解析失败就原样返回。"""
    try:
        return dt.datetime.strptime(raw, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def _parse(payload: dict) -> list[MapPassword]:
    data = payload.get("data") or {}
    bd_data = data.get("bdData") or {}
    if not isinstance(bd_data, dict) or not bd_data:
        raise KkrbError("返回内容里没有 bdData，站点可能改版")

    items: list[MapPassword] = []
    for code, item in bd_data.items():
        if not isinstance(item, dict):
            continue
        items.append(
            MapPassword(
                code=str(code),
                name=MAP_NAMES.get(str(code), str(code)),
                password=str(item.get("password", "")),
                updated=_format_updated(str(item.get("updated", ""))),
                overridden=bool(item.get("overridden", False)),
            )
        )

    order = {code: idx for idx, code in enumerate(MAP_NAMES)}
    items.sort(key=lambda x: order.get(x.code, len(order)))
    return items


def _describe(exc: BaseException) -> str:
    """httpx 的超时异常 str() 常为空，这里补上类型名便于排查。"""
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


async def _fetch_once(timeout: float, proxy: str | None = None) -> list[MapPassword]:
    headers = {
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL + PAGE_PATH,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        client = source_client(
            base_url=BASE_URL, headers=headers, proxy=proxy,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        )
    except ProxyConfigError as exc:
        raise _SourceRefusal(str(exc)) from exc
    async with client:
        try:
            page = await client.get(PAGE_PATH)
            page.raise_for_status()
        except httpx.HTTPError as exc:
            raise KkrbError(f"访问站点失败：{_describe(exc)}") from exc

        csrf = client.cookies.get("csrf_token")
        if not csrf:
            raise KkrbError("未拿到 csrf_token，站点鉴权方式可能已变更")

        auth = {"X-CSRF-Token": csrf}

        # 站点页面会先调 getMenu，会话状态不建立时 getOVData 恒返回 -101。
        try:
            menu = await client.post("/getMenu", data={"globalData": "false"}, headers=auth)
            menu.raise_for_status()
            menu_payload = menu.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KkrbError(f"初始化会话失败：{exc}") from exc
        if menu_payload.get("code") != 1:
            raise _SourceRefusal(f"初始化会话被拒绝：{menu_payload.get('msg', menu_payload)}")

        try:
            resp = await client.post("/getOVData", data={"globalData": "false"}, headers=auth)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KkrbError(f"请求地图密码失败：{exc}") from exc

        if payload.get("code") != 1:
            if payload.get("code") in (-101, -103, 303):
                raise _SourceRefusal(f"站点拒绝地图密码查询（业务码 {payload['code']}）")
            raise KkrbError(
                f"站点返回异常：code={payload.get('code')} msg={payload.get('msg')}"
            )
        return _parse(payload)


async def fetch_passwords(
    timeout: float = 15.0, retries: int = 1, proxy: str | None = None
) -> list[MapPassword]:
    """抓取今日地图密码，失败时用新会话重试一次。"""
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            return await _fetch_once(timeout, proxy)
        except _SourceRefusal:
            raise
        except KkrbError as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.HTTPStatusError):
                if cause.response.status_code in (401, 403, 429, 451) or cause.response.is_redirect:
                    raise
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def render(items: list[MapPassword], date: str, source: bool = True) -> str:
    """把结果排成适合群聊发送的纯文本。"""
    lines = [f"三角洲行动 地图密码（{date}）"]
    for item in items:
        lines.append(f"{item.name}：{item.password}")
    if items and items[0].updated:
        lines.append(f"更新时间：{items[0].updated}")
    if source:
        lines.append("数据来源：kkrb.net")
    return "\n".join(lines)