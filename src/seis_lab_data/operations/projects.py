import logging

import pydantic
import shapely
from sqlmodel.ext.asyncio.session import AsyncSession

from .. import (
    dispatch,
    constants,
    errors,
)
from ..permissions import projects as project_permissions
from ..db import models
from ..db.commands import projects as project_commands
from ..db.commands import surveymissions as mission_commands
from ..db.commands import surveyrelatedrecords as record_commands
from ..db.queries import projects as project_queries
from ..schemas import (
    events as event_schemas,
    identifiers,
    filters as filter_schemas,
    projects as project_schemas,
    user as user_schemas,
    validation as validation_schemas,
)

logger = logging.getLogger(__name__)


async def create_project(
    *,
    request_id: identifiers.RequestId,
    to_create: project_schemas.ProjectCreate,
    initiator: user_schemas.User | None,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.Project | None:
    try:
        if not project_permissions.can_create_project(initiator):
            raise errors.SeisLabDataError("User is not allowed to create a project.")
        project = await project_commands.create_project(session, to_create)
        project_id = identifiers.ProjectId(project.id)
        validated_project = await validate_project(
            request_id=request_id,
            project_id=project_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        if validated_project.validation_result.get("is_valid"):
            await change_project_status(
                request_id=request_id,
                target_status=constants.ProjectStatus.PUBLISHED,
                project_id=project_id,
                initiator=initiator,
                session=session,
                event_dispatcher=event_dispatcher,
            )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                initiator=initiator.id,
                request_id=request_id,
                resource_type=constants.ResourceType.PROJECT,
                resource_id=None,
                modification=constants.ResourceModification.CREATED,
                succeeded=False,
                details=str(err),
            )
        )
        return None

    await event_dispatcher(
        event_schemas.ResourceModificationEvent(
            initiator=initiator.id,
            request_id=request_id,
            resource_type=constants.ResourceType.PROJECT,
            resource_id=str(project.id),
            modification=constants.ResourceModification.CREATED,
            succeeded=True,
        )
    )
    return project


async def change_project_status(
    request_id: identifiers.RequestId,
    target_status: constants.ProjectStatus,
    project_id: identifiers.ProjectId,
    initiator: user_schemas.User | None,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.Project | None:
    try:
        if (project := await project_queries.get_project(session, project_id)) is None:
            raise errors.SeisLabDataError(
                f"Project with id {project_id} does not exist."
            )
        if not project_permissions.can_change_project_status(initiator, project):
            raise errors.SeisLabDataError(
                "User is not allowed to change project status."
            )
        if project.status == target_status:
            logger.info(
                f"Project status is already set to {target_status} - nothing to do"
            )
            return project
        updated_project = await project_commands.set_project_status(
            session, identifiers.ProjectId(project.id), target_status
        )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceStatusChangedEvent(
                initiator=initiator.id,
                request_id=request_id,
                resource_type=constants.ResourceType.PROJECT,
                resource_id=str(project_id),
                succeeded=False,
                details=str(err),
                new_status=None,
            )
        )
        return None
    await event_dispatcher(
        event_schemas.ResourceStatusChangedEvent(
            initiator=initiator.id,
            request_id=request_id,
            resource_type=constants.ResourceType.PROJECT,
            resource_id=str(project_id),
            succeeded=True,
            new_status=updated_project.status,
        )
    )
    return updated_project


