import logging

import shapely
from sqlalchemy.orm import aliased, selectinload
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import (
    exists,
    func,
    or_,
    select,
)

from ...constants import (
    AssetType,
    SurveyRelatedRecordStatus,
)
from ...db import models
from ...schemas import (
    identifiers,
    filters as filter_schemas,
)
from .common import _get_total_num_records

logger = logging.getLogger(__name__)


def _apply_survey_related_record_filters(
    *,
    statement,
    survey_mission_ids: list[identifiers.SurveyMissionId] | None = None,
    project_id: identifiers.ProjectId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
):
    """Apply the common survey-related record search filters to a statement.

    Shared by statement builders that select full records and by those
    that only need matching ids (e.g. for bulk-update operations).
    """
    if record_ids:
        statement = statement.where(models.SurveyRelatedRecord.id.in_(record_ids))
    if en_name_filter:
        statement = statement.where(
            models.SurveyRelatedRecord.name["en"].astext.ilike(f"%{en_name_filter}%")
        )
    if pt_name_filter:
        statement = statement.where(
            models.SurveyRelatedRecord.name["pt"].astext.ilike(f"%{pt_name_filter}%")
        )
    if spatial_intersect is not None:
        statement = statement.where(
            or_(
                func.ST_Intersects(
                    models.SurveyRelatedRecord.bbox_4326,
                    func.ST_GeomFromText(spatial_intersect.wkt, 4326),
                ),
                models.SurveyRelatedRecord.bbox_4326.is_(None),
            )
        )
    if survey_mission_ids:
        statement = statement.where(
            models.SurveyRelatedRecord.survey_mission_id.in_(survey_mission_ids)
        )
    if project_id is not None:
        # aliased so this join doesn't collide with the unaliased SurveyMission
        # join that `_restrict_to_accessible`/`_restrict_to_owned` add later
        mission = aliased(models.SurveyMission)
        statement = statement.join(
            mission, models.SurveyRelatedRecord.survey_mission_id == mission.id
        ).where(mission.project_id == project_id)
    if temporal_extent is not None:
        if temporal_extent.begin is not None:
            statement = statement.where(
                or_(
                    models.SurveyRelatedRecord.temporal_extent_begin
                    >= temporal_extent.begin,
                    models.SurveyRelatedRecord.temporal_extent_begin.is_(None),
                )
            )
        if temporal_extent.end is not None:
            statement = statement.where(
                or_(
                    models.SurveyRelatedRecord.temporal_extent_end
                    <= temporal_extent.end,
                    models.SurveyRelatedRecord.temporal_extent_end.is_(None),
                )
            )
    if dataset_category_id is not None:
        statement = statement.where(
            models.SurveyRelatedRecord.dataset_category_id == dataset_category_id
        )
    if workflow_stage_id is not None:
        statement = statement.where(
            models.SurveyRelatedRecord.workflow_stage_id == workflow_stage_id
        )
    if asset_path_fragment_filter is not None:
        statement = statement.where(
            exists(
                select(models.RecordAsset)
                .where(
                    models.RecordAsset.survey_related_record_id
                    == models.SurveyRelatedRecord.id
                )
                .where(
                    models.RecordAsset.relative_path.ilike(
                        f"%{asset_path_fragment_filter}%"
                    )
                )
            )
        )
    if asset_media_type_filter is not None:
        asset_statement = (
            select(models.RecordAsset)
            .where(
                models.RecordAsset.survey_related_record_id
                == models.SurveyRelatedRecord.id
            )
            .where(models.RecordAsset.media_type.ilike(f"%{asset_media_type_filter}%"))
        )
        if only_data_assets:
            asset_statement = asset_statement.where(
                models.RecordAsset.asset_type.any(AssetType.DATA)
            )
        statement = statement.where(exists(asset_statement))
    return statement


def _build_survey_related_record_statement(
    survey_mission_ids: list[identifiers.SurveyMissionId] | None = None,
    project_id: identifiers.ProjectId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
):
    statement = (
        select(models.SurveyRelatedRecord)
        .options(
            selectinload(models.SurveyRelatedRecord.survey_mission).selectinload(
                models.SurveyMission.project
            )
        )
        .options(selectinload(models.SurveyRelatedRecord.dataset_category))
        .options(selectinload(models.SurveyRelatedRecord.workflow_stage))
        # adding all assets too, since they will always be a small list
        .options(selectinload(models.SurveyRelatedRecord.assets))
        # also adding relationships with other records - only first order relationships are loaded, not the full tree
        .options(selectinload(models.SurveyRelatedRecord.related_to_links))
        .options(selectinload(models.SurveyRelatedRecord.subject_links))
    )
    statement = _apply_survey_related_record_filters(
        statement=statement,
        survey_mission_ids=survey_mission_ids,
        project_id=project_id,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
        asset_path_fragment_filter=asset_path_fragment_filter,
        asset_media_type_filter=asset_media_type_filter,
        record_ids=record_ids,
        dataset_category_id=dataset_category_id,
        workflow_stage_id=workflow_stage_id,
        only_data_assets=only_data_assets,
    )
    return statement.order_by(
        models.SurveyRelatedRecord.temporal_extent_end.desc().nullslast()
    ).order_by(models.SurveyRelatedRecord.temporal_extent_begin.desc().nullslast())


