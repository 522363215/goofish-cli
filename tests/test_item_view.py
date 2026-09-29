"""纯函数测 item view 的参数校验 + URL。"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import goofish_cli.commands.item.view as view_module
from goofish_cli.commands.item.view import __test__ as t
from goofish_cli.core.errors import GoofishError


def test_normalize_item_id_accepts_digits():
    f = t["_normalize_item_id"]
    assert f("123456") == "123456"
    assert f(987654321) == "987654321"
    assert f("  42  ") == "42"


@pytest.mark.parametrize("bad", ["", "abc", "123abc", " ", None, "12 34"])
def test_normalize_item_id_rejects_non_digit(bad):
    with pytest.raises(GoofishError):
        t["_normalize_item_id"](bad)


def test_build_item_url():
    assert t["_build_item_url"]("12345") == "https://www.goofish.com/item?id=12345"


def test_extract_detail_keeps_zero_and_maps_page_response():
    payload = {
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "itemDO": {
                "itemId": "878974891725",
                "title": "测试商品",
                "desc": "商品描述",
                "soldPrice": "9.90",
                "wantCnt": 0,
                "collectCnt": 0,
                "browseCnt": 0,
                "itemLabelExtList": [{"propertyText": "品牌", "text": "测试品牌"}],
                "imageInfos": [{"url": "https://example.test/image.jpg"}],
            },
            "sellerDO": {"nick": "测试卖家", "sellerId": "123"},
        },
    }

    result = t["_extract_detail"](payload, "878974891725")

    assert result["title"] == "测试商品"
    assert result["price"] == "¥9.90"
    assert result["want_count"] == "0"
    assert result["browse_count"] == "0"
    assert result["brand"] == "测试品牌"
    assert result["image_urls"] == ["https://example.test/image.jpg"]


def test_extract_detail_classifies_api_error():
    result = t["_extract_detail"](
        {"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]},
        "878974891725",
    )

    assert result["error"] == "mtop-response-error"
    assert result["error_code"] == "FAIL_SYS_SESSION_EXPIRED"


def test_request_item_id_reads_page_post_body():
    class Request:
        method = "POST"
        post_data = 'data=%7B%22itemId%22%3A%22878974891725%22%7D'

    class Response:
        request = Request()

    assert t["_request_item_id"](Response()) == "878974891725"


def test_published_at_prefers_detail_display_field_and_falls_back_to_epoch():
    assert t["_published_at"]({"GMT_CREATE_DATE_KEY": "2026-09-22 08:43:40", "gmtCreate": 0}) == "2026-09-22 08:43:40"
    assert t["_published_at"]({"gmtCreate": 1790037820000}) == "2026-09-22 08:43:40"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_cookies, expected", [(False, []), (True, None)])
async def test_run_cookie_policy_is_anonymous_by_default(monkeypatch, with_cookies, expected):
    captured = {}

    class Page:
        def on(self, *_args):
            return None

        def remove_listener(self, *_args):
            return None

        async def goto(self, *_args, **_kwargs):
            raise RuntimeError("stop before navigation")

    @asynccontextmanager
    async def fake_page(**kwargs):
        captured.update(kwargs)
        yield Page()

    monkeypatch.setattr(view_module, "goofish_page", fake_page)
    with pytest.raises(RuntimeError, match="stop before navigation"):
        await view_module._run("123456", with_cookies=with_cookies)
    assert captured["cookies"] == expected