async def validate_project(
    *,
    request_id: identifiers.RequestId,
    project_id: identifiers.ProjectId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.Project:
    try:
        if (project := await project_queries.get_project(session, project_id)) is None:
            raise errors.SeisLabDataError(
                f"Project with id {project_id} does not exist."
            )
        if not project_permissions.can_validate_project(initiator, project):
            raise errors.SeisLabDataError("User is not allowed to validate project.")
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ValidationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.PROJECT,
                resource_id=str(project_id),
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
        await change_project_status(
            request_id=request_id,
            target_status=constants.ProjectStatus.UNDER_VALIDATION,
            project_id=project_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        validation_schemas.ValidProject.model_validate(project, from_attributes=True)
    except pydantic.ValidationError as err:
        for error in err.errors():
            validation_errors.append(
                {
                    "name": ".".join(str(i) for i in error["loc"]),
                    "message": error["msg"],
                    "type_": error["type"],
                }
            )
        await project_commands.update_project_validation_result(
            session,
            project,
            validation_result={
                "is_valid": False,
                "errors": validation_errors,
            },
        )
    else:
        await project_commands.update_project_validation_result(
            session, project, validation_result={"is_valid": True, "errors": None}
        )
    finally:
        await change_project_status(
            request_id=request_id,
            target_status=constants.ProjectStatus.DRAFT,
            project_id=project_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        await event_dispatcher(
            event_schemas.ValidationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.PROJECT,
                resource_id=str(project_id),
                request_id=request_id,
                modification=constants.ValidationStage.ENDED,
                succeeded=True,
                is_valid=not validation_errors,
                details=str(validation_errors),
            )
        )
    return project


async def update_project(
    request_id: identifiers.RequestId,
    project_id: identifiers.ProjectId,
    to_update: project_schemas.ProjectUpdate,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> models.Project | None:
    try:
        if (project := await project_queries.get_project(session, project_id)) is None:
            raise errors.SeisLabDataError(
                f"Project with id {project_id} does not exist."
            )
        if not project_permissions.can_update_project(initiator, project):
            raise errors.SeisLabDataError("User is not allowed to update project.")
        await project_commands.update_project(session, project, to_update)
        validated_project = await validate_project(
            request_id=request_id,
            project_id=project_id,
            initiator=initiator,
            session=session,
            event_dispatcher=event_dispatcher,
        )
        if validated_project.validation_result.get("is_valid"):
            if validated_project.status != constants.ProjectStatus.PUBLISHED:
                await change_project_status(
                    request_id=request_id,
                    target_status=constants.ProjectStatus.PUBLISHED,
                    project_id=project_id,
                    initiator=initiator,
                    session=session,
                    event_dispatcher=event_dispatcher,
                )
            published_mission_ids = (
                await mission_commands.bulk_publish_valid_survey_missions_for_project(
                    session, project_id
                )
            )
            if published_mission_ids:
                await event_dispatcher(
                    event_schemas.BulkResourceModificationEvent(
                        initiator=initiator.id,
                        request_id=request_id,
                        resource_type=constants.ResourceType.MISSION,
                        modification=constants.BulkResourceModification.UPDATED,
                        succeeded=True,
                        affected_count=len(published_mission_ids),
                    )
                )
            published_record_count = 0
            for mission_id in published_mission_ids:
                published_record_count += (
                    await record_commands.bulk_publish_valid_survey_related_records(
                        session, mission_id
                    )
                )
            if published_record_count:
                await event_dispatcher(
                    event_schemas.BulkResourceModificationEvent(
                        initiator=initiator.id,
                        request_id=request_id,
                        resource_type=constants.ResourceType.RECORD,
                        modification=constants.BulkResourceModification.UPDATED,
                        succeeded=True,
                        affected_count=published_record_count,
                    )
                )
        else:
            unpublished_mission_ids = (
                await mission_commands.bulk_unpublish_survey_missions_for_project(
                    session, project_id
                )
            )
            if unpublished_mission_ids:
                await event_dispatcher(
                    event_schemas.BulkResourceModificationEvent(
                        initiator=initiator.id,
                        request_id=request_id,
                        resource_type=constants.ResourceType.MISSION,
                        modification=constants.BulkResourceModification.UPDATED,
                        succeeded=True,
                        affected_count=len(unpublished_mission_ids),
                    )
                )
            unpublished_record_count = 0
            for mission_id in unpublished_mission_ids:
                unpublished_record_count += (
                    await record_commands.bulk_unpublish_survey_related_records(
                        session, mission_id
                    )
                )
            if unpublished_record_count:
                await event_dispatcher(
                    event_schemas.BulkResourceModificationEvent(
                        initiator=initiator.id,
                        request_id=request_id,
                        resource_type=constants.ResourceType.RECORD,
                        modification=constants.BulkResourceModification.UPDATED,
                        succeeded=True,
                        affected_count=unpublished_record_count,
                    )
                )
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                initiator=initiator.id,
                resource_type=constants.ResourceType.PROJECT,
                resource_id=str(project_id),
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
            resource_type=constants.ResourceType.PROJECT,
            resource_id=str(project_id),
            request_id=request_id,
            modification=constants.ResourceModification.UPDATED,
            succeeded=True,
        )
    )
    return validated_project


async def delete_project(
    *,
    request_id: identifiers.RequestId,
    project_id: identifiers.ProjectId,
    initiator: user_schemas.User,
    session: AsyncSession,
    event_dispatcher: dispatch.EventDispatcherProtocol,
) -> None:
    try:
        if (project := await project_queries.get_project(session, project_id)) is None:
            raise errors.SeisLabDataError(
                f"Project with id {project_id} does not exist."
            )
        if not project_permissions.can_delete_project(initiator, project):
            raise errors.SeisLabDataError("User is not allowed to delete projects.")
        if project.status != constants.ProjectStatus.DRAFT:
            raise errors.SeisLabDataError(
                f"Cannot delete project with status {project.status}."
            )
        await project_commands.delete_project(session, project_id)
    except errors.SeisLabDataError as err:
        await event_dispatcher(
            event_schemas.ResourceModificationEvent(
                resource_type=constants.ResourceType.PROJECT,
                resource_id=str(project_id),
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
            resource_type=constants.ResourceType.PROJECT,
            resource_id=str(project_id),
            request_id=request_id,
            modification=constants.ResourceModification.DELETED,
            succeeded=True,
            initiator=initiator.id,
        )
    )


async def list_projects(
    session: AsyncSession,
    initiator: user_schemas.User | None,
    page: int = 1,
    page_size: int = 20,
    include_total: bool = False,
    en_name_filter: str | None = None,
    pt_name_filter: str | None = None,
    spatial_intersect: shapely.Polygon | None = None,
    temporal_extent: filter_schemas.TemporalExtentFilterValue | None = None,
) -> tuple[list[models.Project], int | None]:
    kwargs = dict(
        page=page,
        page_size=page_size,
        include_total=include_total,
        en_name_filter=en_name_filter,
        pt_name_filter=pt_name_filter,
        spatial_intersect=spatial_intersect,
        temporal_extent=temporal_extent,
    )
    if initiator is None:
        return await project_queries.list_published_projects(session, **kwargs)
    elif not {constants.ROLE_ADMIN, constants.ROLE_SYSTEM_ADMIN}.isdisjoint(
        initiator.roles
    ):
        return await project_queries.list_projects(session, **kwargs)
    else:
        return await project_queries.list_accessible_projects(
            session, initiator.id, **kwargs
        )


async def get_project(
    project_id: identifiers.ProjectId,
    initiator: user_schemas.User | None,
    session: AsyncSession,
) -> models.Project | None:
    project = await project_queries.get_project(session, project_id)
    if project is None:
        return None
    if not project_permissions.can_read_project(initiator, project):
        raise errors.SeisLabDataError(
            f"User is not allowed to read project {project_id!r}."
        )
    return project
