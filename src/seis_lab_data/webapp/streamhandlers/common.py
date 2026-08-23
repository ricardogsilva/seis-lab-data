import json
import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator

from datastar_py.sse import ServerSentEventGenerator
from datastar_py.starlette import DatastarEvent

from ... import (
    constants,
    subscribers,
)
from ...operations import (
    surveymissions as mission_ops,
    surveyrelatedrecords as record_ops,
)
from ...schemas import (
    identifiers,
    messages as message_schemas,
    webui as webui_schemas,
)

logger = logging.getLogger(__name__)


async def flash_ui_message_after_redirect(
    notification: webui_schemas.Notification,
) -> AsyncGenerator[DatastarEvent, None]:
    payload = {
        "message": notification.message,
        "category": {
            "success": "info",
            "error": "danger",
        }.get(notification.category, "info"),
    }
    logger.debug(f"{payload=}")
    yield ServerSentEventGenerator.execute_script(
        f"localStorage.setItem('sld:flash', {json.dumps(json.dumps(payload))});"
    )


async def flash_ui_message_same_page(
    notification: webui_schemas.Notification,
) -> AsyncGenerator[DatastarEvent, None]:
    payload = {
        "message": notification.message,
        "category": {
            "success": "info",
            "error": "danger",
        }.get(notification.category, "info"),
    }
    yield ServerSentEventGenerator.execute_script(f"showFlash({json.dumps(payload)})")


