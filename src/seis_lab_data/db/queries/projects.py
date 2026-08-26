import logging

import shapely
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import (
    func,
    or_,
    select,
)

from ...constants import ProjectStatus
from ...schemas import (
    filters as filter_schemas,
    identifiers,
)
from .. import models
from .common import _get_total_num_records

logger = logging.getLogger(__name__)


def _build_project_statement(
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
):
    statement = select(models.Project)
    if en_name_filter:
        statement = statement.where(
            models.Project.name["en"].astext.ilike(f"%{en_name_filter}%")
        )
    if pt_name_filter:
        statement = statement.where(
            models.Project.name["pt"].astext.ilike(f"%{pt_name_filter}%")
        )
    if spatial_intersect is not None:
        statement = statement.where(
            or_(
                func.ST_Intersects(
                    models.Project.bbox_4326,
                    func.ST_GeomFromText(spatial_intersect.wkt, 4326),
                ),
                models.Project.bbox_4326.is_(None),
            )
        )
    if temporal_extent is not None:
        if temporal_extent.begin is not None:
            statement = statement.where(
                or_(
                    models.Project.temporal_extent_begin >= temporal_extent.begin,
                    models.Project.temporal_extent_begin.is_(None),
                )
            )
        if temporal_extent.end is not None:
            statement = statement.where(
                or_(
                    models.Project.temporal_extent_end <= temporal_extent.end,
                    models.Project.temporal_extent_end.is_(None),
                )
            )
    return statement.order_by(
        models.Project.temporal_extent_end.desc().nullslast()
    ).order_by(models.Project.temporal_extent_begin.desc().nullslast())


async def _exec_project_list(
    session: AsyncSession,
    statement,
    limit: int,
    offset: int,
    include_total: bool,
) -> tuple[list[models.Project], int | None]:
    items = (await session.exec(statement.offset(offset).limit(limit))).all()
    num_total = (
        await _get_total_num_records(session, statement) if include_total else None
    )
    return items, num_total


async def list_published_projects(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
) -> tuple[list[models.Project], int | None]:
    """Produces a paginated and filterable listing of public projects."""
    statement = _build_project_statement(
        en_name_filter, pt_name_filter, spatial_intersect, temporal_extent
    ).where(models.Project.status == ProjectStatus.PUBLISHED)
    limit = page_size
    offset = page_size * (page - 1)
    return await _exec_project_list(session, statement, limit, offset, include_total)


async def list_projects(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    only_internal: bool = False,
) -> tuple[list[models.Project], int | None]:
    """Produces a paginated and filterable listing of all projects.

    Intended for registered users.
    """
    statement = _build_project_statement(
        en_name_filter, pt_name_filter, spatial_intersect, temporal_extent
    )
    if only_internal:
        statement = statement.where(models.Project.status != ProjectStatus.PUBLISHED)
    limit = page_size
    offset = page_size * (page - 1)
    return await _exec_project_list(session, statement, limit, offset, include_total)


async def collect_all_projects(
    session: AsyncSession,
) -> list[models.Project]:
    _, num_total = await list_projects(session, page_size=1, include_total=True)
    items, _ = await list_projects(session, page_size=num_total, include_total=False)
    return items


async def get_project(
    session: AsyncSession,
    project_id: "identifiers.ProjectId",
) -> models.Project | None:
    return await session.get(models.Project, project_id)


async def get_project_by_english_name(
    session: AsyncSession,
    english_name: str,
) -> models.Project | None:
    statement = select(models.Project).where(
        models.Project.name["en"].astext == english_name
    )
    return (await session.exec(statement)).first()
