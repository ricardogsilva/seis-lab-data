import logging

import pydantic
import shapely
from sqlmodel.ext.asyncio.session import AsyncSession

from .. import (
    constants,
    dispatch,
    errors,
)
from ..permissions import surveyrelatedrecords as record_permissions
from ..db import models
from ..db.commands import surveyrelatedrecords as record_commands
from ..db.queries import (
    surveymissions as mission_queries,
    surveyrelatedrecords as record_queries,
)
from ..schemas import (
    events as event_schemas,
    filters as filter_schemas,
    identifiers,
    surveyrelatedrecords as record_schemas,
    user as user_schemas,
    validation as validation_schemas,
)

logger = logging.getLogger(__name__)


async def create_survey_related_record(
    *,
    request_id: identifiers.RequestId,
    to_create: record_schemas.SurveyRelatedRecordCreate,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
):
    try:
        if not (
            survey_mission := await mission_queries.get_survey_mission(
                session, to_create.survey_mission_id
            )
        ):
            raise errors.SeisLabDataError(
                f"Survey mission with id {to_create.survey_mission_id} does not exist"
            )
        if not record_permissions.can_create_survey_related_record(
            initiator, survey_mission
        ):
            raise errors.SeisLabDataError(
                "User is not allowed to create a survey-related record."
            )
        survey_record = await record_commands.create_survey_related_record(
            session, to_create
        )
        record_id = identifiers.SurveyRelatedRecordId(survey_record.id)
        validated_record = await validate_survey_related_record(
            request_id=request_id,
            survey_related_record_id=record_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        if all(
            (
                validated_record.validation_result.get("is_valid"),
                (validated_record.survey_mission.validation_result or {}).get(
                    "is_valid"
                ),
            )
        ):
            await change_survey_related_record_status(
                request_id=request_id,
                target_status=constants.SurveyRelatedRecordStatus.PUBLISHED,
                survey_related_record_id=record_id,
                initiator=initiator,
                session=session,
                event_dispatcher=event_dispatcher,
            )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                resource_type=constants.ResourceType.RECORD,
                resource_id=None,
                request_id=request_id,
                modification=constants.ResourceModification.CREATED,
                succeeded=False,
                initiator=initiator.id,
                details=str(err),
            )
        )
        raise

    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            resource_type=constants.ResourceType.RECORD,
            resource_id=str(survey_record.id),
            request_id=request_id,
            modification=constants.ResourceModification.CREATED,
            succeeded=True,
            initiator=initiator.id,
        )
    )
    return validated_record