async def handle_resource_modification_new_page(
    message: message_schemas.SldPubSubMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    # this page only cares about the message that corresponds to its own creation request
    if context.request_id != message.request_id:
        return

    # these resource types have no detail page of their own, so - unlike the
    # others handled below - we always send the user back to their listing
    # page, regardless of whether creation succeeded, and report the outcome
    # via a flash notification
    list_page_route_name = {
        constants.ResourceType.ASSET_DISCOVERY_CONFIG: "asset_discovery_configurations:list",
        constants.ResourceType.CATEGORY: "dataset_categories:list",
        constants.ResourceType.WORKFLOW_STAGE: "workflow_stages:list",
    }.get(message.resource_type)

    if list_page_route_name is not None:
        if message.succeeded:
            async for event in flash_ui_message_after_redirect(
                webui_schemas.Notification(
                    message=f"{message.resource_type.capitalize()} was {message.modification} successfully!"
                )
            ):
                yield event
        else:
            async for event in flash_ui_message_after_redirect(
                webui_schemas.Notification(
                    message=f"{message.resource_type.capitalize()} could not be {message.modification}: {message.details}",
                    category="error",
                )
            ):
                yield event
        yield ServerSentEventGenerator.redirect(
            str(context.url_resolver(list_page_route_name))
        )
        return

    if not message.succeeded:
        yield ServerSentEventGenerator.patch_signals({"submitting": False})
        async for event in flash_ui_message_same_page(
            webui_schemas.Notification(
                message=f"{message.resource_type.capitalize()} could not be {message.modification}: {message.details}",
                category="error",
            )
        ):
            yield event
        return

    async for event in flash_ui_message_after_redirect(
        webui_schemas.Notification(
            message=f"{message.resource_type.capitalize()} was {message.modification} successfully!"
        )
    ):
        yield event

    match message.resource_type:
        case constants.ResourceType.PROJECT:
            yield ServerSentEventGenerator.redirect(
                str(
                    context.url_resolver(
                        "projects:detail", project_id=message.resource_id
                    )
                )
            )
        case constants.ResourceType.MISSION:
            yield ServerSentEventGenerator.redirect(
                str(
                    context.url_resolver(
                        "survey_missions:detail", survey_mission_id=message.resource_id
                    )
                )
            )
        case constants.ResourceType.RECORD:
            yield ServerSentEventGenerator.redirect(
                str(
                    context.url_resolver(
                        "survey_related_records:detail",
                        survey_related_record_id=message.resource_id,
                    )
                )
            )


async def handle_resource_modification_list_page(
    message: message_schemas.SldPubSubMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    if (
        not message.succeeded
    ):  # only send notification if the context has the same request_id
        if context.request_id == message.request_id:
            async for event in flash_ui_message_same_page(
                webui_schemas.Notification(
                    message=f"{message.resource_type.capitalize()} could not be {message.modification}: {message.details}",
                    category="error",
                )
            ):
                yield event
    else:
        async for event in flash_ui_message_same_page(
            webui_schemas.Notification(
                message=f"{message.resource_type.capitalize()} {message.resource_id} was {message.modification.value}",
            )
        ):
            yield event
    # update datastar signal that frontend recognizes as needing to re-fetch listing
    yield ServerSentEventGenerator.patch_signals(
        {"listingVersion": int(time.time() * 1000)}
    )


async def handle_bulk_resource_modification(
    message: message_schemas.SldPubSubMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    """Flash the outcome of a bulk update and return to the mission detail page.

    There's no single `resource_id` to match on for a bulk action, so this
    matches on `request_id` instead - `context.resource_id` is repurposed to
    carry the survey mission to redirect back to.
    """
    if message.request_id != context.request_id:
        return

    if message.succeeded:
        notification = webui_schemas.Notification(
            message=f"Bulk update succeeded: {message.affected_count} record(s) updated.",
            category="success",
        )
    else:
        notification = webui_schemas.Notification(
            message=f"Bulk update failed: {message.details}",
            category="error",
        )
    async for event in flash_ui_message_after_redirect(notification):
        yield event
    yield ServerSentEventGenerator.redirect(
        str(
            context.url_resolver(
                "survey_missions:detail", survey_mission_id=context.resource_id
            )
        )
    )


async def handle_resource_modification_edit_page(
    message: message_schemas.SldPubSubMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    """Enqueue validation and redirect to project detail page after editing."""
    logger.debug(f"{message=}")
    logger.debug(f"{context=}")
    if message.resource_id != context.resource_id:
        return

    if message.succeeded:
        notification = webui_schemas.Notification(
            message=f"{message.resource_type.capitalize()} updated successfully!",
            category="success",
        )
    else:
        notification = webui_schemas.Notification(
            message=f"{message.resource_type.capitalize()} could not be updated: {message.details}",
            category="error",
        )

    async for event in flash_ui_message_after_redirect(notification):
        yield event

    match message.resource_type:
        case constants.ResourceType.PROJECT:
            yield ServerSentEventGenerator.redirect(
                str(
                    context.url_resolver(
                        "projects:detail", project_id=message.resource_id
                    )
                )
            )
        case constants.ResourceType.MISSION:
            yield ServerSentEventGenerator.redirect(
                str(
                    context.url_resolver(
                        "survey_missions:detail", survey_mission_id=message.resource_id
                    )
                )
            )
        case constants.ResourceType.RECORD:
            yield ServerSentEventGenerator.redirect(
                str(
                    context.url_resolver(
                        "survey_related_records:detail",
                        survey_related_record_id=message.resource_id,
                    )
                )
            )
        case constants.ResourceType.ASSET_DISCOVERY_CONFIG:
            yield ServerSentEventGenerator.redirect(
                str(context.url_resolver("asset_discovery_configurations:list"))
            )
        case constants.ResourceType.CATEGORY:
            yield ServerSentEventGenerator.redirect(
                str(context.url_resolver("dataset_categories:list"))
            )
        case constants.ResourceType.WORKFLOW_STAGE:
            yield ServerSentEventGenerator.redirect(
                str(context.url_resolver("workflow_stages:list"))
            )


async def _handle_project_modification_detail_page(
    message: message_schemas.ResourceModificationMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    project_id = identifiers.ProjectId(uuid.UUID(context.resource_id))
    if message.resource_type == constants.ResourceType.PROJECT:
        # are we handling this project's details?
        if message.resource_id != context.resource_id:
            return

        if not message.succeeded:  # if same request_id, show a notification
            if message.request_id == context.request_id:
                async for event in flash_ui_message_same_page(
                    webui_schemas.Notification(
                        message=f"{message.resource_type.capitalize()} {message.modification} failed: {message.details}",
                        category="error",
                    )
                ):
                    yield event

        else:
            match message.modification:
                case constants.ResourceModification.UPDATED:
                    logger.debug(
                        f"Project {message.resource_id!r} has been updated - re-rendering its details..."
                    )
                    async for event in flash_ui_message_after_redirect(
                        webui_schemas.Notification(
                            message=f"{message.resource_type.capitalize()} {message.resource_id} was {message.modification}",
                        )
                    ):
                        yield event
                    yield ServerSentEventGenerator.redirect(
                        str(
                            context.url_resolver(
                                "projects:detail", project_id=project_id
                            )
                        )
                    )
                case constants.ResourceModification.DELETED:
                    async for event in flash_ui_message_after_redirect(
                        webui_schemas.Notification(
                            message=f"{message.resource_type.capitalize()} {message.resource_id} was deleted",
                        )
                    ):
                        yield event
                    yield ServerSentEventGenerator.redirect(
                        str(context.url_resolver("projects:list"))
                    )
                case _:
                    logger.debug(
                        f"Don't know how to handle modification {message.modification!r}, skipping..."
                    )

    elif message.resource_type == constants.ResourceType.MISSION and message.succeeded:
        # only care about survey_mission if it is child of project in context
        survey_mission_id = identifiers.SurveyMissionId(uuid.UUID(message.resource_id))
        async with context.db_session_factory() as session:
            if (
                db_mission := await mission_ops.get_survey_mission(
                    survey_mission_id, context.user, session
                )
            ) is None:
                logger.debug(
                    f"Could not find survey_mission with id {message.resource_id!r} in the DB"
                )
                return
            if identifiers.ProjectId(db_mission.project_id) != project_id:
                logger.debug(
                    f"survey_mission {survey_mission_id!r} is not child of current project - skipping..."
                )
                return
        logger.debug(
            f"Survey mission {message.resource_id!r} is a child of the current project - asking frontend to re-fetch mission listing..."
        )
        async for event in flash_ui_message_same_page(
            notification=webui_schemas.Notification(
                message=(
                    f"Project {project_id} has had child survey mission {survey_mission_id} "
                    f"modified - Reloaded mission list"
                )
            )
        ):
            yield event
        # update datastar signal which frontend uses to check when needing to re-fetch list of project child missions
        yield ServerSentEventGenerator.patch_signals(
            {"listingVersion": int(time.time() * 1000)}
        )

    elif message.resource_type == constants.ResourceType.RECORD and message.succeeded:
        # only care about record if it is grandchild of project in context
        record_id = identifiers.SurveyRelatedRecordId(uuid.UUID(message.resource_id))
        async with context.db_session_factory() as session:
            if (
                record_info := await record_ops.get_survey_related_record(
                    record_id, context.user, session
                )
            ) is None:
                logger.debug(
                    f"Could not find survey_related_record with id {message.resource_id!r} in the DB"
                )
                return
            db_record = record_info[0]
            if identifiers.ProjectId(db_record.survey_mission.project_id) != project_id:
                logger.debug(
                    f"survey_related_record {record_id!r} is not grandchild of current project - skipping..."
                )
                return
        logger.debug(
            f"Survey-related record {message.resource_id!r} is grandchild of current project - asking frontend to re-fetch mission listing..."
        )
        message = f"Project {project_id} has had grandchild survey-related record {record_id} modified - Reloaded mission list"
        async for event in flash_ui_message_same_page(
            notification=webui_schemas.Notification(message=message)
        ):
            yield event
        # update datastar signal which frontend uses to check when needing to re-fetch list of project child missions
        yield ServerSentEventGenerator.patch_signals(
            {"listingVersion": int(time.time() * 1000)}
        )


async def _handle_survey_mission_modification_detail_page(
    message: message_schemas.ResourceModificationMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    mission_id = identifiers.SurveyMissionId(uuid.UUID(context.resource_id))
    if message.resource_type == constants.ResourceType.MISSION:
        # are we handling this mission's details?
        if message.resource_id != context.resource_id:
            return

        if not message.succeeded:  # if same request_id, show a notification
            if message.request_id == context.request_id:
                async for event in flash_ui_message_same_page(
                    webui_schemas.Notification(
                        message=f"{message.resource_type.capitalize()} {message.modification} failed: {message.details}",
                        category="error",
                    )
                ):
                    yield event

        else:
            match message.modification:
                case constants.ResourceModification.UPDATED:
                    logger.debug(
                        f"Survey mission {message.resource_id!r} has been updated - re-rendering its details..."
                    )
                    # TODO: yield re-render of mission details
                case constants.ResourceModification.DELETED:
                    async for event in flash_ui_message_after_redirect(
                        webui_schemas.Notification(
                            message=f"{message.resource_type.capitalize()} {message.resource_id} was deleted",
                        )
                    ):
                        yield event
                    if (project_id := message.parent_resource_id) is not None:
                        redirect_to = context.url_resolver(
                            "projects:detail", project_id=project_id
                        )
                    else:
                        redirect_to = context.url_resolver("survey_missions:list")

                    yield ServerSentEventGenerator.redirect(str(redirect_to))
                case _:
                    logger.debug(
                        f"Don't know how to handle modification {message.modification!r}, skipping..."
                    )

    elif message.resource_type == constants.ResourceType.RECORD and message.succeeded:
        # only care about record if it is child of mission in context
        record_id = identifiers.SurveyRelatedRecordId(uuid.UUID(message.resource_id))
        async with context.db_session_factory() as session:
            if (
                record_info := await record_ops.get_survey_related_record(
                    record_id, context.user, session
                )
            ) is None:
                logger.debug(
                    f"Could not find survey_related_record with id {message.resource_id!r} in the DB"
                )
                return
            db_record = record_info[0]
            if identifiers.SurveyMissionId(db_record.survey_mission_id) != mission_id:
                logger.debug(
                    f"survey_related_record {record_id!r} is not child of current mission - skipping..."
                )
                return
        logger.debug(
            f"Survey-related record {message.resource_id!r} is a child of the current mission - asking frontend to re-fetch record listing..."
        )
        async for event in flash_ui_message_same_page(
            notification=webui_schemas.Notification(
                message=(
                    f"Mission {mission_id} has had child survey-related record {record_id} "
                    f"modified - Reloaded record list"
                )
            )
        ):
            yield event
        # update datastar signal which frontend uses to check when needing to re-fetch list of mission child records
        yield ServerSentEventGenerator.patch_signals(
            {"listingVersion": int(time.time() * 1000)}
        )


async def _handle_survey_related_record_modification_detail_page(
    message: message_schemas.ResourceModificationMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    if message.resource_id != context.resource_id:
        # are we handling this record's details?
        return

    if not message.succeeded:  # if same request_id, show a notification
        if message.request_id == context.request_id:
            async for event in flash_ui_message_same_page(
                webui_schemas.Notification(
                    message=f"{message.resource_type.capitalize()} {message.modification} failed: {message.details}",
                    category="error",
                )
            ):
                yield event
    else:
        match message.modification:
            case constants.ResourceModification.UPDATED:
                logger.debug(
                    f"Survey-related record {message.resource_id!r} has been updated - re-rendering its details..."
                )
                yield ServerSentEventGenerator.redirect(
                    str(
                        context.url_resolver(
                            "survey_related_records:detail",
                            survey_related_record_id=message.resource_id,
                        )
                    )
                )
            case constants.ResourceModification.DELETED:
                async for event in flash_ui_message_after_redirect(
                    webui_schemas.Notification(
                        message=f"{message.resource_type.capitalize()} {message.resource_id} was deleted",
                    )
                ):
                    yield event
                if (mission_id := message.parent_resource_id) is not None:
                    redirect_to = context.url_resolver(
                        "survey_missions:detail", survey_mission_id=mission_id
                    )
                else:
                    redirect_to = context.url_resolver("survey_related_records:list")

                yield ServerSentEventGenerator.redirect(str(redirect_to))
            case _:
                logger.debug(
                    f"Don't know how to handle modification {message.modification!r}, skipping..."
                )


async def handle_resource_status_changed_detail_page(
    message: message_schemas.ResourceStatusChangedMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    """Handle status changes for an item's detail page."""

    async for event in flash_ui_message_after_redirect(
        webui_schemas.Notification(
            message=f"{message.resource_type.value} changed status to {message.new_status}"
        )
    ):
        yield event

    match context.resource_type:
        case constants.ResourceType.PROJECT:
            async for event in _handle_project_status_changed_detail_page(
                message, context, done
            ):
                yield event
        case constants.ResourceType.MISSION:
            async for event in _handle_survey_mission_status_changed_detail_page(
                message, context, done
            ):
                yield event
        case _:
            if context.resource_id != message.resource_id:
                return
            match message.resource_type:
                case constants.ResourceType.RECORD:
                    yield ServerSentEventGenerator.redirect(
                        str(
                            context.url_resolver(
                                "survey_related_records:detail",
                                survey_related_record_id=message.resource_id,
                            )
                        )
                    )
                case _:
                    logger.debug(
                        f"Don't know how to handle a status change for {message!r}"
                    )


async def _handle_survey_mission_status_changed_detail_page(
    message: message_schemas.ResourceStatusChangedMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    """Handle a status changed message in the context of a survey mission detail page.

    If a mission status changes and the user is looking at the mission's detail page we want to refresh the page

    If a survey-related record status changes and the user is looking at its parent mission detail page we want to
    reload the list of records because it may be modified
    """

    match message:
        case message_schemas.ResourceStatusChangedMessage(
            succeeded=True, resource_type=constants.ResourceType.MISSION
        ):
            if message.resource_id == context.resource_id:
                yield ServerSentEventGenerator.redirect(
                    str(
                        context.url_resolver(
                            "survey_missions:detail",
                            survey_mission_id=message.resource_id,
                        )
                    )
                )
        case message_schemas.ResourceStatusChangedMessage(
            succeeded=True,
            resource_type=constants.ResourceType.RECORD,
        ):
            record_id = identifiers.SurveyRelatedRecordId(
                uuid.UUID(message.resource_id)
            )
            # only care if record is a child of context's mission
            async with context.db_session_factory() as session:
                if (
                    record_info := await record_ops.get_survey_related_record(
                        survey_related_record_id=record_id,
                        initiator=context.user,
                        session=session,
                    )
                ) is not None and str(
                    record_info[0].survey_mission_id == context.resource_id
                ):
                    async for event in flash_ui_message_same_page(
                        notification=webui_schemas.Notification(
                            message=(
                                f"Survey mission {context.resource_id!r} has had child record {record_info[0].id!r} "
                                f"modified - Reloaded record list"
                            )
                        )
                    ):
                        yield event
                    # update datastar signal which frontend uses to check when needing to re-fetch list of project child missions
                    yield ServerSentEventGenerator.patch_signals(
                        {"listingVersion": int(time.time() * 1000)}
                    )


async def _handle_project_status_changed_detail_page(
    message: message_schemas.ResourceStatusChangedMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    """Handle a status changed message in the context of a project detail page.

    If a project status changes and the user is looking at the project's detail page we want to refresh the page

    If a survey mission status changes and the user is looking at its parent project detail page we want to
    reload the list of survey missions because it may be modified

    If a survey-related record's status changes and the user is looking at its parent project detail page we want
    to reload the list of survey missions because it may be modified
    """

    match message:
        case message_schemas.ResourceStatusChangedMessage(
            succeeded=True, resource_type=constants.ResourceType.PROJECT
        ):
            if message.resource_id == context.resource_id:
                yield ServerSentEventGenerator.redirect(
                    str(
                        context.url_resolver(
                            "projects:detail", project_id=message.resource_id
                        )
                    )
                )
        case message_schemas.ResourceStatusChangedMessage(
            succeeded=True,
            resource_type=constants.ResourceType.MISSION,
        ):
            mission_id = identifiers.SurveyMissionId(uuid.UUID(message.resource_id))
            # only care if mission is a child of context's project
            async with context.db_session_factory() as session:
                if (
                    db_mission := await mission_ops.get_survey_mission(
                        survey_mission_id=mission_id,
                        initiator=context.user,
                        session=session,
                    )
                ) is not None and str(db_mission.project_id == context.resource_id):
                    async for event in flash_ui_message_same_page(
                        notification=webui_schemas.Notification(
                            message=(
                                f"Project {context.resource_id!r} has had child survey mission {db_mission.id!r} "
                                f"modified - Reloaded mission list"
                            )
                        )
                    ):
                        yield event
                    # update datastar signal which frontend uses to check when needing to re-fetch list of project child missions
                    yield ServerSentEventGenerator.patch_signals(
                        {"listingVersion": int(time.time() * 1000)}
                    )
        case message_schemas.ResourceStatusChangedMessage(
            succeeded=True,
            resource_type=constants.ResourceType.RECORD,
        ):
            record_id = identifiers.SurveyRelatedRecordId(
                uuid.UUID(message.resource_id)
            )
            # only care if record's parent mission is child of the context's project
            async with context.db_session_factory() as session:
                if (
                    record_info := await record_ops.get_survey_related_record(
                        survey_related_record_id=record_id,
                        initiator=context.user,
                        session=session,
                    )
                ) is not None and str(
                    record_info[0].survey_mission.project_id
                ) == context.resource_id:
                    async for event in flash_ui_message_same_page(
                        notification=webui_schemas.Notification(
                            message=(
                                f"Project {context.resource_id!r} has had child survey-related record {record_info[0].id!r} "
                                f"modified - Reloaded mission list"
                            )
                        )
                    ):
                        yield event
                    # update datastar signal which frontend uses to check when needing to re-fetch list of project child missions
                    yield ServerSentEventGenerator.patch_signals(
                        {"listingVersion": int(time.time() * 1000)}
                    )


async def handle_resource_modification_detail_page(
    message: message_schemas.ResourceModificationMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    # resource might:
    # - have been updated - reload the resource details
    # - have been deleted - redirect to resource list page
    # - have had a child resource modified - only applies to SurveyMission and Project - ask the frontend to reload the list of child resources
    # - have been created - not relevant for the detail page
    # - have had its status changed - handled in another handler - need to update the status-related fields
    # - have had its discovery progressed - handled in another handler - only applies to SurveyMission: need to update the discovery-related fields
    logger.debug(f"{context=}")
    logger.debug(f"{message=}")

    match context.resource_type:
        case constants.ResourceType.PROJECT:
            async for event in _handle_project_modification_detail_page(
                message, context, done
            ):
                yield event
        case constants.ResourceType.MISSION:
            async for event in _handle_survey_mission_modification_detail_page(
                message, context, done
            ):
                yield event
        case constants.ResourceType.RECORD:
            async for event in _handle_survey_related_record_modification_detail_page(
                message, context, done
            ):
                yield event
        case _:
            if message.resource_id != context.resource_id:
                return
            if not message.succeeded:  # if same request_id, show a notification
                if message.request_id == context.request_id:
                    async for event in flash_ui_message_same_page(
                        webui_schemas.Notification(
                            message=f"{message.resource_type.capitalize()} {message.modification} failed: {message.details}",
                            category="error",
                        )
                    ):
                        yield event
                return
            async for event in flash_ui_message_after_redirect(
                webui_schemas.Notification(
                    message=f"{message.resource_type} {message.resource_id} was {message.modification}"
                )
            ):
                yield event
            match message.resource_type:
                # regardless of it being an update or a delete, redirect to the listing page
                case constants.ResourceType.ASSET_DISCOVERY_CONFIG:
                    yield ServerSentEventGenerator.redirect(
                        str(context.url_resolver("asset_discovery_configurations:list"))
                    )
                case constants.ResourceType.CATEGORY:
                    yield ServerSentEventGenerator.redirect(
                        str(context.url_resolver("dataset_categories:list"))
                    )
                case constants.ResourceType.WORKFLOW_STAGE:
                    yield ServerSentEventGenerator.redirect(
                        str(context.url_resolver("workflow_stages:list"))
                    )


async def handle_discovery_detail_page(
    message: message_schemas.DiscoveryMessage,
    context: subscribers.HandlerContext,
    done: asyncio.Event | None = None,
) -> AsyncGenerator[DatastarEvent, None]:
    # - have had its discovery progressed - only applies to SurveyMission: need to update the discovery-related fields
    logger.debug(f"{context=}")
    logger.debug(f"{message=}")
    if message.resource_id != context.resource_id:
        return
    if not message.succeeded:
        if message.request_id == context.request_id:
            async for event in flash_ui_message_same_page(
                webui_schemas.Notification(
                    message=f"{message.resource_type.capitalize()} discovery failed: {message.details}",
                    category="error",
                )
            ):
                yield event
    else:
        notification_msg = f"{message.resource_type.capitalize()} discovery {message.modification.value}"
        if message.details:
            notification_msg += f": {message.details}"
        async for event in flash_ui_message_same_page(
            webui_schemas.Notification(message=notification_msg)
        ):
            yield event