def _build_survey_related_record_id_statement(
    survey_mission_id: identifiers.SurveyMissionId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
):
    """Build a statement selecting only the ids of matching records.

    Intended for reuse as a subquery/executed-upfront id list (e.g. by
    bulk-update commands), where loading full records would be wasteful.
    """
    return _apply_survey_related_record_filters(
        statement=select(models.SurveyRelatedRecord.id),
        survey_mission_ids=(
            [survey_mission_id] if survey_mission_id is not None else None
        ),
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
        asset_path_fragment_filter=asset_path_fragment_filter,
        asset_media_type_filter=asset_media_type_filter,
        record_ids=record_ids,
        dataset_category_id=dataset_category_id,
        workflow_stage_id=workflow_stage_id,
        only_data_assets=only_data_assets,
    )


async def _exec_survey_related_record_list(
    session: AsyncSession,
    statement,
    limit: int,
    offset: int,
    include_total: bool,
) -> tuple[list[models.SurveyRelatedRecord], int | None]:
    items = (await session.exec(statement.offset(offset).limit(limit))).all()
    num_total = (
        await _get_total_num_records(session, statement) if include_total else None
    )
    return items, num_total


async def list_published_survey_related_records(
    session: AsyncSession,
    survey_mission_ids: list[identifiers.SurveyMissionId] | None = None,
    project_id: identifiers.ProjectId | None = None,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
) -> tuple[list[models.SurveyRelatedRecord], int | None]:
    statement = _build_survey_related_record_statement(
        survey_mission_ids=survey_mission_ids,
        project_id=project_id,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
        asset_path_fragment_filter=asset_path_fragment_filter,
        asset_media_type_filter=asset_media_type_filter,
        record_ids=record_ids,
        dataset_category_id=dataset_category_id,
        workflow_stage_id=workflow_stage_id,
        only_data_assets=only_data_assets,
    ).where(models.SurveyRelatedRecord.status == SurveyRelatedRecordStatus.PUBLISHED)
    limit = page_size
    offset = page_size * (page - 1)
    return await _exec_survey_related_record_list(
        session, statement, limit, offset, include_total
    )


def build_survey_related_record_id_statement(
    survey_mission_id: identifiers.SurveyMissionId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    excluded_record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
):
    """Build a statement selecting the ids of matching records, unrestricted.

    Intended for admins, who may bulk-update any matching record.
    """
    statement = _build_survey_related_record_id_statement(
        survey_mission_id,
        en_name_filter,
        pt_name_filter,
        spatial_intersect,
        temporal_extent,
        asset_path_fragment_filter,
        asset_media_type_filter,
        record_ids,
        dataset_category_id,
        workflow_stage_id,
        only_data_assets,
    )
    if excluded_record_ids:
        statement = statement.where(
            models.SurveyRelatedRecord.id.notin_(excluded_record_ids)
        )
    return statement


def build_owned_survey_related_record_id_statement(
    survey_mission_id: identifiers.SurveyMissionId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    excluded_record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
):
    """Build a statement selecting the ids of matching records a user owns.

    Intended for non-admin editors, who may only bulk-update records they
    own, or whose survey mission or project they own.
    """
    statement = _build_survey_related_record_id_statement(
        survey_mission_id,
        en_name_filter,
        pt_name_filter,
        spatial_intersect,
        temporal_extent,
        asset_path_fragment_filter,
        asset_media_type_filter,
        record_ids,
        dataset_category_id,
        workflow_stage_id,
        only_data_assets,
    )
    if excluded_record_ids:
        statement = statement.where(
            models.SurveyRelatedRecord.id.notin_(excluded_record_ids)
        )
    return statement


async def count_survey_related_records_matching(
    session: AsyncSession, ids_statement
) -> int:
    """Count how many records an id-only statement matches.

    Intended for the bulk-update id builders above, to show a user how many
    records a pending bulk update would affect without materializing ids.
    """
    return await _get_total_num_records(session, ids_statement)


