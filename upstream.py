"""仅供本插件访问上游的 HTTP 客户端。"""

import httpx


class ProxyConfigError(ValueError):
    """可直接展示且不包含代理地址或凭据的配置错误。"""


def source_client(*, base_url: str, headers: dict, timeout: httpx.Timeout, proxy: str | None) -> httpx.AsyncClient:
    transport = None
    if proxy is not None:
        if not proxy:
            raise ProxyConfigError("已开启上游代理，请填写代理地址")
        try:
            url = httpx.URL(proxy)
            if url.scheme not in ("http", "socks5") or not url.host or url.raw_path != b"/" or url.fragment:
                raise ValueError
            transport = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(url), trust_env=False)
        except (ValueError, httpx.InvalidURL) as exc:
            raise ProxyConfigError("代理地址无效，请填写 http://主机:端口 或 socks5://主机:端口") from exc
        except ImportError as exc:
            raise ProxyConfigError("代理依赖未安装，请重新安装插件依赖") from exc

    return httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=timeout, transport=transport,
        trust_env=False, follow_redirects=False,
    )
