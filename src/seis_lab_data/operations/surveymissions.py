import logging

import pydantic
import shapely
from sqlmodel.ext.asyncio.session import AsyncSession

from .. import (
    constants,
    dispatch,
    errors,
)
from ..db import models
from ..db.commands import surveymissions as mission_commands
from ..db.commands import surveyrelatedrecords as record_commands
from ..db.queries import (
    projects as project_queries,
    surveymissions as mission_queries,
)
from ..permissions import surveymissions as mission_permissions
from ..schemas import (
    events as event_schemas,
    filters as filter_schemas,
    identifiers,
    surveymissions as survey_mission_schemas,
    user as user_schemas,
    validation as validation_schemas,
)

logger = logging.getLogger(__name__)


async def create_survey_mission(
    *,
    request_id: identifiers.RequestId,
    to_create: survey_mission_schemas.SurveyMissionCreate,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyMission | None:
    try:
        if not (
            project := await project_queries.get_project(session, to_create.project_id)
        ):
            raise errors.SeisLabDataError(
                f"Project with id {to_create.project_id} does not exist"
            )
        if not mission_permissions.can_create_survey_mission(initiator, project):
            raise errors.SeisLabDataError(
                f"User {initiator!r} is not allowed to create a survey mission."
            )
        survey_mission = await mission_commands.create_survey_mission(
            session, to_create
        )
        mission_id = identifiers.SurveyMissionId(survey_mission.id)
        validated_mission = await validate_survey_mission(
            request_id=request_id,
            survey_mission_id=mission_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        if all(
            (
                validated_mission.validation_result.get("is_valid"),
                (validated_mission.project.validation_result or {}).get("is_valid"),
            )
        ):
            await change_survey_mission_status(
                request_id=request_id,
                target_status=constants.SurveyMissionStatus.PUBLISHED,
                survey_mission_id=mission_id,
                initiator=initiator,
                session=session,
                event_dispatcher=event_dispatcher,
            )
    except errors.SeisLabDataError as err:
        logger.error(str(err))
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                resource_type=constants.ResourceType.MISSION,
                resource_id=None,
                request_id=request_id,
                modification=constants.ResourceModification.CREATED,
                succeeded=False,
                initiator=initiator.id,
                details=str(err),
            )
        )
        return None
    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            resource_type=constants.ResourceType.MISSION,
            resource_id=str(survey_mission.id),
            request_id=request_id,
            modification=constants.ResourceModification.CREATED,
            succeeded=True,
            initiator=initiator.id,
        )
    )
    return survey_mission


