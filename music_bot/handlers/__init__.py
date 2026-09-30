from aiogram import Router

from . import commands, inline


def build_router() -> Router:
    router = Router(name="root")
    # Inline first: it is the hot path and must not be shadowed by anything.
    router.include_router(inline.router)
    router.include_router(commands.router)
    return router


__all__ = ["build_router"]
