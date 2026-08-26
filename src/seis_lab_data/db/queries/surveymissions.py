import shapely
from sqlalchemy.orm import selectinload
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import (
    func,
    or_,
    select,
)

from ...constants import SurveyMissionStatus
from ...db import models
from ...schemas import (
    identifiers,
    filters as filter_schemas,
)
from .common import _get_total_num_records


def _build_survey_mission_statement(
    project_id: identifiers.ProjectId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
):
    statement = select(models.SurveyMission).options(
        selectinload(models.SurveyMission.project)
    )
    if en_name_filter:
        statement = statement.where(
            models.SurveyMission.name["en"].astext.ilike(f"%{en_name_filter}%")
        )
    if pt_name_filter:
        statement = statement.where(
            models.SurveyMission.name["pt"].astext.ilike(f"%{pt_name_filter}%")
        )
    if spatial_intersect is not None:
        statement = statement.where(
            or_(
                func.ST_Intersects(
                    models.SurveyMission.bbox_4326,
                    func.ST_GeomFromText(spatial_intersect.wkt, 4326),
                ),
                models.SurveyMission.bbox_4326.is_(None),
            )
        )
    if project_id is not None:
        statement = statement.where(models.SurveyMission.project_id == project_id)
    if temporal_extent is not None:
        if temporal_extent.begin is not None:
            statement = statement.where(
                or_(
                    models.SurveyMission.temporal_extent_begin >= temporal_extent.begin,
                    models.SurveyMission.temporal_extent_begin.is_(None),
                )
            )
        if temporal_extent.end is not None:
            statement = statement.where(
                or_(
                    models.SurveyMission.temporal_extent_end <= temporal_extent.end,
                    models.SurveyMission.temporal_extent_end.is_(None),
                )
            )
    return statement.order_by(
        models.SurveyMission.temporal_extent_end.desc().nullslast()
    ).order_by(models.SurveyMission.temporal_extent_begin.desc().nullslast())


async def _exec_survey_mission_list(
    session: AsyncSession,
    statement,
    limit: int,
    offset: int,
    include_total: bool,
) -> tuple[list[models.SurveyMission], int | None]:
    items = (await session.exec(statement.offset(offset).limit(limit))).all()
    num_total = (
        await _get_total_num_records(session, statement) if include_total else None
    )
    return items, num_total


async def list_published_survey_missions(
    session: AsyncSession,
    project_id: identifiers.ProjectId | None = None,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
) -> tuple[list[models.SurveyMission], int | None]:
    statement = _build_survey_mission_statement(
        project_id, en_name_filter, pt_name_filter, spatial_intersect, temporal_extent
    ).where(models.SurveyMission.status == SurveyMissionStatus.PUBLISHED)
    limit = page_size
    offset = page_size * (page - 1)
    return await _exec_survey_mission_list(
        session, statement, limit, offset, include_total
    )


async def list_survey_missions(
    session: AsyncSession,
    project_id: identifiers.ProjectId | None = None,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    only_internal: bool = False,
) -> tuple[list[models.SurveyMission], int | None]:
    """Return all survey missions regardless of status. Intended for admin use."""
    statement = _build_survey_mission_statement(
        project_id, en_name_filter, pt_name_filter, spatial_intersect, temporal_extent
    )
    if only_internal:
        statement = statement.where(
            models.SurveyMission.status != SurveyMissionStatus.PUBLISHED
        )
    limit = page_size
    offset = page_size * (page - 1)
    return await _exec_survey_mission_list(
        session, statement, limit, offset, include_total
    )


async def collect_all_project_survey_missions(
    session: AsyncSession,
    project_id: identifiers.ProjectId,
) -> list[models.SurveyMission]:
    statement = _build_survey_mission_statement(project_id=project_id)
    return (await session.exec(statement)).all()


async def get_survey_mission(
    session: AsyncSession,
    survey_mission_id: identifiers.SurveyMissionId,
) -> models.SurveyMission | None:
    statement = (
        select(models.SurveyMission)
        .where(models.SurveyMission.id == survey_mission_id)
        .options(selectinload(models.SurveyMission.project))
    )
    return (await session.exec(statement)).first()


async def get_survey_mission_by_english_name(
    session: AsyncSession,
    project_id: identifiers.ProjectId,
    english_name: str,
) -> models.SurveyMission | None:
    statement = (
        select(models.SurveyMission)
        .where(models.SurveyMission.name["en"].astext == english_name)
        .where(models.SurveyMission.project_id == project_id)
        .options(selectinload(models.SurveyMission.project))
    )
    return (await session.exec(statement)).first()


async def get_survey_mission_by_path(
    session: AsyncSession,
    project_id: identifiers.ProjectId,
    mission_path: str,
) -> models.SurveyMission | None:
    statement = (
        select(models.SurveyMission)
        .where(models.SurveyMission.project_id == project_id)
        .where(models.SurveyMission.relative_path == mission_path)
        .options(selectinload(models.SurveyMission.project))
    )
    return (await session.exec(statement)).first()