async def change_survey_mission_status(
    *,
    request_id: identifiers.RequestId,
    target_status: constants.SurveyMissionStatus,
    survey_mission_id: identifiers.SurveyMissionId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyMission | None:
    try:
        if (
            survey_mission := await mission_queries.get_survey_mission(
                session, survey_mission_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey mission with id {survey_mission_id} does not exist."
            )
        if not mission_permissions.can_change_survey_mission_status(
            initiator, survey_mission
        ):
            raise errors.SeisLabDataError(
                "User is not allowed to change survey mission status."
            )
        if survey_mission.status == target_status:
            logger.info(
                f"Survey mission status is already set to {target_status} - nothing to do"
            )
            return survey_mission
        updated_survey_mission = await mission_commands.set_survey_mission_status(
            session, identifiers.SurveyMissionId(survey_mission.id), target_status
        )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceStatusChangedEvent(
                request_id=request_id,
                initiator=initiator.id,
                resource_type=constants.ResourceType.MISSION,
                resource_id=str(survey_mission_id),
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
            resource_type=constants.ResourceType.MISSION,
            resource_id=str(survey_mission_id),
            succeeded=True,
            new_status=updated_survey_mission.status,
        )
    )
    return updated_survey_mission


async def validate_survey_mission(
    *,
    request_id: identifiers.RequestId,
    survey_mission_id: identifiers.SurveyMissionId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyMission:
    try:
        if (
            survey_mission := await mission_queries.get_survey_mission(
                session, survey_mission_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey mission with id {survey_mission_id} does not exist."
            )
        if not mission_permissions.can_validate_survey_mission(
            initiator, survey_mission
        ):
            raise errors.SeisLabDataError(
                "User is not allowed to validate survey mission."
            )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ValidationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.MISSION,
                resource_id=str(survey_mission_id),
                request_id=request_id,
                modification=constants.ValidationStage.ENDED,
                succeeded=False,
                is_valid=False,
                details=str(err),
            )
        )
        raise

    validation_errors = []
    try:
        await change_survey_mission_status(
            request_id=request_id,
            target_status=constants.SurveyMissionStatus.UNDER_VALIDATION,
            survey_mission_id=survey_mission_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        validation_schemas.ValidSurveyMission.model_validate(
            survey_mission, from_attributes=True
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
        await mission_commands.update_survey_mission_validation_result(
            session,
            survey_mission,
            validation_result={
                "is_valid": False,
                "errors": validation_errors,
            },
        )
    else:
        await mission_commands.update_survey_mission_validation_result(
            session,
            survey_mission,
            validation_result={"is_valid": True, "errors": None},
        )
    finally:
        await event_dispatcher(
            event_schemas.ValidationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.MISSION,
                resource_id=str(survey_mission_id),
                request_id=request_id,
                modification=constants.ValidationStage.ENDED,
                succeeded=True,
                is_valid=not validation_errors,
                details=str(validation_errors),
            )
        )
    await change_survey_mission_status(
        request_id=request_id,
        target_status=constants.SurveyMissionStatus.DRAFT,
        survey_mission_id=survey_mission_id,
        initiator=initiator,
        session=session,
        event_dispatcher=event_dispatcher,
    )
    return survey_mission


async def update_survey_mission(
    request_id: identifiers.RequestId,
    survey_mission_id: identifiers.SurveyMissionId,
    to_update: survey_mission_schemas.SurveyMissionUpdate,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.SurveyMission | None:
    try:
        if (
            survey_mission := await mission_queries.get_survey_mission(
                session, survey_mission_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey mission {survey_mission_id!r} does not exist."
            )
        if not mission_permissions.can_update_survey_mission(initiator, survey_mission):
            raise errors.SeisLabDataError("User not allowed to update survey mission.")
        await mission_commands.update_survey_mission(session, survey_mission, to_update)
        validated_mission = await validate_survey_mission(
            request_id=request_id,
            survey_mission_id=survey_mission_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        if all(
            (
                validated_mission.validation_result.get("is_valid"),
                (validated_mission.project.validation_result or {}).get("is_valid"),
            )
        ):
            if validated_mission.status != constants.SurveyMissionStatus.PUBLISHED:
                await change_survey_mission_status(
                    request_id=request_id,
                    target_status=constants.SurveyMissionStatus.PUBLISHED,
                    survey_mission_id=survey_mission_id,
                    initiator=initiator,
                    session=session,
                    event_dispatcher=event_dispatcher,
                )
            published_count = (
                await record_commands.bulk_publish_valid_survey_related_records(
                    session, survey_mission_id
                )
            )
            if published_count:
                await event_dispatcher(
                    event_schemas.BulkResourceModificationEvent(
                        initiator=initiator.id,
                        request_id=request_id,
                        resource_type=constants.ResourceType.RECORD,
                        modification=constants.BulkResourceModification.UPDATED,
                        succeeded=True,
                        affected_count=published_count,
                    )
                )
        else:
            unpublished_count = (
                await record_commands.bulk_unpublish_survey_related_records(
                    session, survey_mission_id
                )
            )
            if unpublished_count:
                await event_dispatcher(
                    event_schemas.BulkResourceModificationEvent(
                        initiator=initiator.id,
                        request_id=request_id,
                        resource_type=constants.ResourceType.RECORD,
                        modification=constants.BulkResourceModification.UPDATED,
                        succeeded=True,
                        affected_count=unpublished_count,
                    )
                )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                resource_type=constants.ResourceType.MISSION,
                resource_id=str(survey_mission_id),
                request_id=request_id,
                modification=constants.ResourceModification.UPDATED,
                succeeded=False,
                initiator=initiator.id,
                details=str(err),
            )
        )
        raise

    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            resource_type=constants.ResourceType.MISSION,
            resource_id=str(survey_mission_id),
            request_id=request_id,
            modification=constants.ResourceModification.UPDATED,
            succeeded=True,
            initiator=initiator.id,
        )
    )
    return validated_mission


async def delete_survey_mission(
    *,
    request_id: identifiers.RequestId,
    survey_mission_id: identifiers.SurveyMissionId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> None:
    try:
        if (
            survey_mission := await mission_queries.get_survey_mission(
                session, survey_mission_id
            )
        ) is None:
            raise errors.SeisLabDataError(
                f"Survey mission with id {survey_mission_id!r} does not exist."
            )
        if not mission_permissions.can_delete_survey_mission(initiator, survey_mission):
            raise errors.SeisLabDataError(
                "User is not allowed to delete survey missions."
            )
        if survey_mission.status != constants.SurveyMissionStatus.DRAFT:
            raise errors.SeisLabDataError(
                f"Cannot delete survey mission with status {survey_mission.status}"
            )
        if survey_mission.project.status != constants.ProjectStatus.DRAFT:
            raise errors.SeisLabDataError(
                f"Cannot delete survey mission because parent project's status "
                f"is {survey_mission.project.status}"
            )
        parent_id = survey_mission.project_id
        await mission_commands.delete_survey_mission(session, survey_mission_id)
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                resource_type=constants.ResourceType.MISSION,
                resource_id=str(survey_mission_id),
                request_id=request_id,
                modification=constants.ResourceModification.DELETED,
                succeeded=False,
                initiator=initiator.id,
                details=str(err),
            )
        )
        return None

    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            resource_type=constants.ResourceType.MISSION,
            resource_id=str(survey_mission_id),
            parent_resource_id=str(parent_id),
            request_id=request_id,
            modification=constants.ResourceModification.DELETED,
            succeeded=True,
            initiator=initiator.id,
        )
    )


async def list_survey_missions(
    session: AsyncSession,
    initiator: user_schemas.User | None,
    project_id: identifiers.ProjectId | None = None,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
) -> tuple[list[models.SurveyMission], int | None]:
    kwargs = dict(
        project_id=project_id,
        page=page,
        page_size=page_size,
        include_total=include_total,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
    )
    if initiator is None:
        return await mission_queries.list_published_survey_missions(session, **kwargs)
    elif not {constants.ROLE_ADMIN, constants.ROLE_SYSTEM_ADMIN}.isdisjoint(
        initiator.roles
    ):
        return await mission_queries.list_survey_missions(session, **kwargs)
    else:
        return await mission_queries.list_accessible_survey_missions(
            session, initiator.id, **kwargs
        )


async def get_survey_mission(
    survey_mission_id: identifiers.SurveyMissionId,
    initiator: user_schemas.User | None,
    session: AsyncSession,
) -> models.SurveyMission | None:
    mission = await mission_queries.get_survey_mission(session, survey_mission_id)
    if mission is None:
        return None
    if not mission_permissions.can_read_survey_mission(initiator, mission):
        raise errors.SeisLabDataError(
            f"User is not allowed to read survey mission {survey_mission_id!r}."
        )
    return mission