async def list_survey_related_records(
    session: AsyncSession,
    survey_mission_ids: list[identifiers.SurveyMissionId] | None = None,
    project_id: identifiers.ProjectId | None = None,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Geometry | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    only_internal: bool = False,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
    only_data_assets: bool = True,
) -> tuple[list[models.SurveyRelatedRecord], int | None]:
    """Return all records. Intended for admin use."""
    statement = _build_survey_related_record_statement(
        survey_mission_ids=survey_mission_ids,
        project_id=project_id,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
        asset_path_fragment_filter=asset_path_fragment_filter,
        asset_media_type_filter=asset_media_type_filter,
        record_ids=record_ids,
        dataset_category_id=dataset_category_id,
        workflow_stage_id=workflow_stage_id,
        only_data_assets=only_data_assets,
    )
    if only_internal:
        statement = statement.where(
            models.SurveyRelatedRecord.status != SurveyRelatedRecordStatus.PUBLISHED
        )
    limit = page_size
    offset = page_size * (page - 1)
    return await _exec_survey_related_record_list(
        session, statement, limit, offset, include_total
    )


async def get_survey_related_record(
    session: AsyncSession,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
) -> models.SurveyRelatedRecord | None:
    statement = (
        select(models.SurveyRelatedRecord)
        .where(models.SurveyRelatedRecord.id == survey_related_record_id)
        .options(
            selectinload(models.SurveyRelatedRecord.survey_mission).selectinload(
                models.SurveyMission.project
            )
        )
        .options(selectinload(models.SurveyRelatedRecord.dataset_category))
        .options(selectinload(models.SurveyRelatedRecord.workflow_stage))
        # adding all assets too, since they will always be a small list
        .options(selectinload(models.SurveyRelatedRecord.assets))
        # also adding relationships with other records - only first order relationships are loaded, not the full tree
        .options(selectinload(models.SurveyRelatedRecord.related_to_links))
        .options(selectinload(models.SurveyRelatedRecord.subject_links))
    )
    return (await session.exec(statement)).first()


async def get_survey_related_record_by_english_name(
    session: AsyncSession,
    survey_mission_id: identifiers.SurveyMissionId,
    english_name: str,
) -> models.SurveyRelatedRecord | None:
    statement = (
        select(models.SurveyRelatedRecord)
        .where(models.SurveyRelatedRecord.survey_mission_id == survey_mission_id)
        .where(models.SurveyRelatedRecord.name["en"].astext == english_name)
        .options(
            selectinload(models.SurveyRelatedRecord.survey_mission).selectinload(
                models.SurveyMission.project
            )
        )
        .options(selectinload(models.SurveyRelatedRecord.dataset_category))
        .options(selectinload(models.SurveyRelatedRecord.workflow_stage))
        # adding all assets too, since they will always be a small list
        .options(selectinload(models.SurveyRelatedRecord.assets))
        # also adding relationships with other records - only first order relationships are loaded, not the full tree
        .options(selectinload(models.SurveyRelatedRecord.related_to_links))
        .options(selectinload(models.SurveyRelatedRecord.subject_links))
    )
    return (await session.exec(statement)).first()


async def list_survey_related_record_related_to_records(
    session: AsyncSession,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    limit: int = 20,
    offset: int = 0,
) -> list[tuple[dict, models.SurveyRelatedRecord]]:
    """Return records in which the input id is a subject of a relation.

    Returned records are those which are related to the input id.
    """
    statement = (
        select(
            models.SurveyRelatedRecordSelfLink.relation,
            models.SurveyRelatedRecord,
        )
        .join(
            models.SurveyRelatedRecordSelfLink,
            models.SurveyRelatedRecordSelfLink.related_to_id
            == models.SurveyRelatedRecord.id,
        )
        .where(
            models.SurveyRelatedRecordSelfLink.subject_id == survey_related_record_id
        )
        .options(
            selectinload(models.SurveyRelatedRecord.survey_mission).selectinload(
                models.SurveyMission.project
            )
        )
        .options(selectinload(models.SurveyRelatedRecord.dataset_category))
        .options(selectinload(models.SurveyRelatedRecord.workflow_stage))
    )
    return (await session.exec(statement.offset(offset).limit(limit))).all()


async def list_survey_related_record_subject_records(
    session: AsyncSession,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    limit: int = 20,
    offset: int = 0,
) -> list[tuple[dict, models.SurveyRelatedRecord]]:
    """Return records which are subjects in a relation with the input id.

    Returned records are those which are subjects in a relation where the input id is involved
    """
    statement = (
        select(
            models.SurveyRelatedRecordSelfLink.relation,
            models.SurveyRelatedRecord,
        )
        .join(
            models.SurveyRelatedRecordSelfLink,
            models.SurveyRelatedRecordSelfLink.subject_id
            == models.SurveyRelatedRecord.id,
        )
        .where(
            models.SurveyRelatedRecordSelfLink.related_to_id == survey_related_record_id
        )
        .options(
            selectinload(models.SurveyRelatedRecord.survey_mission).selectinload(
                models.SurveyMission.project
            )
        )
        .options(selectinload(models.SurveyRelatedRecord.dataset_category))
        .options(selectinload(models.SurveyRelatedRecord.workflow_stage))
    )
    return (await session.exec(statement.offset(offset).limit(limit))).all()
