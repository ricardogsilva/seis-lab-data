import dataclasses
import json
import logging
from typing import TypeVar
import uuid

import shapely
from datastar_py import ServerSentEventGenerator
from datastar_py.consts import ElementPatchMode
from datastar_py.starlette import DatastarResponse
from redis.asyncio import Redis
from starlette_babel import gettext_lazy as _
from starlette.endpoints import HTTPEndpoint
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette_wtf import csrf_protect

from ... import (
    config,
    constants,
    errors,
    geojson,
    localization,
    subscribers,
)
from ...constants import SURVEY_RELATED_RECORD_MAX_RELATED
from ...operations import (
    projects as project_ops,
    surveymissions as survey_mission_ops,
    surveyrelatedrecords as survey_related_record_ops,
)
from ...permissions import surveyrelatedrecords as record_permissions
from ...db import models
from ...db.queries import (
    datasetcategories as category_queries,
    recordassets as asset_queries,
    workflowstages as stage_queries,
)
from ...tasks import surveyrelatedrecords as record_tasks
from ...schemas import (
    common as common_schemas,
    identifiers,
    projects as project_schemas,
    surveymissions as mission_schemas,
    surveyrelatedrecords as record_schemas,
    webui as webui_schemas,
)
from .. import (
    filters,
    forms,
)
from ..streamhandlers import common as common_handlers
from .auth import (
    requires_auth,
)
from .common import (
    build_related_record_compound_name,
    build_mission_compound_name,
    build_project_compound_name,
    get_id_from_request_path,
    get_page_from_request_params,
    get_pagination_info,
    UPDATE_BASEMAP_JS_SCRIPT,
)

logger = logging.getLogger(__name__)


async def _get_survey_related_record_details(
    request: Request,
) -> webui_schemas.SurveyRelatedRecordDetails:
    """Utility function to get survey-related record details and its assets."""
    user = request.user if request.user.is_authenticated else None
    survey_related_record_id = get_id_from_request_path(
        request, "survey_related_record_id", identifiers.SurveyRelatedRecordId
    )
    async with request.state.settings.get_db_session_maker()() as session:
        survey_related_record_info = (
            await survey_related_record_ops.get_survey_related_record(
                survey_related_record_id,
                user,
                session,
            )
        )
        if survey_related_record_info is None:
            raise HTTPException(
                status_code=404,
                detail=_(
                    f"Survey-related record {survey_related_record_id!r} not found."
                ),
            )
    survey_related_record, related_to, subject_for = survey_related_record_info
    serialized = webui_schemas.SurveyRelatedRecordReadDetail.from_db_instance(
        survey_related_record, related_to, subject_for
    )
    can_update = record_permissions.can_update_survey_related_record(
        user, survey_related_record
    )
    settings: config.SeisLabDataSettings = request.state.settings
    return webui_schemas.SurveyRelatedRecordDetails(
        item=serialized,
        permissions=webui_schemas.UserPermissionDetails(
            can_create_children=can_update,
            can_update=can_update,
            can_delete=record_permissions.can_delete_survey_related_record(
                user, survey_related_record
            ),
        ),
        breadcrumbs=[
            webui_schemas.BreadcrumbItem(
                name=_("Home"),
                url=str(request.url_for("home")),
                icon=settings.icons.home,
            ),
            webui_schemas.BreadcrumbItem(
                name=_("Projects"),
                url=str(request.url_for("projects:list")),
                icon=settings.icons.projects,
            ),
            webui_schemas.BreadcrumbItem(
                name=str(survey_related_record.survey_mission.project.name["en"]),
                url=str(
                    request.url_for(
                        "projects:detail",
                        project_id=survey_related_record.survey_mission.project.id,
                    )
                ),
                icon=settings.icons.projects,
            ),
            webui_schemas.BreadcrumbItem(
                name=str(survey_related_record.survey_mission.name["en"]),
                url=str(
                    request.url_for(
                        "survey_missions:detail",
                        survey_mission_id=survey_related_record.survey_mission.id,
                    )
                ),
                icon=settings.icons.survey_missions,
            ),
            webui_schemas.BreadcrumbItem(
                name=str(survey_related_record.name["en"]),
                icon=settings.icons.survey_related_records,
            ),
        ],
    )


