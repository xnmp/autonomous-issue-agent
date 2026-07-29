"""Synchronous test façade over HTTPX's in-process asynchronous ASGI transport.

Starlette's ``TestClient`` owns a cross-thread AnyIO portal. Some constrained
CI/sandbox environments allow the thread to start but never schedule work onto
that portal, so a request hangs before the ASGI app sees it. Keeping the test
transport in one event loop tests the same HTTP contract without that unrelated
threading dependency.
"""

from __future__ import annotations

import asyncio

import httpx


class ASGITestClient:
    __test__ = False
    def __init__(self, app, *, headers=None, base_url: str = "http://testserver"):
        self._app = app
        self._headers = headers or {}
        self._base_url = base_url

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport,
                headers=self._headers,
                base_url=self._base_url,
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)
