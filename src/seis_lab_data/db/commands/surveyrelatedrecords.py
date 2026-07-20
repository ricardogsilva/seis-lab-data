import logging
import uuid

import shapely
from sqlalchemy import (
    Boolean,
    bindparam,
    column,
    delete,
    func,
    select,
    true,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import (
    ARRAY,
    JSONB,
    UUID as PG_UUID,
    insert as pg_insert,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from ... import errors
from ...constants import SurveyRelatedRecordStatus
from ...schemas import (
    filters as filter_schemas,
    identifiers,
    surveyrelatedrecords as record_schemas,
)
from .. import models
from ..queries import surveyrelatedrecords as record_queries
from .common import get_bbox_4326_for_db

logger = logging.getLogger(__name__)


async def create_survey_related_record(
    session: AsyncSession,
    to_create: record_schemas.SurveyRelatedRecordCreate,
) -> models.SurveyRelatedRecord:
    survey_record = models.SurveyRelatedRecord(
        **to_create.model_dump(exclude={"assets", "bbox_4326", "related_records"}),
        bbox_4326=(
            get_bbox_4326_for_db(bbox)
            if (bbox := to_create.bbox_4326) is not None
            else bbox
        ),
    )
    # need to ensure english name is unique for combination of mission and record
    if await record_queries.get_survey_related_record_by_english_name(
        session,
        identifiers.SurveyMissionId(to_create.survey_mission_id),
        to_create.name.en,
    ):
        raise errors.SeisLabDataError(
            f"There is already a survey-related record with english name {to_create.name.en!r} for "
            f"the same survey mission."
        )
    session.add(survey_record)
    for asset_to_create in to_create.assets:
        db_asset = models.RecordAsset(
            **asset_to_create.model_dump(),
            survey_related_record_id=survey_record.id,
        )
        session.add(db_asset)
    for related in to_create.related_records:
        db_related = models.SurveyRelatedRecordSelfLink(
            subject_id=survey_record.id,
            related_to_id=related.related_record_id,
            relation=related.relationship.model_dump(),
        )
        session.add(db_related)
    await session.commit()
    await session.refresh(survey_record)
    return await record_queries.get_survey_related_record(session, to_create.id)


async def delete_survey_related_record(
    session: AsyncSession,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
) -> None:
    if survey_record := (
        await record_queries.get_survey_related_record(
            session, survey_related_record_id
        )
    ):
        await session.delete(survey_record)
        await session.commit()
    else:
        raise errors.SeisLabDataError(
            f"Survey-related record with id {survey_related_record_id!r} does not exist."
        )


async def _replace_related_records_for_subjects(
    session: AsyncSession,
    subject_ids: list[uuid.UUID],
    related_records: list[record_schemas.RelatedRecordCreate],
) -> None:
    """Make every given subject record's outgoing relations match `related_records`.

    Implemented as a bulk delete of stale links followed by a bulk upsert,
    rather than per-record diffing, so it scales to large subject sets.
    """
    proposed_related_ids = [r.related_record_id for r in related_records]
    delete_stmt = delete(models.SurveyRelatedRecordSelfLink).where(
        models.SurveyRelatedRecordSelfLink.subject_id.in_(subject_ids)
    )
    if proposed_related_ids:
        delete_stmt = delete_stmt.where(
            models.SurveyRelatedRecordSelfLink.related_to_id.notin_(
                proposed_related_ids
            )
        )
    await session.execute(delete_stmt)

    if not related_records:
        return

    proposed = values(
        column("related_to_id", PG_UUID(as_uuid=True)),
        column("relation", JSONB),
        name="proposed_related_record",
    ).data(
        [(r.related_record_id, r.relationship.model_dump()) for r in related_records]
    )
    subject_ids_table = (
        func.unnest(
            bindparam(
                "subject_ids", value=subject_ids, type_=ARRAY(PG_UUID(as_uuid=True))
            )
        )
        .table_valued("subject_id")
        .render_derived()
    )
    select_pairs = select(
        subject_ids_table.c.subject_id,
        proposed.c.related_to_id,
        proposed.c.relation,
    ).select_from(subject_ids_table.join(proposed, true()))
    upsert_stmt = pg_insert(models.SurveyRelatedRecordSelfLink).from_select(
        ["subject_id", "related_to_id", "relation"], select_pairs
    )
    upsert_stmt = upsert_stmt.on_conflict_do_update(
        index_elements=["subject_id", "related_to_id"],
        set_={"relation": upsert_stmt.excluded.relation},
    )
    await session.execute(upsert_stmt)


async def _apply_bulk_update_to_matched_records(
    session: AsyncSession,
    to_update: record_schemas.SurveyRelatedRecordBulkUpdate,
    matched_ids: list[uuid.UUID],
) -> int:
    if not matched_ids:
        return 0

    values_to_set = to_update.model_dump(
        exclude={"bbox_4326", "related_records"}, exclude_unset=True
    )
    # bbox_4326 is handled separately (like elsewhere in this module) because
    # invalid geometries are silently dropped rather than stored as-is.
    if "bbox_4326" in to_update.model_fields_set:
        values_to_set["bbox_4326"] = (
            get_bbox_4326_for_db(bbox)
            if (bbox := to_update.bbox_4326) is not None
            else None
        )

    if values_to_set:
        result = await session.execute(
            update(models.SurveyRelatedRecord)
            .where(models.SurveyRelatedRecord.id.in_(matched_ids))
            .values(**values_to_set)
        )
        updated_count = result.rowcount
    else:
        updated_count = len(matched_ids)

    if "related_records" in to_update.model_fields_set:
        await _replace_related_records_for_subjects(
            session, matched_ids, to_update.related_records
        )

    return updated_count


async def bulk_update_manually_selected_records(
    session: AsyncSession,
    to_update: record_schemas.SurveyRelatedRecordBulkUpdate,
    selected: list[identifiers.SurveyRelatedRecordId],
    user_id: identifiers.UserId,
    restrict_to_owned: bool = True,
    survey_mission_id: identifiers.SurveyMissionId | None = None,
) -> int:
    if restrict_to_owned:
        ids_statement = record_queries.build_owned_survey_related_record_id_statement(
            user_id, survey_mission_id=survey_mission_id, record_ids=selected
        )
    else:
        ids_statement = record_queries.build_survey_related_record_id_statement(
            survey_mission_id=survey_mission_id, record_ids=selected
        )
    matched_ids = (await session.exec(ids_statement)).all()
    try:
        updated_count = await _apply_bulk_update_to_matched_records(
            session, to_update, matched_ids
        )
        await session.commit()
    except Exception as err:
        await session.rollback()
        raise err
    return updated_count


async def bulk_update_filtered_records(
    session: AsyncSession,
    to_update: record_schemas.SurveyRelatedRecordBulkUpdate,
    user_id: identifiers.UserId,
    restrict_to_owned: bool = True,
    excluded_record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    survey_mission_id: identifiers.SurveyMissionId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
) -> int:
    filter_kwargs = dict(
        survey_mission_id=survey_mission_id,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
        asset_path_fragment_filter=asset_path_fragment_filter,
        excluded_record_ids=excluded_record_ids,
    )
    if restrict_to_owned:
        ids_statement = record_queries.build_owned_survey_related_record_id_statement(
            user_id, **filter_kwargs
        )
    else:
        ids_statement = record_queries.build_survey_related_record_id_statement(
            **filter_kwargs
        )
    matched_ids = (await session.exec(ids_statement)).all()
    try:
        updated_count = await _apply_bulk_update_to_matched_records(
            session, to_update, matched_ids
        )
        await session.commit()
    except Exception as err:
        await session.rollback()
        raise err
    return updated_count


async def update_survey_related_record(
    session: AsyncSession,
    survey_related_record: models.SurveyRelatedRecord,
    to_update: record_schemas.SurveyRelatedRecordUpdate,
):
    """Update a survey-related record and its assets.

    Updating the record also means that underlying assets may be
    added, updated, or removed.
    """
    logger.debug(f"{to_update=}")
    for key, value in to_update.model_dump(
        exclude={"bbox_4326", "assets", "related_records"}, exclude_unset=True
    ).items():
        setattr(survey_related_record, key, value)
    updated_bbox_4326 = (
        get_bbox_4326_for_db(bbox)
        if (bbox := to_update.bbox_4326) is not None
        else None
    )
    survey_related_record.bbox_4326 = updated_bbox_4326
    session.add(survey_related_record)

    for proposed_asset in to_update.assets:
        try:
            existing_asset = [
                a
                for a in survey_related_record.assets
                if identifiers.RecordAssetId(a.id) == proposed_asset.id
            ][0]
        except IndexError:  # this is a new asset that needs to be created
            db_asset = models.RecordAsset(
                **proposed_asset.model_dump(),
                survey_related_record_id=survey_related_record.id,
            )
            session.add(db_asset)
        else:  # this is an existing asset that needs to be updated
            for key, value in proposed_asset.model_dump(exclude_unset=True).items():
                setattr(existing_asset, key, value)
            session.add(existing_asset)

    proposed_asset_ids = [s.id for s in to_update.assets]
    for existing_asset in survey_related_record.assets:
        if identifiers.RecordAssetId(existing_asset.id) not in proposed_asset_ids:
            await session.delete(existing_asset)

    already_related_to = (
        await record_queries.list_survey_related_record_related_to_records(
            session, identifiers.SurveyRelatedRecordId(survey_related_record.id)
        )
    )
    logger.debug(f"{already_related_to=}")
    for proposed_related_to in to_update.related_records:
        # did a relationship to this record already exist?
        try:
            existing_relationship = [
                r
                for r in survey_related_record.related_to_links
                if r.related_to_id == proposed_related_to.related_record_id
            ][0]
        except IndexError:  # this is a new relationship that must be created
            db_relationship = models.SurveyRelatedRecordSelfLink(
                subject_id=survey_related_record.id,
                related_to_id=proposed_related_to.related_record_id,
                relation=proposed_related_to.relationship.model_dump(),
            )
            session.add(db_relationship)
        else:  # this is an existing relationship that needs to be updated
            existing_relationship.relation = (
                proposed_related_to.relationship.model_dump()
            )
            session.add(existing_relationship)

    proposed_related_to_ids = [r.related_record_id for r in to_update.related_records]
    for existing_related in survey_related_record.related_to_links:
        if (
            identifiers.SurveyRelatedRecordId(existing_related.related_to_id)
            not in proposed_related_to_ids
        ):
            await session.delete(existing_related)

    await session.commit()
    await session.refresh(survey_related_record)
    return await record_queries.get_survey_related_record(
        session, identifiers.SurveyRelatedRecordId(survey_related_record.id)
    )


async def update_survey_related_record_validation_result(
    session: AsyncSession,
    survey_related_record: models.SurveyRelatedRecord,
    validation_result: models.ValidationResult,
) -> models.SurveyRelatedRecord:
    """Unconditionally sets the survey-related record's validation result."""
    survey_related_record.validation_result = validation_result
    session.add(survey_related_record)
    await session.commit()
    await session.refresh(survey_related_record)
    return await record_queries.get_survey_related_record(
        session, identifiers.SurveyRelatedRecordId(survey_related_record.id)
    )


async def set_survey_related_record_status(
    session: AsyncSession,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    status: SurveyRelatedRecordStatus,
) -> models.SurveyRelatedRecord:
    """Unconditionally sets the survey-related record's status."""
    if (
        survey_related_record := (
            await record_queries.get_survey_related_record(
                session, survey_related_record_id
            )
        )
    ) is None:
        raise errors.SeisLabDataError(
            f"Survey-related record with id {survey_related_record_id} does not exist."
        )
    survey_related_record.status = status
    session.add(survey_related_record)
    await session.commit()
    await session.refresh(survey_related_record)
    return await record_queries.get_survey_related_record(
        session, identifiers.SurveyRelatedRecordId(survey_related_record_id)
    )


async def bulk_publish_valid_survey_related_records(
    session: AsyncSession,
    survey_mission_id: identifiers.SurveyMissionId,
) -> int:
    """Publish every already-valid, not-yet-published record of a survey mission.

    A record's own validity does not depend on its parent mission, so when the
    mission (re)becomes valid there is no need to re-validate its records -
    those already marked valid can be published directly from their stored
    validation result.
    """
    result = await session.execute(
        update(models.SurveyRelatedRecord)
        .where(models.SurveyRelatedRecord.survey_mission_id == survey_mission_id)
        .where(models.SurveyRelatedRecord.status != SurveyRelatedRecordStatus.PUBLISHED)
        .where(
            models.SurveyRelatedRecord.validation_result["is_valid"].astext.cast(
                Boolean
            )
            == true()
        )
        .values(status=SurveyRelatedRecordStatus.PUBLISHED)
    )
    await session.commit()
    return result.rowcount


async def bulk_unpublish_survey_related_records(
    session: AsyncSession,
    survey_mission_id: identifiers.SurveyMissionId,
) -> int:
    """Unpublish every published record of a survey mission.

    Used when the mission (or its parent project) becomes invalid, so its
    records don't stay published without a published parent.
    """
    result = await session.execute(
        update(models.SurveyRelatedRecord)
        .where(models.SurveyRelatedRecord.survey_mission_id == survey_mission_id)
        .where(models.SurveyRelatedRecord.status == SurveyRelatedRecordStatus.PUBLISHED)
        .values(status=SurveyRelatedRecordStatus.DRAFT)
    )
    await session.commit()
    return result.rowcount