@requires_auth
async def get_details_component(request: Request):
    details = await _get_survey_related_record_details(request)
    template_processor = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/detail-component.html"
    )
    rendered = template.render(
        request=request,
        survey_related_record=details.item,
        permissions=details.permissions,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


FormType = TypeVar(
    "FormType",
    bound=forms.FormProtocol,
)


async def build_survey_related_record_form_instance(
    request: Request, form_type: type[FormType]
) -> FormType:
    form_instance = await form_type.from_formdata(request)
    current_language = request.state.language
    async with request.state.settings.get_db_session_maker()() as session:
        form_instance.dataset_category_id.choices = [
            (dc.id, dc.name.get(current_language, dc.name["en"]))
            for dc in await category_queries.collect_all_dataset_categories(
                session,
                order_by_clause=models.DatasetCategory.name[current_language].astext,
            )
        ]
        form_instance.workflow_stage_id.choices = [
            (ws.id, ws.name.get(current_language, ws.name["en"]))
            for ws in await stage_queries.collect_all_workflow_stages(
                session,
                order_by_clause=models.WorkflowStage.name[current_language].astext,
            )
        ]
    return form_instance


async def get_record_parent_survey_mission_from_request(
    request: Request,
) -> models.SurveyMission:
    user = request.user if request.user.is_authenticated else None
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    async with request.state.settings.get_db_session_maker()() as session:
        if not (
            survey_mission := await survey_mission_ops.get_survey_mission(
                parent_survey_mission_id, user, session
            )
        ):
            raise HTTPException(
                404, _(f"Survey mission with id {parent_survey_mission_id} not found.")
            )
    return survey_mission


@csrf_protect
@requires_auth
async def get_creation_form(request: Request):
    """Show an HTML form for the client to prepare a record creation operation."""
    parent_survey_mission = await get_record_parent_survey_mission_from_request(request)
    survey_mission_id = identifiers.SurveyMissionId(parent_survey_mission.id)
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    form_instance.request_id.data = str(identifiers.RequestId(uuid.uuid4()))
    template_processor: Jinja2Templates = request.state.templates
    settings: config.SeisLabDataSettings = request.state.settings
    return template_processor.TemplateResponse(
        request,
        "survey-related-records/create-form-page.html",
        context={
            "form": form_instance,
            "survey_mission_id": survey_mission_id,
            "breadcrumbs": [
                webui_schemas.BreadcrumbItem(
                    name=_("Home"),
                    url=request.url_for("home"),
                    icon=settings.icons.home,
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("Projects"),
                    url=request.url_for("projects:list"),
                    icon=settings.icons.projects,
                ),
                webui_schemas.BreadcrumbItem(
                    name=parent_survey_mission.project.name["en"],
                    url=request.url_for(
                        "projects:detail", project_id=parent_survey_mission.project.id
                    ),
                    icon=settings.icons.projects,
                ),
                webui_schemas.BreadcrumbItem(
                    name=parent_survey_mission.name["en"],
                    url=request.url_for(
                        "survey_missions:detail", survey_mission_id=survey_mission_id
                    ),
                    icon=settings.icons.survey_missions,
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("New survey-related record"), icon=settings.icons.new_item
                ),
            ],
        },
    )


@csrf_protect
async def add_creation_form_link(request: Request):
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    form_instance.links.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=parent_survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_creation_form_link(request: Request):
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    link_index = int(request.query_params.get("link_index", 0))
    form_instance.links.entries.pop(link_index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=parent_survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_creation_form_asset(request: Request):
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    form_instance.assets.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=parent_survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_creation_form_asset(request: Request):
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    index = int(request.query_params.get("asset_index", 0))
    form_instance.assets.entries.pop(index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=parent_survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_creation_form_asset_link(request: Request):
    survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    asset_index = int(request.path_params["asset_index"])
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    form_instance.assets[asset_index].asset_links.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_creation_form_asset_link(request: Request):
    survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )

    asset_index = int(request.path_params["asset_index"])
    link_index = int(request.query_params.get("link_index", 0))

    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)

    form_instance.assets[asset_index].asset_links.entries.pop(link_index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_creation_form_related_record(request: Request):
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    form_instance.related_records.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=parent_survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_creation_form_related_record(request: Request):
    parent_survey_mission_id = get_id_from_request_path(
        request, "survey_mission_id", identifiers.SurveyMissionId
    )
    form_instance = await forms.SurveyRelatedRecordCreateForm.from_request(request)
    index = int(request.query_params.get("index", 0))
    form_instance.related_records.entries.pop(index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/create-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_mission_id=parent_survey_mission_id,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
@requires_auth
async def get_update_form(request: Request):
    """Show an HTML form for the client to prepare a record update operation."""
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(
        request,
        data={
            "name": {
                "en": details.item.name.en,
                "pt": details.item.name.pt,
            },
            "description": {
                "en": details.item.description.en,
                "pt": details.item.description.pt,
            },
            "dataset_category_id": details.item.dataset_category.id
            if details.item.dataset_category
            else None,
            "workflow_stage_id": details.item.workflow_stage.id
            if details.item.workflow_stage
            else None,
            "bounding_box": {
                "min_lon": bbox.bounds[0],
                "min_lat": bbox.bounds[1],
                "max_lon": bbox.bounds[2],
                "max_lat": bbox.bounds[3],
            }
            if (bbox := details.item.bbox_4326)
            else None,
            "temporal_extent_begin": details.item.temporal_extent_begin,
            "temporal_extent_end": details.item.temporal_extent_end,
            "links": [
                {
                    "url": li.url,
                    "media_type": li.media_type,
                    "relation": li.relation,
                    "link_description": {
                        "en": li.link_description.en,
                        "pt": li.link_description.pt,
                    },
                }
                for li in details.item.links
            ],
            "assets": [
                {
                    "asset_id": str(ass.id),
                    "asset_name": {
                        "en": ass.name.en,
                        "pt": ass.name.pt,
                    },
                    "asset_description": {
                        "en": ass.description.en,
                        "pt": ass.description.pt,
                    },
                    "relative_path": ass.relative_path,
                    "asset_links": [
                        {
                            "url": ali.url,
                            "media_type": ali.media_type,
                            "relation": ali.relation,
                            "link_description": {
                                "en": ali.link_description.en,
                                "pt": ali.link_description.pt,
                            },
                        }
                        for ali in ass.links
                    ],
                }
                for ass in details.item.record_assets
            ],
            "related_records": [
                {
                    "related_record": build_related_record_compound_name(request, r[1]),
                    "relationship": {
                        "en": r[0].en,
                        "pt": r[0].pt,
                    },
                }
                for r in details.item.related_to_records
            ],
        },
    )
    form_instance.request_id.data = uuid.uuid4()
    template_processor: Jinja2Templates = request.state.templates
    user = request.user if request.user.is_authenticated else None
    async with request.state.settings.get_db_session_maker()() as session:
        (
            initial_related_records_list,
            __,
        ) = await survey_related_record_ops.list_survey_related_records(
            session,
            initiator=user,
        )
    initial_related_records = [
        (i.id, i.name["en"]) for i in initial_related_records_list
    ]
    settings: config.SeisLabDataSettings = request.state.settings
    return template_processor.TemplateResponse(
        request,
        "survey-related-records/update-form-page.html",
        context={
            "survey_related_record": details.item,
            "form": form_instance,
            "initial_related_records": initial_related_records,
            "breadcrumbs": [
                webui_schemas.BreadcrumbItem(
                    name=_("Home"),
                    url=request.url_for("home"),
                    icon=settings.icons.home,
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("Projects"),
                    url=request.url_for("projects:list"),
                    icon=settings.icons.projects,
                ),
                webui_schemas.BreadcrumbItem(
                    name=details.item.survey_mission.project.name.en,
                    url=request.url_for(
                        "projects:detail",
                        project_id=details.item.survey_mission.project.id,
                    ),
                    icon=settings.icons.projects,
                ),
                webui_schemas.BreadcrumbItem(
                    name=details.item.survey_mission.name.en,
                    url=request.url_for(
                        "survey_missions:detail",
                        survey_mission_id=details.item.survey_mission.id,
                    ),
                    icon=settings.icons.survey_missions,
                ),
                webui_schemas.BreadcrumbItem(
                    name=details.item.name.en,
                    url=request.url_for(
                        "survey_related_records:detail",
                        survey_related_record_id=details.item.id,
                    ),
                    icon=settings.icons.survey_related_records,
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("Edit"), icon=settings.icons.edit_item
                ),
            ],
        },
    )


@csrf_protect
async def add_update_form_link(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: implement some logic to limit the number of links that can be added
    form_instance.links.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_update_form_link(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: Check we are not trying to remove an index that is invalid
    link_index = int(request.query_params.get("link_index", 0))
    form_instance.links.entries.pop(link_index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_update_form_related_to_record(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: implement some logic to limit the number of related_to relationships that can be added
    logger.debug(f"{len(form_instance.related_records.entries)=}")
    if len(form_instance.related_records.entries) > SURVEY_RELATED_RECORD_MAX_RELATED:
        # cannot add more
        ...
    form_instance.related_records.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )

    user = request.user if request.user.is_authenticated else None
    async with request.state.settings.get_db_session_maker()() as session:
        (
            initial_related_records_list,
            _,
        ) = await survey_related_record_ops.list_survey_related_records(
            session,
            initiator=user,
        )
    initial_related_records = [
        (i.id, i.name["en"]) for i in initial_related_records_list
    ]
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
        initial_related_records=initial_related_records,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_update_form_related_to_record(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: Check we are not trying to remove an index that is invalid
    index = int(request.query_params.get("index", 0))
    form_instance.related_records.entries.pop(index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_update_form_asset(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: implement some logic to limit the number of assets that can be added
    form_instance.assets.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_update_form_asset(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: Check we are not trying to remove an index that is invalid
    index = int(request.query_params.get("index", 0))
    form_instance.asset.entries.pop(index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_update_form_asset_link(request: Request):
    details = await _get_survey_related_record_details(request)
    # TODO: Check we have a valid index
    asset_index = int(request.path_params["asset_index"])
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    # TODO: implement some logic to limit the number of links that can be added
    form_instance.assets[asset_index].asset_links.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_update_form_asset_link(request: Request):
    details = await _get_survey_related_record_details(request)
    form_instance = await forms.SurveyRelatedRecordUpdateForm.from_request(request)
    try:
        asset_index = int(request.path_params["asset_index"])
        if asset_index < 0 or asset_index >= len(form_instance.assets.entries):
            raise RuntimeError("Invalid asset index")
    except (ValueError, KeyError):
        raise HTTPException(404, "Invalid asset index")
    try:
        link_index = int(request.query_params.get("link_index", 0))
        if link_index < 0 or link_index >= len(
            form_instance.assets[asset_index].links.entries
        ):
            raise RuntimeError("Invalid asset link index")
    except (ValueError, KeyError):
        raise HTTPException(404, "Invalid asset link index")

    form_instance.assets[asset_index].asset_links.entries.pop(link_index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/update-form.html"
    )
    rendered = template.render(
        form=form_instance,
        request=request,
        survey_related_record=details.item,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


async def list_by_name(request: Request):
    """Specialized endpoint that provides record names for building datalists."""
    current_language = request.state.language
    if (target_datalist_id := request.query_params.get("target")) is None:
        raise HTTPException(status_code=400, detail="target is required")
    if (search_param_name := request.query_params.get("searchParam")) is None:
        raise HTTPException(status_code=400, detail="searchParam is required")

    try:
        if (raw_search_params := request.query_params.get("datastar")) is None:
            raise HTTPException(status_code=400, detail="datastar is required")
        search_value = json.loads(raw_search_params).get(search_param_name)
    except json.decoder.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid search params")

    record_name_filter = filters.SearchNameFilter(
        internal_name=f"{current_language}_name_filter",
        public_name=f"{current_language}_name",
        value=search_value,
    )
    internal_filter_kwargs = record_name_filter.as_kwargs()
    logger.debug(f"{internal_filter_kwargs=}")
    user = request.user if request.user.is_authenticated else None
    async with request.state.settings.get_db_session_maker()() as session:
        items, _ = await survey_related_record_ops.list_survey_related_records(
            session,
            initiator=user,
            **internal_filter_kwargs,
        )
    serialized_items = [  # noqa
        webui_schemas.SurveyRelatedRecordReadListItem.from_db_instance(item)
        for item in items
    ]

    rendered_items = []
    for item in serialized_items:
        compound_name = build_related_record_compound_name(request, item)
        rendered_items.append(f'<option value="{compound_name}"></option>')

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            f'<datalist id="{target_datalist_id}">{"".join(rendered_items)}</datalist>',
        )

    return DatastarResponse(event_streamer())


async def get_list_component(request: Request):
    if (raw_search_params := request.query_params.get("datastar")) is not None:
        try:
            list_filters = filters.SurveyRelatedRecordListFilters.from_json(
                raw_search_params, request.state.language
            )
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid search params")
        else:
            internal_filter_kwargs = list_filters.as_kwargs()
            filter_query_string = list_filters.serialize_to_query_string()
    else:
        internal_filter_kwargs = {}
        filter_query_string = ""
    logger.debug(f"{internal_filter_kwargs=}")
    current_page = get_page_from_request_params(request)
    user = request.user if request.user.is_authenticated else None
    settings: config.SeisLabDataSettings = request.state.settings
    async with settings.get_db_session_maker()() as session:
        items, num_total = await survey_related_record_ops.list_survey_related_records(
            session,
            initiator=user,
            page=current_page,
            page_size=settings.pagination_page_size,
            include_total=True,
            **internal_filter_kwargs,
        )
        num_unfiltered_total = (
            await survey_related_record_ops.list_survey_related_records(
                session, initiator=user, include_total=True
            )
        )[1]
    pagination_info = get_pagination_info(
        current_page,
        settings.pagination_page_size,
        num_total,
        num_unfiltered_total,
        collection_url=str(request.url_for("survey_related_records:list")),
    )
    serialized_items = [
        webui_schemas.SurveyRelatedRecordReadListItem.from_db_instance(item)
        for item in items
    ]
    template_processor = request.state.templates
    template = template_processor.get_template(
        "survey-related-records/list-component.html"
    )
    rendered = template.render(
        request=request,
        items=serialized_items,
        update_current_url_with=filter_query_string,
        pagination=pagination_info,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.items_selector,
            mode=ElementPatchMode.REPLACE,
        )
        yield ServerSentEventGenerator.execute_script(
            UPDATE_BASEMAP_JS_SCRIPT.format(
                dumped_features=json.dumps(
                    geojson.to_feature_collection(serialized_items)
                )
            )
        )

    return DatastarResponse(event_streamer())


async def stream_to_list_page(request: Request):
    topic_names = [constants.NEW_TOPIC_SURVEY_RELATED_RECORDS]
    pubsub = await subscribers.open_topic_subscription(
        request.state.redis_client, topic_names
    )
    subscription = subscribers.iter_topic_messages(
        pubsub,
        topic_names,
        subscribers.HandlerContext(
            jinja_environment=request.state.templates.env,
            url_resolver=request.url_for,
            db_session_factory=request.state.settings.get_db_session_maker(),
            user=request.user if request.user.is_authenticated else None,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_list_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer(), status_code=200)


class SurveyRelatedRecordCollectionEndpoint(HTTPEndpoint):
    async def get(self, request: Request):
        """List survey-related records."""
        current_page = get_page_from_request_params(request)
        current_language = request.state.language
        list_filters = filters.SurveyRelatedRecordListFilters.from_params(
            request.query_params, current_language
        )
        settings: config.SeisLabDataSettings = request.state.settings
        user = request.user if request.user.is_authenticated else None
        async with settings.get_db_session_maker()() as session:
            some_db_projects = (
                await project_ops.list_projects(
                    session, initiator=user, include_total=False
                )
            )[0]
            some_projects = [
                project_schemas.ProjectReadListItem.from_db_instance(i)
                for i in some_db_projects
            ]
            some_db_missions = (
                await survey_mission_ops.list_survey_missions(
                    session, initiator=user, include_total=False
                )
            )[0]
            some_missions = [
                mission_schemas.SurveyMissionReadListItem.from_db_instance(i)
                for i in some_db_missions
            ]
            some_media_types = await asset_queries.list_media_types(session)

            dataset_category_filter_options = []
            for (
                dataset_category
            ) in await category_queries.collect_all_dataset_categories(session):
                dataset_category_filter_options.append(
                    (
                        dataset_category.id,
                        localization.translate_localizable_dict(
                            dataset_category.name, request.state.language
                        ),
                    )
                )
            workflow_stage_filter_options = []
            for workflow_stage in await stage_queries.collect_all_workflow_stages(
                session
            ):
                workflow_stage_filter_options.append(
                    (
                        workflow_stage.id,
                        localization.translate_localizable_dict(
                            workflow_stage.name, request.state.language
                        ),
                    )
                )
            (
                items,
                num_total,
            ) = await survey_related_record_ops.list_survey_related_records(
                session,
                initiator=user,
                page=current_page,
                page_size=settings.pagination_page_size,
                include_total=True,
                **list_filters.as_kwargs(),
            )
            num_unfiltered_total = (
                await survey_related_record_ops.list_survey_related_records(
                    session, initiator=user, include_total=True
                )
            )[1]
        template_processor = request.state.templates
        pagination_info = get_pagination_info(
            current_page,
            settings.pagination_page_size,
            num_total,
            num_unfiltered_total,
            collection_url=str(request.url_for("survey_related_records:list")),
        )
        if (current_bbox := list_filters.spatial_intersect_filter) is not None:
            min_lon, min_lat, max_lon, max_lat = current_bbox.value.bounds
        else:
            default_bbox = shapely.from_wkt(settings.webmap_default_bbox_wkt)
            min_lon, min_lat, max_lon, max_lat = default_bbox.bounds
        serialized_items = [
            webui_schemas.SurveyRelatedRecordReadListItem.from_db_instance(item)
            for item in items
        ]
        geojson_features = geojson.to_feature_collection(serialized_items)
        return template_processor.TemplateResponse(
            request,
            "survey-related-records/list.html",
            context={
                "items": serialized_items,
                "geojson_features": json.dumps(geojson_features),
                "pagination": pagination_info,
                "dataset_categories": dataset_category_filter_options,
                "workflow_stages": workflow_stage_filter_options,
                "filter_projects_datalist": [
                    build_project_compound_name(request, i) for i in some_projects
                ],
                "filter_missions_datalist": [
                    build_mission_compound_name(request, i) for i in some_missions
                ],
                "filter_media_types_datalist": some_media_types,
                "map_bounds": {
                    "min_lon": min_lon,
                    "min_lat": min_lat,
                    "max_lon": max_lon,
                    "max_lat": max_lat,
                },
                "current_temporal_extent": {
                    "begin": settings.default_temporal_extent_begin,
                    "end": settings.default_temporal_extent_end,
                },
                "breadcrumbs": [
                    webui_schemas.BreadcrumbItem(
                        name=_("Home"),
                        url=request.url_for("home"),
                        icon=settings.icons.home,
                    ),
                    webui_schemas.BreadcrumbItem(
                        name=_("Survey-related records"),
                        icon=settings.icons.survey_related_records,
                    ),
                ],
                "search_initial_value": list_filters.get_text_search_filter(
                    current_language
                ),
                "map_popup_detail_base_url": str(
                    request.url_for(
                        "survey_related_records:detail", survey_related_record_id="_"
                    )
                ).rpartition("/")[0],
            },
        )


class SurveyRelatedRecordDetailEndpoint(HTTPEndpoint):
    async def get(self, request: Request):
        """Get survey-related record details."""
        try:
            details = await _get_survey_related_record_details(request)
        except errors.SeisLabDataError:
            # TODO: figure out how to surface the error to the UI
            return RedirectResponse(request.url_for("home"))
        template_processor = request.state.templates
        return template_processor.TemplateResponse(
            request,
            "survey-related-records/detail.html",
            context={
                "request_id": uuid.uuid4(),
                "item": details.item,
                "permissions": details.permissions,
                "breadcrumbs": details.breadcrumbs,
            },
        )

    @csrf_protect
    @requires_auth
    async def delete(self, request: Request):
        survey_related_record_id = get_id_from_request_path(
            request, "survey_related_record_id", identifiers.SurveyRelatedRecordId
        )
        request_id = identifiers.RequestId(
            uuid.UUID(request.query_params["request_id"])
        )
        user = request.user
        async with request.state.settings.get_db_session_maker()() as session:
            if (
                await survey_related_record_ops.get_survey_related_record(
                    survey_related_record_id,
                    user,
                    session,
                )
            ) is None:
                raise HTTPException(
                    status_code=404,
                    detail=_(
                        f"Survey-related record {survey_related_record_id!r} not found."
                    ),
                )

        record_tasks.delete_survey_related_record.send(
            raw_request_id=str(request_id),
            raw_survey_related_record_id=str(survey_related_record_id),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )
        return Response(status_code=200)

    @csrf_protect
    @requires_auth
    async def put(self, request: Request):
        """Update an existing survey-related record."""
        template_processor: Jinja2Templates = request.state.templates
        user = request.user
        survey_related_record_id = get_id_from_request_path(
            request, "survey_related_record_id", identifiers.SurveyRelatedRecordId
        )
        async with request.state.settings.get_db_session_maker()() as session:
            if (
                survey_related_record_details
                := await survey_related_record_ops.get_survey_related_record(
                    survey_related_record_id,
                    user,
                    session,
                )
            ) is None:
                raise HTTPException(
                    404,
                    f"Survey-related record {survey_related_record_id!r} not found.",
                )

        survey_related_record, related_to, subject_for = survey_related_record_details
        parent_survey_mission_id = identifiers.SurveyMissionId(
            survey_related_record.survey_mission_id
        )
        form_instance = (
            await forms.SurveyRelatedRecordUpdateForm.get_validated_form_instance(
                request,
                survey_mission_id=parent_survey_mission_id,
                disregard_id=survey_related_record_id,
            )
        )
        logger.debug(f"{form_instance.has_validation_errors()=}")

        if form_instance.has_validation_errors():
            logger.debug("form did not validate")
            logger.debug(f"{form_instance.errors=}")

            async def stream_validation_failed_events():
                yield ServerSentEventGenerator.patch_signals({"submitting": False})
                template = template_processor.get_template(
                    "survey-related-records/update-form.html"
                )
                rendered = template.render(
                    request=request,
                    survey_related_record=survey_related_record,
                    form=form_instance,
                )
                yield ServerSentEventGenerator.patch_elements(
                    rendered,
                    selector=webui_schemas.selector_info.main_content_selector,
                    mode=ElementPatchMode.INNER,
                )
                yield ServerSentEventGenerator.execute_script(
                    "document.querySelector('.is-invalid')?.scrollIntoView({behavior: 'smooth', block: 'center'})"
                )

            # Datastar only processes SSE streams from 2xx responses; non-2xx are treated as errors
            return DatastarResponse(stream_validation_failed_events(), status_code=200)

        related_records = []
        for related_ in form_instance.related_records.entries:
            related_records.append(
                record_schemas.RelatedRecordCreate(
                    related_record_id=identifiers.SurveyRelatedRecordId(
                        uuid.UUID(
                            form_instance.parse_related_record_compound_name(
                                related_.related_record.data
                            )
                        )
                    ),
                    relationship=common_schemas.LocalizableDraftRelationship(
                        en=related_.relationship.en.data,
                        pt=related_.relationship.pt.data,
                    ),
                )
            )
        to_update = record_schemas.SurveyRelatedRecordUpdate(
            owner_id=user.id,
            survey_mission_id=parent_survey_mission_id,
            name=common_schemas.LocalizableDraftName(
                en=form_instance.name.en.data,
                pt=form_instance.name.pt.data,
            ),
            description=common_schemas.LocalizableDraftDescription(
                en=form_instance.description.en.data,
                pt=form_instance.description.pt.data,
            ),
            dataset_category_id=form_instance.dataset_category_id.data,
            workflow_stage_id=form_instance.workflow_stage_id.data,
            bbox_4326=(
                f"POLYGON(("
                f"{form_instance.bounding_box.min_lon.data} {form_instance.bounding_box.min_lat.data}, "
                f"{form_instance.bounding_box.max_lon.data} {form_instance.bounding_box.min_lat.data}, "
                f"{form_instance.bounding_box.max_lon.data} {form_instance.bounding_box.max_lat.data}, "
                f"{form_instance.bounding_box.min_lon.data} {form_instance.bounding_box.max_lat.data}, "
                f"{form_instance.bounding_box.min_lon.data} {form_instance.bounding_box.min_lat.data}"
                f"))"
            ),
            temporal_extent_begin=form_instance.temporal_extent_begin.data,
            temporal_extent_end=form_instance.temporal_extent_end.data,
            links=[
                common_schemas.LinkSchema(
                    url=lf.url.data,
                    media_type=lf.media_type.data,
                    relation=lf.relation.data,
                    link_description=common_schemas.LocalizableDraftDescription(
                        en=lf.link_description.en.data,
                        pt=lf.link_description.pt.data,
                    ),
                )
                for lf in form_instance.links.entries
            ],
            assets=[
                record_schemas.RecordAssetUpdate(
                    id=identifiers.RecordAssetId(uuid.UUID(af.asset_id.data)),
                    name=common_schemas.LocalizableDraftName(
                        en=af.asset_name.en.data,
                        pt=af.asset_name.pt.data,
                    ),
                    description=common_schemas.LocalizableDraftDescription(
                        en=af.asset_description.en.data,
                        pt=af.asset_description.pt.data,
                    ),
                    media_type=af.media_type.data,
                    relative_path=af.relative_path.data,
                    links=[
                        common_schemas.LinkSchema(
                            url=afl.url.data,
                            media_type=afl.media_type.data,
                            relation=afl.relation.data,
                            link_description=common_schemas.LocalizableDraftDescription(
                                en=afl.link_description.en.data,
                                pt=afl.link_description.pt.data,
                            ),
                        )
                        for afl in af.asset_links.entries
                    ],
                )
                for af in form_instance.assets.entries
            ],
            related_records=related_records,
        )

        record_tasks.update_survey_related_record.send(
            raw_request_id=str(form_instance.request_id.data),
            raw_survey_related_record_id=str(survey_related_record_id),
            raw_to_update=to_update.model_dump_json(exclude_unset=True),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )
        return Response(status_code=200)


@requires_auth
async def stream_to_new_page(request: Request):
    """Stream relevant updates for the new survey-related record page."""
    try:
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid request id") from err

    topic_names = [constants.NEW_TOPIC_SURVEY_RELATED_RECORDS]
    pubsub = await subscribers.open_topic_subscription(
        request.state.redis_client, topic_names
    )
    subscription = subscribers.iter_topic_messages(
        pubsub,
        topic_names,
        subscribers.HandlerContext(
            request_id=request_id,
            user=request.user,
            url_resolver=request.url_for,
            jinja_environment=request.state.templates.env,
            db_session_factory=request.state.settings.get_db_session_maker(),
            target_page=constants.PageType.RESOURCE_NEW,
            resource_type=constants.ResourceType.RECORD,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_new_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer())


async def stream_to_detail_page(request: Request):
    try:
        record_id = identifiers.SurveyRelatedRecordId(
            uuid.UUID(request.path_params["survey_related_record_id"])
        )
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(
            status_code=400, detail="Invalid survey_related_record id"
        ) from err
    session_maker = request.state.settings.get_db_session_maker()
    redis_client: Redis = request.state.redis_client
    user = request.user if request.user.is_authenticated else None

    topic_names = [constants.NEW_TOPIC_SURVEY_RELATED_RECORDS]
    pubsub = await subscribers.open_topic_subscription(redis_client, topic_names)
    subscription = subscribers.iter_topic_messages(
        pubsub,
        topic_names,
        subscribers.HandlerContext(
            resource_id=str(record_id),
            user=user,
            jinja_environment=request.state.templates.env,
            url_resolver=request.url_for,
            db_session_factory=session_maker,
            request_id=request_id,
            resource_type=constants.ResourceType.RECORD,
            target_page=constants.PageType.RESOURCE_DETAIL,
        ),
        message_handlers={
            "resource_modified": common_handlers.handle_resource_modification_detail_page,
            "resource_status_changed": common_handlers.handle_resource_status_changed_detail_page,
        },
    )

    async def event_streamer():
        async for datastar_event in subscription:
            yield datastar_event

    return DatastarResponse(event_streamer())


@requires_auth
async def stream_to_update_page(request: Request):
    try:
        record_id = identifiers.SurveyRelatedRecordId(
            uuid.UUID(request.path_params["survey_related_record_id"])
        )
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(
            status_code=400, detail="Invalid survey_related_record id"
        ) from err
    session_maker = request.state.settings.get_db_session_maker()
    redis_client: Redis = request.state.redis_client
    user = request.user

    topic_names = [constants.NEW_TOPIC_SURVEY_RELATED_RECORDS]
    pubsub = await subscribers.open_topic_subscription(redis_client, topic_names)
    subscription = subscribers.iter_topic_messages(
        pubsub,
        topic_names,
        subscribers.HandlerContext(
            resource_id=str(record_id),
            user=user,
            jinja_environment=request.state.templates.env,
            url_resolver=request.url_for,
            db_session_factory=session_maker,
            request_id=request_id,
            target_page=constants.PageType.RESOURCE_UPDATE,
        ),
        message_handlers={
            "resource_modified": common_handlers.handle_resource_modification_edit_page,
        },
    )

    async def event_streamer():
        async for datastar_event in subscription:
            yield datastar_event

    return DatastarResponse(event_streamer())


@csrf_protect
@requires_auth
async def trigger_publishing(request: Request):
    record_tasks.handle_survey_related_record_publication.send(
        raw_request_id=str(request.query_params.get("request_id", "")),
        raw_survey_related_record_id=request.path_params["survey_related_record_id"],
        raw_to_update=record_schemas.SurveyRelatedRecordPublication(
            published=True
        ).model_dump_json(exclude_unset=True),
        raw_initiator=json.dumps(dataclasses.asdict(request.user)),
    )  # noqa
    return Response(status_code=200)


@csrf_protect
@requires_auth
async def trigger_unpublishing(request: Request):
    record_tasks.handle_survey_related_record_publication.send(
        raw_request_id=str(request.query_params.get("request_id", "")),
        raw_survey_related_record_id=request.path_params["survey_related_record_id"],
        raw_to_update=record_schemas.SurveyRelatedRecordPublication(
            published=False
        ).model_dump_json(exclude_unset=True),
        raw_initiator=json.dumps(dataclasses.asdict(request.user)),
    )  # noqa
    return Response(status_code=200)


routes = [
    Route(
        "/{survey_mission_id}/new",
        get_creation_form,
        methods=["GET"],
        name="get_creation_form",
    ),
    Route(
        "/{survey_mission_id}/new/{request_id}/stream",
        stream_to_new_page,
        methods=["GET"],
        name="new_stream",
    ),
    Route(
        "/{survey_mission_id}/new/add-form-link",
        add_creation_form_link,
        methods=["POST"],
        name="add_form_link",
    ),
    Route(
        "/{survey_mission_id}/new/remove-form-link",
        remove_creation_form_link,
        methods=["POST"],
        name="remove_form_link",
    ),
    Route(
        "/{survey_mission_id}/new/add-asset-form",
        add_creation_form_asset,
        methods=["POST"],
        name="add_asset_form",
    ),
    Route(
        "/{survey_mission_id}/new/remove-asset-form",
        remove_creation_form_asset,
        methods=["POST"],
        name="remove_asset_form",
    ),
    Route(
        "/{survey_mission_id}/new/add-asset-link-form/{asset_index}",
        add_creation_form_asset_link,
        methods=["POST"],
        name="add_asset_link_form",
    ),
    Route(
        "/{survey_mission_id}/new/remove-asset-link-form/{asset_index}",
        remove_creation_form_asset_link,
        methods=["POST"],
        name="remove_asset_link_form",
    ),
    Route(
        "/{survey_mission_id}/new/add-related-record-form",
        add_creation_form_related_record,
        methods=["POST"],
        name="add_related_record_form",
    ),
    Route(
        "/{survey_mission_id}/new/remove-related-record-form",
        remove_creation_form_related_record,
        methods=["POST"],
        name="remove_related_record_form",
    ),
    Route(
        "/",
        SurveyRelatedRecordCollectionEndpoint,
        methods=["GET"],
        name="list",
    ),
    Route(
        "/search",
        get_list_component,
        methods=["GET"],
        name="get_list_component",
    ),
    Route(
        "/stream",
        stream_to_list_page,
        methods=["GET"],
        name="list_stream",
    ),
    Route(
        "/filter-by-name",
        list_by_name,
        methods=["GET"],
        name="list_by_name",
    ),
    Route(
        "/{survey_related_record_id}",
        SurveyRelatedRecordDetailEndpoint,
        name="detail",
    ),
    Route(
        "/{survey_related_record_id}/stream/{request_id}",
        stream_to_detail_page,
        methods=["GET"],
        name="detail_stream",
    ),
    Route(
        "/{survey_related_record_id}/details",
        get_details_component,
        methods=["GET"],
        name="get_details_component",
    ),
    Route(
        "/{survey_related_record_id}/update",
        get_update_form,
        methods=["GET"],
        name="get_update_form",
    ),
    Route(
        "/{survey_related_record_id}/update/stream/{request_id}",
        stream_to_update_page,
        methods=["GET"],
        name="update_stream",
    ),
    Route(
        "/{survey_related_record_id}/update/add-form-link",
        add_update_form_link,
        methods=["POST"],
        name="add_update_form_link",
    ),
    Route(
        "/{survey_related_record_id}/update/remove-form-link",
        remove_update_form_link,
        methods=["POST"],
        name="remove_update_form_link",
    ),
    Route(
        "/{survey_related_record_id}/update/add-asset-form",
        add_update_form_asset,
        methods=["POST"],
        name="add_update_form_asset",
    ),
    Route(
        "/{survey_related_record_id}/update/remove-asset-form",
        remove_update_form_asset,
        methods=["POST"],
        name="remove_update_form_asset",
    ),
    Route(
        "/{survey_related_record_id}/update/add-asset-link-form/{asset_index}",
        add_update_form_asset_link,
        methods=["POST"],
        name="add_update_form_asset_link",
    ),
    Route(
        "/{survey_related_record_id}/update/remove-asset-link-form/{asset_index}",
        remove_update_form_asset_link,
        methods=["POST"],
        name="remove_update_form_asset_link",
    ),
    Route(
        "/{survey_related_record_id}/update/add-related-record-form",
        add_update_form_related_to_record,
        methods=["POST"],
        name="add_update_form_related_to_record",
    ),
    Route(
        "/{survey_related_record_id}/update/remove-related-record-form",
        remove_update_form_related_to_record,
        methods=["POST"],
        name="remove_update_form_related_to_record",
    ),
    Route(
        "/{survey_related_record_id}/publish",
        trigger_publishing,
        methods=["POST"],
        name="publish",
    ),
    Route(
        "/{survey_related_record_id}/unpublish",
        trigger_unpublishing,
        methods=["POST"],
        name="unpublish",
    ),
]
