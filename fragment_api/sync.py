"""Blocking wrapper around :class:`fragment_api.Fragment` for scripts and sync frameworks.

Do not use inside a running event loop (e.g. aiogram handlers) — use the
async :class:`Fragment` there.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from .service import Fragment


class FragmentSync:
    def __init__(self, fragment: Fragment, loop: asyncio.AbstractEventLoop) -> None:
        self._fragment = fragment
        self._loop = loop

    @classmethod
    def create(cls, cookies: Any, **kwargs: Any) -> "FragmentSync":
        """Same arguments as :meth:`Fragment.create`."""
        loop = asyncio.new_event_loop()

        async def make() -> Fragment:
            # httpx clients must be created inside the loop that will use them
            return Fragment.create(cookies, **kwargs)

        return cls(loop.run_until_complete(make()), loop)

    @property
    def fragment(self) -> Fragment:
        return self._fragment

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fragment, name)
        if not asyncio.iscoroutinefunction(attr):
            return attr

        @functools.wraps(attr)
        def run(*args: Any, **kwargs: Any) -> Any:
            return self._loop.run_until_complete(attr(*args, **kwargs))

        return run

    def close(self) -> None:
        if not self._loop.is_closed():
            self._loop.run_until_complete(self._fragment.aclose())
            self._loop.close()

    def __enter__(self) -> "FragmentSync":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