async def change_survey_related_record_status(
    *,
    request_id: identifiers.RequestId,
    target_status: constants.SurveyRelatedRecordStatus,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyRelatedRecord | None:
    try:
        if (
            survey_related_record := await record_queries.get_survey_related_record(
                session, survey_related_record_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey-related record with id {survey_related_record_id} does not exist."
            )
        if survey_related_record.status == target_status:
            logger.info(
                f"Survey-related record status is already "
                f"set to {target_status} - nothing to do"
            )
            return survey_related_record
        if not record_permissions.can_change_survey_related_record_status(
            initiator, survey_related_record
        ):
            raise errors.SeisLabDataError(
                "User is not allowed to change survey-related record's status."
            )
        updated_survey_related_record = (
            await record_commands.set_survey_related_record_status(
                session,
                identifiers.SurveyRelatedRecordId(survey_related_record.id),
                target_status,
            )
        )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceStatusChangedEvent(
                request_id=request_id,
                initiator=initiator.id,
                resource_type=constants.ResourceType.RECORD,
                resource_id=str(survey_related_record_id),
                succeeded=False,
                new_status=None,
                details=str(err),
            )
        )
        return None
    await event_dispatcher(
        event_schemas.ResourceStatusChangedEvent(
            request_id=request_id,
            initiator=initiator.id,
            resource_type=constants.ResourceType.RECORD,
            resource_id=str(survey_related_record_id),
            succeeded=True,
            new_status=updated_survey_related_record.status,
        )
    )
    return updated_survey_related_record


async def validate_survey_related_record(
    *,
    request_id: identifiers.RequestId,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyRelatedRecord:
    try:
        if (
            survey_related_record := await record_queries.get_survey_related_record(
                session, survey_related_record_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey-related record with id {survey_related_record_id} does not exist."
            )
        if not record_permissions.can_validate_survey_related_record(
            initiator, survey_related_record
        ):
            raise errors.SeisLabDataError(
                "User is not allowed to validate survey-related record."
            )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ValidationEvent(
                resource_type=constants.ResourceType.RECORD,
                resource_id=str(survey_related_record_id),
                request_id=request_id,
                modification=constants.ValidationStage.ENDED,
                succeeded=False,
                is_valid=False,
                initiator=initiator.id,
                details=str(err),
            )
        )
        raise

    validation_errors = []
    try:
        await change_survey_related_record_status(
            request_id=request_id,
            target_status=constants.SurveyRelatedRecordStatus.UNDER_VALIDATION,
            survey_related_record_id=survey_related_record_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        validation_schemas.ValidSurveyRelatedRecord.model_validate(
            survey_related_record, from_attributes=True
        )
    except pydantic.ValidationError as err:
        for error in err.errors():
            validation_errors.append(
                {
                    "name": ".".join(str(i) for i in error["loc"]),
                    "message": error["msg"],
                    "type_": error["type"],
                }
            )
        await record_commands.update_survey_related_record_validation_result(
            session,
            survey_related_record,
            validation_result={
                "is_valid": False,
                "errors": validation_errors,
            },
        )
    else:
        await record_commands.update_survey_related_record_validation_result(
            session,
            survey_related_record,
            validation_result={"is_valid": True, "errors": None},
        )
    finally:
        await event_dispatcher(
            event_schemas.ValidationEvent(
                resource_type=constants.ResourceType.RECORD,
                resource_id=str(survey_related_record_id),
                request_id=request_id,
                modification=constants.ValidationStage.ENDED,
                succeeded=True,
                is_valid=not validation_errors,
                initiator=initiator.id,
                details=str(validation_errors),
            )
        )
    await change_survey_related_record_status(
        request_id=request_id,
        target_status=constants.SurveyRelatedRecordStatus.DRAFT,
        survey_related_record_id=survey_related_record_id,
        initiator=initiator,
        session=session,
        event_dispatcher=event_dispatcher,
    )
    return survey_related_record


async def delete_survey_related_record(
    *,
    request_id: identifiers.RequestId,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> None:
    try:
        if (
            survey_record := await record_queries.get_survey_related_record(
                session, survey_related_record_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey-related record with id {survey_related_record_id!r} does not exist."
            )
        if not record_permissions.can_delete_survey_related_record(
            initiator, survey_record
        ):
            raise errors.SeisLabDataError(
                "User is not allowed to delete survey-related record."
            )
        if (
            mission_status := survey_record.survey_mission.status
        ) != constants.SurveyMissionStatus.DRAFT:
            raise errors.SeisLabDataError(
                f"Cannot update survey-related record because parent survey "
                f"mission's status is {mission_status}"
            )
        if (
            project_status := survey_record.survey_mission.project.status
        ) != constants.ProjectStatus.DRAFT:
            raise errors.SeisLabDataError(
                f"Cannot update survey-related record because parent project's "
                f"status is {project_status}"
            )
        parent_id = survey_record.survey_mission_id
        await record_commands.delete_survey_related_record(
            session, survey_related_record_id
        )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.RECORD,
                resource_id=str(survey_related_record_id),
                request_id=request_id,
                modification=constants.ResourceModification.DELETED,
                succeeded=False,
                details=str(err),
            )
        )
        return None
    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            initiator=initiator.id,
            resource_type=constants.ResourceType.RECORD,
            resource_id=str(survey_related_record_id),
            parent_resource_id=str(parent_id),
            request_id=request_id,
            modification=constants.ResourceModification.DELETED,
            succeeded=True,
        )
    )


async def list_survey_related_records(
    session: AsyncSession,
    initiator: user_schemas.User | None,
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
) -> tuple[list[models.SurveyRelatedRecord], int | None]:
    kwargs = dict(
        survey_mission_ids=survey_mission_ids,
        project_id=project_id,
        page=page,
        page_size=page_size,
        include_total=include_total,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
        asset_path_fragment_filter=asset_path_fragment_filter,
        dataset_category_id=dataset_category_id,
        workflow_stage_id=workflow_stage_id,
        asset_media_type_filter=asset_media_type_filter,
        record_ids=record_ids,
    )
    if initiator is None:
        return await record_queries.list_published_survey_related_records(
            session, **kwargs
        )
    else:
        kwargs.update(only_internal=only_internal)
        return await record_queries.list_survey_related_records(session, **kwargs)


async def get_survey_related_record(
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    initiator: user_schemas.User | None,
    session: AsyncSession,
) -> (
    tuple[
        models.SurveyRelatedRecord,
        list[tuple[str, models.SurveyRelatedRecord]],
        list[tuple[str, models.SurveyRelatedRecord]],
    ]
    | None
):
    record = await record_queries.get_survey_related_record(
        session, survey_related_record_id
    )
    if record is None:
        return None

    record_id = identifiers.SurveyRelatedRecordId(record.id)
    records_related_to = (
        await record_queries.list_survey_related_record_related_to_records(
            session, record_id
        )
    )
    records_subject_for = (
        await record_queries.list_survey_related_record_subject_records(
            session, record_id
        )
    )

    if record.status == constants.SurveyRelatedRecordStatus.PUBLISHED:
        return record, records_related_to, records_subject_for

    error_msg = f"User not allowed to read survey-related record {record_id!r}."

    if initiator is None:
        raise errors.UserNotAllowedError(error_msg)

    if not record_permissions.can_read_private_survey_related_record(initiator, record):
        raise errors.UserNotAllowedError(error_msg)

    return record, records_related_to, records_subject_for


async def update_survey_related_record(
    *,
    request_id: identifiers.RequestId,
    survey_related_record_id: identifiers.SurveyRelatedRecordId,
    to_update: record_schemas.SurveyRelatedRecordUpdate,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyRelatedRecord:
    try:
        if (
            survey_related_record := await record_queries.get_survey_related_record(
                session, survey_related_record_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey-related id {survey_related_record_id!r} does not exist."
            )
        if not record_permissions.can_update_survey_related_record(
            initiator, survey_related_record
        ):
            raise errors.SeisLabDataError(
                "User not allowed to update survey-related record."
            )
        await record_commands.update_survey_related_record(
            session, survey_related_record, to_update
        )
        validated_record = await validate_survey_related_record(
            request_id=request_id,
            survey_related_record_id=survey_related_record_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        if all(
            (
                validated_record.validation_result.get("is_valid"),
                (validated_record.survey_mission.validation_result or {}).get(
                    "is_valid"
                ),
            )
        ):
            await change_survey_related_record_status(
                request_id=request_id,
                target_status=constants.SurveyRelatedRecordStatus.PUBLISHED,
                survey_related_record_id=survey_related_record_id,
                initiator=initiator,
                session=session,
                event_dispatcher=event_dispatcher,
            )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.RECORD,
                resource_id=str(survey_related_record_id),
                request_id=request_id,
                modification=constants.ResourceModification.UPDATED,
                succeeded=False,
                details=str(err),
            )
        )
        raise
    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            initiator=initiator.id,
            resource_type=constants.ResourceType.RECORD,
            resource_id=str(survey_related_record_id),
            request_id=request_id,
            modification=constants.ResourceModification.UPDATED,
            succeeded=True,
        )
    )
    return validated_record


