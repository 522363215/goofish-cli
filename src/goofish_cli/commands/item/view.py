"""item view — 浏览器视角的商品详情。对标 OpenCLI `xianyu/item.js`。

`item get` 已经在 CLI 里直签调 `mtop.taobao.idle.pc.detail` v1.0，字段浅抽（5 项）。
`item view` 在真实 Chrome 的商品页里复用页面自身发出的 detail API 响应，
好处：

1. **字段完整**：直接按 OpenCLI 的提取器从 `data.itemDO / data.sellerDO / itemLabelExtList`
   抽 20+ 字段（description / want_count / browse_count / 成色 / 品牌 / image_urls /
   seller_score / reply_ratio_24h 等）。
2. **抗风控兜底**：CLI 直签偶尔遇到 x5sec / h5_token 失效，浏览器路径作为备份链路。

保留 `item get`（快且不需要 Chrome），两条路并存。用户根据场景选。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

from goofish_cli.core import Strategy, command
from goofish_cli.core.browser import goofish_page
from goofish_cli.core.errors import AuthRequiredError, GoofishError


def _normalize_item_id(value: Any) -> str:
    s = str(value or "").strip()
    if not re.fullmatch(r"\d+", s):
        raise GoofishError(f"item_id 必须是数字，收到：{value!r}")
    return s


def _build_item_url(item_id: str) -> str:
    return f"https://www.goofish.com/item?id={item_id}"


_DETAIL_API = "mtop.taobao.idle.pc.detail"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _string_or_empty(value: Any) -> str:
    return "" if value is None else str(value)


def _published_at(item: dict[str, Any]) -> str:
    value = item.get("GMT_CREATE_DATE_KEY")
    if value:
        return _clean(value)
    timestamp = item.get("gmtCreate")
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return ""


def _extract_detail(payload: dict[str, Any], item_id: str) -> dict[str, Any]:
    """Extract the response triggered by the page itself.

    The page already requests the detail API during navigation.  Issuing a second
    ``window.lib.mtop.request`` after navigation can be rejected with TIMEOUT/ABORT,
    so extraction is deliberately kept separate from transport.
    """
    ret = payload.get("ret") or []
    ret_text = " | ".join(str(value) for value in ret) if isinstance(ret, list) else str(ret)
    ret_code = _clean(ret_text).split("::", 1)[0] if ret_text else ""
    if ret_code and ret_code != "SUCCESS":
        return {
            "error": "mtop-response-error",
            "error_code": ret_code,
            "error_message": _clean(ret_text),
        }

    data = payload.get("data") or {}
    item = data.get("itemDO") or {}
    seller = data.get("sellerDO") or {}
    labels = item.get("itemLabelExtList") if isinstance(item.get("itemLabelExtList"), list) else []
    label_map = {
        _clean(label.get("propertyText")): label.get("text", "")
        for label in labels
        if isinstance(label, dict)
    }
    images = [
        entry.get("url")
        for entry in (item.get("imageInfos") or [])
        if isinstance(entry, dict) and entry.get("url")
    ]

    price = _clean("¥" + _string_or_empty(item.get("soldPrice") or item.get("defaultPrice") or ""))
    if price == "¥":
        price = ""

    return {
        "item_id": _clean(item.get("itemId") or item_id),
        "title": _clean(item.get("title")),
        "description": _clean(item.get("desc")),
        "price": price,
        "stock": _string_or_empty(item.get("quantity")),
        "published_at": _published_at(item),
        "original_price": _clean(item.get("originalPrice")),
        "want_count": _string_or_empty(item.get("wantCnt")),
        "collect_count": _string_or_empty(item.get("collectCnt")),
        "browse_count": _string_or_empty(item.get("browseCnt")),
        "status": _clean(item.get("itemStatusStr")),
        "condition": _clean(label_map.get("成色")),
        "brand": _clean(label_map.get("品牌")),
        "category": _clean(label_map.get("分类")),
        "location": _clean(seller.get("publishCity") or seller.get("city")),
        "seller_name": _clean(seller.get("nick") or seller.get("uniqueName")),
        "seller_id": _string_or_empty(seller.get("sellerId")),
        "seller_score": _clean(seller.get("xianyuSummary")),
        "reply_ratio_24h": _clean(seller.get("replyRatio24h")),
        "reply_interval": _clean(seller.get("replyInterval")),
        "seller_url": (
            f"https://www.goofish.com/personal?userId={seller.get('sellerId')}"
            if seller.get("sellerId") else ""
        ),
        "image_count": str(len(images)),
        "image_urls": images,
    }


def _request_item_id(response: Any) -> str:
    """Read the item ID from a page-generated mtop POST body."""
    try:
        encoded = parse_qs(response.request.post_data or "").get("data", [""])[0]
        body = json.loads(encoded)
    except (TypeError, json.JSONDecodeError, IndexError):
        return ""
    return _string_or_empty(body.get("itemId")) if isinstance(body, dict) else ""


async def _run(item_id: str, *, with_cookies: bool = False) -> dict[str, Any]:
    url = _build_item_url(item_id)
    # Public item detail works without authentication.  Avoid injecting a stale
    # cookies.json by default; callers can explicitly opt in when needed.
    cookies = None if with_cookies else []
    async with goofish_page(cookies=cookies) as page:
        loop = asyncio.get_running_loop()
        detail_future: asyncio.Future[Any] = loop.create_future()

        def on_response(response: Any) -> None:
            if (
                _DETAIL_API in response.url
                and response.request.method == "POST"
                and _request_item_id(response) == item_id
                and not detail_future.done()
            ):
                detail_future.set_result(response)

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            try:
                response = await asyncio.wait_for(asyncio.shield(detail_future), timeout=15)
            except TimeoutError as exc:
                raise GoofishError("商品详情页未收到 detail 接口响应") from exc
            try:
                payload = await response.json()
            except Exception as exc:  # noqa: BLE001
                raise GoofishError("商品详情接口响应不是合法 JSON") from exc
            result = _extract_detail(payload, item_id)
        finally:
            page.remove_listener("response", on_response)

    if not isinstance(result, dict):
        raise GoofishError("商品详情页返回结构非预期")

    if result.get("item_id") != item_id and not result.get("error"):
        raise GoofishError(
            f"商品详情接口返回 ID 不匹配：请求 {item_id}，返回 {result.get('item_id') or '空值'}"
        )

    err = result.get("error")
    if err == "blocked":
        raise GoofishError("商品详情页被验证码/安全验证拦截，触发风控")
    if err == "mtop-not-ready":
        raise GoofishError("页面 window.lib.mtop 未就绪（等待超时），页面加载异常")

    # auth 判定统一走 mtop 返回的 error_code/error_message 里的 SESSION_EXPIRED，
    # 不再依赖 body 文案正则（"登录后" 在页脚也会出现，误报多）。
    err_code = str(result.get("error_code") or "")
    err_msg = str(result.get("error_message") or "")
    if re.search(r"FAIL_SYS_SESSION_EXPIRED|SESSION_EXPIRED", err_code + " " + err_msg):
        raise AuthRequiredError("mtop 返回 session 过期：" + (err_msg or err_code))
    if err:
        raise GoofishError(f"mtop 调用失败 [{err_code or err}]: {err_msg or '无详情'}")
    if not result.get("title"):
        raise GoofishError(f"未拿到商品 {item_id} 的标题，接口返回为空")

    return result


@command(
    namespace="item",
    name="view",
     description="浏览器视角查看商品详情（默认匿名，字段比 item get 更全）",
    strategy=Strategy.COOKIE,
    columns=[
        "item_id", "title", "price", "condition", "brand", "location",
        "seller_name", "want_count", "browse_count", "stock", "published_at",
    ],
)
def view(item_id: str, *, with_cookies: bool = False) -> dict[str, Any]:
    normalized = _normalize_item_id(item_id)
    # MCP / table 渲染依赖 dict；image_urls 是列表（JSON 里保留，table 里压成长度）
    result = asyncio.run(_run(normalized, with_cookies=with_cookies))
    result["_image_urls_json"] = json.dumps(result.get("image_urls", []), ensure_ascii=False)
    return result


__test__ = {
    "_normalize_item_id": _normalize_item_id,
    "_build_item_url": _build_item_url,
    "_extract_detail": _extract_detail,
    "_request_item_id": _request_item_id,
    "_published_at": _published_at,
}
