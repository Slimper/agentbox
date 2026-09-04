from dataclasses import dataclass

from fastapi import Query
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PageParams:
    cursor: str | None
    limit: int


def page_params(cursor: str | None = Query(None), limit: int = Query(20, ge=1, le=100)) -> PageParams:
    return PageParams(cursor=cursor, limit=limit)


async def paginate(session: AsyncSession, stmt: Select, id_column, params: PageParams) -> tuple[list, str | None]:
    if params.cursor:
        stmt = stmt.where(id_column < params.cursor)
    stmt = stmt.order_by(id_column.desc()).limit(params.limit + 1)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > params.limit:
        rows = rows[: params.limit]
        next_cursor = rows[-1].id
    return rows, next_cursor


def list_response(items: list[dict], next_cursor: str | None) -> dict:
    return {"data": items, "next_cursor": next_cursor}