async def bulk_update_survey_related_records(
    *,
    request_id: identifiers.RequestId,
    to_update: record_schemas.SurveyRelatedRecordBulkUpdate,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
    selected: list[identifiers.SurveyRelatedRecordId] | None = None,
    excluded_record_ids: list[identifiers.SurveyRelatedRecordId] | None = None,
    survey_mission_id: identifiers.SurveyMissionId | None = None,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
    asset_path_fragment_filter: str | None = None,
    asset_media_type_filter: str | None = None,
    dataset_category_id: identifiers.DatasetCategoryId | None = None,
    workflow_stage_id: identifiers.WorkflowStageId | None = None,
) -> int | None:
    """Bulk-update either a manually selected set of records or all matching a filter.

    `selected` and the filter/`excluded_record_ids` arguments are mutually
    exclusive ways of specifying which records to update, mirroring the two
    selection modes offered by the UI: an explicit set of chosen records, or
    "everything matching the current search, except what was excluded".

    `survey_mission_id` is an optional additional scope - omit it to bulk-update
    across all missions a user may access.
    """
    try:
        if not record_permissions.can_bulk_update_survey_related_records(initiator):
            raise errors.UserNotAllowedError(
                "User not allowed to bulk-update survey-related records."
            )
        if selected is not None:
            updated_count = await record_commands.bulk_update_manually_selected_records(
                session,
                to_update,
                selected,
                survey_mission_id=survey_mission_id,
            )
        else:
            updated_count = await record_commands.bulk_update_filtered_records(
                session,
                to_update,
                excluded_record_ids=excluded_record_ids,
                survey_mission_id=survey_mission_id,
                en_name_filter=en_name_filter,
                pt_name_filter=pt_name_filter,
                spatial_intersect=spatial_intersect,
                temporal_extent=temporal_extent,
                asset_path_fragment_filter=asset_path_fragment_filter,
                asset_media_type_filter=asset_media_type_filter,
                dataset_category_id=dataset_category_id,
                workflow_stage_id=workflow_stage_id,
            )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.BulkResourceModificationEvent(
                initiator=initiator.id,
                request_id=request_id,
                resource_type=constants.ResourceType.RECORD,
                modification=constants.BulkResourceModification.UPDATED,
                succeeded=False,
                affected_count=0,
                details=str(err),
            )
        )
        return None

    await event_dispatcher(
        event_schemas.BulkResourceModificationEvent(
            initiator=initiator.id,
            request_id=request_id,
            resource_type=constants.ResourceType.RECORD,
            modification=constants.BulkResourceModification.UPDATED,
            succeeded=True,
            affected_count=updated_count,
        )
    )
    return updated_count
