import dataclasses
import json
import logging
import uuid

import shapely
from datastar_py import ServerSentEventGenerator
from datastar_py.consts import ElementPatchMode
from datastar_py.starlette import DatastarResponse
from redis.asyncio import Redis
from starlette.endpoints import HTTPEndpoint
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.requests import Request
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette_babel import gettext_lazy as _
from starlette_wtf import csrf_protect

from ... import (
    config,
    constants,
    errors,
    geojson,
    subscribers,
)
from ...operations import (
    projects as project_ops,
    surveymissions as survey_mission_ops,
)
from ...permissions import (
    projects as project_permissions,
    surveymissions as mission_permissions,
)
from ...tasks import (
    projects as project_tasks,
    surveymissions as survey_mission_tasks,
)
from ...schemas import (
    common as common_schemas,
    identifiers,
    projects as project_schemas,
    surveymissions as mission_schemas,
    webui as webui_schemas,
)
from ..streamhandlers import common as common_handlers
from .. import filters
from ..forms import (
    projects as project_forms,
    surveymissions as mission_forms,
)
from .auth import (
    requires_auth,
)
from .common import (
    get_id_from_request_path,
    get_page_from_request_params,
    get_pagination_info,
    UPDATE_BASEMAP_JS_SCRIPT,
)
from .datalist import get_projects_datalist

logger = logging.getLogger(__name__)


@csrf_protect
@requires_auth
async def get_project_creation_form(request: Request):
    """Return a form suitable for creating a new project."""
    form_instance = await project_forms.ProjectCreateForm.from_formdata(request)
    form_instance.request_id.data = str(identifiers.RequestId(uuid.uuid4()))
    template_processor: Jinja2Templates = request.state.templates
    settings: config.SeisLabDataSettings = request.state.settings
    return template_processor.TemplateResponse(
        request,
        "projects/create-form-page.html",
        context={
            "form": form_instance,
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
                    name=_("New project"), icon=settings.icons.new_item
                ),
            ],
        },
    )


@csrf_protect
@requires_auth
async def get_project_update_form(request: Request):
    """Return a form suitable for updating an existing project."""
    user = request.user if request.user.is_authenticated else None
    session_maker = request.state.settings.get_db_session_maker()
    project_id = get_id_from_request_path(request, "project_id", identifiers.ProjectId)
    async with session_maker() as session:
        try:
            project = await project_ops.get_project(
                project_id,
                user,
                session,
            )
        except errors.SeisLabDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if project is None:
            raise HTTPException(
                status_code=404, detail=_(f"Project {project_id!r} not found.")
            )
    current_bbox = (
        shapely.from_wkb(project.bbox_4326.data)
        if project.bbox_4326 is not None
        else None
    )
    update_form = project_forms.ProjectUpdateForm(
        request=request,
        data={
            "name": {
                "en": project.name.get("en", ""),
                "pt": project.name.get("pt", ""),
            },
            "description": {
                "en": project.description.get("en", ""),
                "pt": project.description.get("pt", ""),
            },
            "root_path": project.root_path,
            "bounding_box": {
                "min_lon": current_bbox.bounds[0],
                "min_lat": current_bbox.bounds[1],
                "max_lon": current_bbox.bounds[2],
                "max_lat": current_bbox.bounds[3],
            }
            if current_bbox
            else None,
            "temporal_extent_begin": project.temporal_extent_begin,
            "temporal_extent_end": project.temporal_extent_end,
            "links": [
                {
                    "url": li.get("url", ""),
                    "media_type": li.get("media_type", ""),
                    "relation": li.get("relation", ""),
                    "link_description": {
                        "en": li.get("link_description", {}).get("en", ""),
                        "pt": li.get("link_description", {}).get("pt", ""),
                    },
                }
                for li in project.links
            ],
            "discovery_configuration": (
                json.dumps(project.discovery_configuration)
                if project.discovery_configuration
                else ""
            ),
        },
    )
    update_form.request_id.data = uuid.uuid4()
    template_processor: Jinja2Templates = request.state.templates
    settings: config.SeisLabDataSettings = request.state.settings
    return template_processor.TemplateResponse(
        request,
        "projects/update-form-page.html",
        context={
            "project": webui_schemas.ProjectReadDetail.from_db_instance(project),
            "form": update_form,
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
                    name=project.name["en"],
                    url=request.url_for("projects:detail", project_id=project_id),
                    icon=settings.icons.projects,
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("Edit"), icon=settings.icons.edit_item
                ),
            ],
        },
    )


@requires_auth
async def get_project_details_component(request: Request):
    details = await _get_project_details(request)
    template_processor = request.state.templates
    template = template_processor.get_template("projects/detail-component.html")
    rendered = template.render(
        request=request,
        project=details.item,
        pagination=details.pagination,
        survey_missions=details.children,
        search_initial_value=details.children_filter,
        permissions=details.permissions,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


async def stream_to_list_page(request: Request):
    """Stream relevant updates for the project list page."""

    topic_names = [constants.NEW_TOPIC_PROJECTS]
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
            target_page=constants.PageType.RESOURCE_LIST,
            resource_type=constants.ResourceType.PROJECT,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_list_page,
            # "project_creation_successful": project_handlers.handle_list_page_project_modification,
            # "project_deletion_successful": project_handlers.handle_list_page_project_modification,
            # "project_update_successful": project_handlers.handle_list_page_project_modification,
            # "project_created": project_handlers.handle_list_page_project_modification,
            # "project_updated": project_handlers.handle_list_page_project_modification,
            # "project_deleted": project_handlers.handle_list_page_project_modification,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer(), status_code=200)


@requires_auth
async def stream_to_new_page(request: Request):
    """Stream relevant updates for the new project page."""
    try:
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid request id") from err

    # TODO: should we update the form fields with handlers too?
    topic_names = [constants.NEW_TOPIC_PROJECTS]
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
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_new_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer())


@requires_auth
async def stream_to_update_page(request: Request):
    """Stream relevant updates for the project update page."""
    try:
        project_id = identifiers.ProjectId(uuid.UUID(request.path_params["project_id"]))
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(
            status_code=400, detail="Invalid project or request id"
        ) from err
    session_maker = request.state.settings.get_db_session_maker()
    redis_client: Redis = request.state.redis_client
    user = request.user if request.user.is_authenticated else None

    topic_names = [constants.NEW_TOPIC_PROJECTS]
    pubsub = await subscribers.open_topic_subscription(redis_client, topic_names)
    subscription = subscribers.iter_topic_messages(
        pubsub,
        topic_names,
        subscribers.HandlerContext(
            resource_id=str(project_id),
            user=user,
            jinja_environment=request.state.templates.env,
            url_resolver=request.url_for,
            db_session_factory=session_maker,
            request_id=request_id,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_edit_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer())


async def stream_to_detail_page(request: Request):
    """Stream relevant updates for the project details page."""
    try:
        project_id = identifiers.ProjectId(uuid.UUID(request.path_params["project_id"]))
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(
            status_code=400, detail="Invalid project or request id"
        ) from err
    session_maker = request.state.settings.get_db_session_maker()
    redis_client: Redis = request.state.redis_client
    user = request.user if request.user.is_authenticated else None

    topic_names = [
        constants.NEW_TOPIC_PROJECTS,
        constants.NEW_TOPIC_SURVEY_MISSIONS,
    ]
    pubsub = await subscribers.open_topic_subscription(redis_client, topic_names)
    subscription = subscribers.iter_topic_messages(
        pubsub,
        topic_names,
        subscribers.HandlerContext(
            resource_id=str(project_id),
            user=user,
            jinja_environment=request.state.templates.env,
            url_resolver=request.url_for,
            db_session_factory=session_maker,
            request_id=request_id,
            resource_type=constants.ResourceType.PROJECT,
            target_page=constants.PageType.RESOURCE_DETAIL,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_detail_page,
            "resource_status_changed": common_handlers.handle_resource_status_changed_detail_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer())


async def _get_project_details(request: Request) -> webui_schemas.ProjectDetails:
    """utility function to get project details and its survey missions."""
    survey_mission_current_page = get_page_from_request_params(request)
    current_language = request.state.language
    survey_mission_list_filters = filters.SurveyMissionListFilters.from_params(
        request.query_params, current_language
    )
    user = request.user if request.user.is_authenticated else None
    project_id = get_id_from_request_path(request, "project_id", identifiers.ProjectId)
    settings: config.SeisLabDataSettings = request.state.settings
    async with settings.get_db_session_maker()() as session:
        try:
            project = await project_ops.get_project(
                project_id,
                user,
                session,
            )
        except errors.SeisLabDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if project is None:
            raise HTTPException(
                status_code=404, detail=_(f"Project {project_id!r} not found.")
            )
        survey_missions, total = await survey_mission_ops.list_survey_missions(
            session,
            user,
            project_id=project_id,
            include_total=True,
            page=survey_mission_current_page,
            page_size=settings.pagination_page_size,
            **survey_mission_list_filters.as_kwargs(),
        )
    return webui_schemas.ProjectDetails(
        item=webui_schemas.ProjectReadDetail.from_db_instance(project),
        children=[
            webui_schemas.SurveyMissionReadListItem.from_db_instance(sm)
            for sm in survey_missions
        ],
        children_filter=survey_mission_list_filters.get_text_search_filter(
            current_language
        ),
        pagination=get_pagination_info(
            survey_mission_current_page,
            settings.pagination_page_size,
            total,
            total,
            collection_url=str(
                request.url_for("projects:detail", project_id=project_id)
            ),
        ),
        permissions=webui_schemas.UserPermissionDetails(
            can_delete=project_permissions.can_delete_project(user, project),
            can_update=project_permissions.can_update_project(user, project),
            can_create_children=mission_permissions.can_create_survey_mission(
                user, project
            ),
            can_validate=project_permissions.can_validate_project(user, project),
            can_discover=project_permissions.can_discover_project(user, project),
        ),
        breadcrumbs=[
            webui_schemas.BreadcrumbItem(
                name=_("Home"),
                url=str(request.url_for("home")),
                icon=settings.icons.home,
            ),
            webui_schemas.BreadcrumbItem(
                name=_("Projects"),
                url=request.url_for("projects:list"),
                icon=settings.icons.projects,
            ),
            webui_schemas.BreadcrumbItem(
                name=project.name["en"],
                icon=settings.icons.projects,
            ),
        ],
    )


async def get_list_component(request: Request):
    if (raw_search_params := request.query_params.get("datastar")) is not None:
        try:
            list_filters = filters.ProjectListFilters.from_json(
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
    current_page = get_page_from_request_params(request)
    settings: config.SeisLabDataSettings = request.state.settings
    user = request.user if request.user.is_authenticated else None
    async with settings.get_db_session_maker()() as session:
        items, num_total = await project_ops.list_projects(
            session,
            initiator=user,
            page=current_page,
            page_size=settings.pagination_page_size,
            include_total=True,
            **internal_filter_kwargs,
        )
        num_unfiltered_total = (
            await project_ops.list_projects(session, initiator=user, include_total=True)
        )[1]

    pagination_info = get_pagination_info(
        current_page,
        settings.pagination_page_size,
        num_total,
        num_unfiltered_total,
        collection_url=str(request.url_for("projects:list")),
    )
    serialized_items = [
        project_schemas.ProjectReadListItem.from_db_instance(i) for i in items
    ]
    template_processor = request.state.templates
    template = template_processor.get_template("projects/list-component.html")
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


class ProjectCollectionEndpoint(HTTPEndpoint):
    """Manage the collection of projects."""

    async def get(self, request: Request):
        """List projects."""
        settings: config.SeisLabDataSettings = request.state.settings
        user = request.user if request.user.is_authenticated else None
        current_page = get_page_from_request_params(request)
        current_language = request.state.language
        list_filters = filters.ProjectListFilters.from_params(
            request.query_params, current_language
        )
        async with settings.get_db_session_maker()() as session:
            items, num_total = await project_ops.list_projects(
                session,
                initiator=user,
                page=current_page,
                page_size=settings.pagination_page_size,
                include_total=True,
                **list_filters.as_kwargs(),
            )
            num_unfiltered_total = (
                await project_ops.list_projects(
                    session, initiator=user, include_total=True
                )
            )[1]
        template_processor = request.state.templates
        pagination_info = get_pagination_info(
            current_page,
            settings.pagination_page_size,
            num_total,
            num_unfiltered_total,
            collection_url=str(request.url_for("projects:list")),
        )
        if (current_bbox := list_filters.spatial_intersect_filter) is not None:
            min_lon, min_lat, max_lon, max_lat = current_bbox.value.bounds
        else:
            default_bbox = shapely.from_wkt(settings.webmap_default_bbox_wkt)
            min_lon, min_lat, max_lon, max_lat = default_bbox.bounds

        serialized_items = [
            project_schemas.ProjectReadListItem.from_db_instance(i) for i in items
        ]
        geojson_features = geojson.to_feature_collection(serialized_items)
        return template_processor.TemplateResponse(
            request,
            "projects/list.html",
            context={
                "items": serialized_items,
                "geojson_features": json.dumps(geojson_features),
                "pagination": pagination_info,
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
                        icon=settings.icons.home,
                        url=request.url_for("home"),
                    ),
                    webui_schemas.BreadcrumbItem(
                        name=_("Projects"),
                        icon=settings.icons.projects,
                    ),
                ],
                "user_can_create": project_permissions.can_create_project(user),
                "search_initial_value": list_filters.get_text_search_filter(
                    current_language
                ),
                "map_popup_detail_base_url": str(
                    request.url_for("projects:detail", project_id="_")
                ).rpartition("/")[0],
            },
        )

    @csrf_protect
    @requires_auth
    async def post(self, request: Request):
        """Create a new project."""
        template_processor: Jinja2Templates = request.state.templates
        user = request.user
        form_instance = (
            await project_forms.ProjectCreateForm.get_validated_form_instance(request)
        )
        if form_instance.has_validation_errors():
            logger.debug("form did not validate")

            async def event_streamer():
                yield ServerSentEventGenerator.patch_signals({"submitting": False})
                template = template_processor.get_template("projects/create-form.html")
                rendered = template.render(
                    request=request,
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
            return DatastarResponse(event_streamer(), status_code=200)

        to_create = project_schemas.ProjectCreate(
            id=identifiers.ProjectId(uuid.uuid4()),
            owner_id=user.id,
            name=common_schemas.LocalizableDraftName(
                en=form_instance.name.en.data,
                pt=form_instance.name.pt.data,
            ),
            description=common_schemas.LocalizableDraftDescription(
                en=form_instance.description.en.data,
                pt=form_instance.description.pt.data,
            ),
            root_path=form_instance.root_path.data,
            bbox_4326=(
                f"POLYGON(("
                f"{form_instance.bounding_box.min_lon.data} {form_instance.bounding_box.min_lat.data}, "
                f"{form_instance.bounding_box.max_lon.data} {form_instance.bounding_box.min_lat.data}, "
                f"{form_instance.bounding_box.max_lon.data} {form_instance.bounding_box.max_lat.data}, "
                f"{form_instance.bounding_box.min_lon.data} {form_instance.bounding_box.max_lat.data}, "
                f"{form_instance.bounding_box.min_lon.data} {form_instance.bounding_box.min_lat.data}"
                f"))"
            ),
            discovery_configuration=form_instance.discovery_configuration.data,
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
        )

        request_id = identifiers.RequestId(uuid.UUID(form_instance.request_id.data))
        project_tasks.create_project.send(
            raw_request_id=str(request_id),
            raw_to_create=to_create.model_dump_json(),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)


class ProjectDetailEndpoint(HTTPEndpoint):
    """Manage a single project and its collection of survey missions."""

    async def get(self, request: Request):
        """
        Get project details and provide a paginated list of its survey missions.
        """

        details = await _get_project_details(request)
        template_processor = request.state.templates

        return template_processor.TemplateResponse(
            request,
            "projects/detail.html",
            context={
                "request_id": uuid.uuid4(),
                "item": details.item,
                "pagination": details.pagination,
                "survey_missions": details.children,
                "search_initial_value": details.children_filter,
                "permissions": details.permissions,
                "breadcrumbs": details.breadcrumbs,
            },
        )

    @csrf_protect
    @requires_auth
    async def put(self, request: Request):
        """Update an existing project."""
        template_processor: Jinja2Templates = request.state.templates
        user = request.user
        session_maker = request.state.settings.get_db_session_maker()
        project_id = get_id_from_request_path(
            request, "project_id", identifiers.ProjectId
        )
        async with session_maker() as session:
            if (
                project := await project_ops.get_project(project_id, user, session)
            ) is None:
                raise HTTPException(404, f"Project {project_id!r} not found.")
        form_instance = (
            await project_forms.ProjectUpdateForm.get_validated_form_instance(
                request, disregard_id=project_id
            )
        )

        if form_instance.has_validation_errors():
            logger.debug("form did not validate")
            logger.debug(f"{form_instance.errors=}")

            async def event_streamer():
                yield ServerSentEventGenerator.patch_signals({"updating": False})
                template = template_processor.get_template("projects/update-form.html")
                rendered = template.render(
                    request=request,
                    project=webui_schemas.ProjectReadDetail.from_db_instance(project),
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
            return DatastarResponse(event_streamer(), status_code=200)

        raw_dc = form_instance.discovery_configuration.data
        to_update = project_schemas.ProjectUpdate(
            owner_id=user.id,
            name=common_schemas.LocalizableDraftName(
                en=form_instance.name.en.data,
                pt=form_instance.name.pt.data,
            ),
            description=common_schemas.LocalizableDraftDescription(
                en=form_instance.description.en.data,
                pt=form_instance.description.pt.data,
            ),
            root_path=form_instance.root_path.data,
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
            discovery_configuration=json.loads(raw_dc) if raw_dc else None,
        )
        project_tasks.update_project.send(
            raw_request_id=str(identifiers.RequestId(uuid.uuid4())),
            raw_project_id=str(project_id),
            raw_to_update=to_update.model_dump_json(),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)

    @csrf_protect
    @requires_auth
    async def delete(self, request: Request):
        """Delete a project."""
        request_id = identifiers.RequestId(
            uuid.UUID(request.query_params["request_id"])
        )
        user = request.user
        session_maker = request.state.settings.get_db_session_maker()
        project_id = get_id_from_request_path(
            request, "project_id", identifiers.ProjectId
        )
        async with session_maker() as session:
            project = await project_ops.get_project(
                project_id,
                user,
                session,
            )
            if project is None:
                raise HTTPException(
                    status_code=404, detail=_(f"Project {project_id!r} not found.")
                )
        project_tasks.delete_project.send(
            raw_request_id=str(request_id),
            raw_project_id=str(project_id),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)

    @csrf_protect
    @requires_auth
    async def post(self, request: Request):
        """Create a new survey mission belonging to the project."""
        user = request.user
        project_id = get_id_from_request_path(
            request, "project_id", identifiers.ProjectId
        )
        session_maker = request.state.settings.get_db_session_maker()
        template_processor: Jinja2Templates = request.state.templates
        creation_form = await mission_forms.SurveyMissionCreateForm.from_formdata(
            request
        )
        async with session_maker() as session:
            try:
                project = await project_ops.get_project(project_id, user, session)
            except errors.SeisLabDataError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if project is None:
                raise HTTPException(
                    status_code=404, detail=_(f"Project {project_id!r} not found.")
                )
        await creation_form.validate_on_submit()
        creation_form.validate_with_schema()
        async with session_maker() as session:
            await creation_form.check_if_english_name_is_unique_for_project(
                session, project_id
            )

        if creation_form.has_validation_errors():

            async def stream_validation_failed_events():
                logger.debug("form did not validate")
                yield ServerSentEventGenerator.patch_signals({"submitting": False})
                template = template_processor.get_template(
                    "survey-missions/create-form.html"
                )
                rendered = template.render(
                    request=request,
                    form=creation_form,
                    project=project,
                )
                yield ServerSentEventGenerator.patch_elements(
                    rendered,
                    selector=webui_schemas.selector_info.main_content_selector,
                    mode=ElementPatchMode.INNER,
                )

            # Datastar only processes SSE streams from 2xx responses; non-2xx are treated as errors
            return DatastarResponse(stream_validation_failed_events(), status_code=200)

        to_create = mission_schemas.SurveyMissionCreate(
            id=identifiers.SurveyMissionId(uuid.uuid4()),
            project_id=identifiers.ProjectId(project.id),
            owner_id=user.id,
            name=common_schemas.LocalizableDraftName(
                en=creation_form.name.en.data,
                pt=creation_form.name.pt.data,
            ),
            description=common_schemas.LocalizableDraftDescription(
                en=creation_form.description.en.data,
                pt=creation_form.description.pt.data,
            ),
            relative_path=creation_form.relative_path.data,
            bbox_4326=(
                f"POLYGON(("
                f"{creation_form.bounding_box.min_lon.data} {creation_form.bounding_box.min_lat.data}, "
                f"{creation_form.bounding_box.max_lon.data} {creation_form.bounding_box.min_lat.data}, "
                f"{creation_form.bounding_box.max_lon.data} {creation_form.bounding_box.max_lat.data}, "
                f"{creation_form.bounding_box.min_lon.data} {creation_form.bounding_box.max_lat.data}, "
                f"{creation_form.bounding_box.min_lon.data} {creation_form.bounding_box.min_lat.data}"
                f"))"
            ),
            temporal_extent_begin=creation_form.temporal_extent_begin.data,
            temporal_extent_end=creation_form.temporal_extent_end.data,
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
                for lf in creation_form.links.entries
            ],
        )
        request_id = identifiers.RequestId(uuid.UUID(creation_form.request_id.data))
        survey_mission_tasks.create_survey_mission.send(
            raw_request_id=str(request_id),
            raw_to_create=to_create.model_dump_json(),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)


async def get_project_missions_list_component(request: Request):
    """Return a paginated, filtered list of survey missions belonging to a project."""
    project_id = get_id_from_request_path(request, "project_id", identifiers.ProjectId)
    if (raw_search_params := request.query_params.get("datastar")) is not None:
        try:
            list_filters = filters.SurveyMissionListFilters.from_json(
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
    current_page = get_page_from_request_params(request)
    settings: config.SeisLabDataSettings = request.state.settings
    user = request.user if request.user.is_authenticated else None
    async with settings.get_db_session_maker()() as session:
        items, num_total = await survey_mission_ops.list_survey_missions(
            session,
            initiator=user,
            project_id=project_id,
            page=current_page,
            page_size=settings.pagination_page_size,
            include_total=True,
            **internal_filter_kwargs,
        )
        num_unfiltered_total = (
            await survey_mission_ops.list_survey_missions(
                session, initiator=user, project_id=project_id, include_total=True
            )
        )[1]
    pagination_info = get_pagination_info(
        current_page,
        settings.pagination_page_size,
        num_total,
        num_unfiltered_total,
        collection_url=str(request.url_for("projects:detail", project_id=project_id)),
    )
    serialized_items = [
        webui_schemas.SurveyMissionReadListItem.from_db_instance(i) for i in items
    ]
    template_processor = request.state.templates
    template = template_processor.get_template("survey-missions/list-component.html")
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

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_create_project_form_link(request: Request):
    """Add a form link to a create_project form."""
    creation_form = await project_forms.ProjectCreateForm.from_formdata(request)
    creation_form.links.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template("projects/create-form.html")
    rendered = template.render(
        form=creation_form,
        request=request,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_create_project_form_link(request: Request):
    """Remove a form link from a create_project form."""
    creation_form = await project_forms.ProjectCreateForm.from_formdata(request)
    link_index = int(request.query_params["link_index"])
    creation_form.links.entries.pop(link_index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template("projects/create-form.html")
    rendered = template.render(
        form=creation_form,
        request=request,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def add_update_project_form_link(request: Request):
    """Add a form link to an update_project form."""
    details = await _get_project_details(request)
    form_ = await project_forms.ProjectUpdateForm.from_formdata(request)
    form_.links.append_entry()
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template("projects/update-form.html")
    rendered = template.render(
        form=form_,
        project=details.item,
        request=request,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
async def remove_update_project_form_link(request: Request):
    """Remove a form link from an update_project form."""
    details = await _get_project_details(request)
    form_ = await project_forms.ProjectUpdateForm.from_formdata(request)
    link_index = int(request.query_params["link_index"])
    form_.links.entries.pop(link_index)
    template_processor: Jinja2Templates = request.state.templates
    template = template_processor.get_template("projects/update-form.html")
    rendered = template.render(
        form=form_,
        project=details.item,
        request=request,
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.main_content_selector,
            mode=ElementPatchMode.INNER,
        )

    return DatastarResponse(event_streamer())


routes = [
    Route("/", ProjectCollectionEndpoint, name="list"),
    Route("/datalist", get_projects_datalist, name="get_datalist"),
    Route("/stream", stream_to_list_page, name="list_stream"),
    Route("/search", get_list_component, name="get_list_component"),
    Route(
        "/new/add-form-link",
        add_create_project_form_link,
        methods=["POST"],
        name="add_form_link",
    ),
    Route(
        "/new/remove-form-link",
        remove_create_project_form_link,
        methods=["POST"],
        name="remove_form_link",
    ),
    Route(
        "/new",
        get_project_creation_form,
        methods=["GET"],
        name="get_creation_form",
    ),
    Route(
        "/new/{request_id}/stream",
        stream_to_new_page,
        methods=["GET"],
        name="new_stream",
    ),
    Route(
        "/{project_id}/update",
        get_project_update_form,
        methods=["GET"],
        name="get_update_form",
    ),
    Route(
        "/{project_id}/update/stream/{request_id}",
        stream_to_update_page,
        methods=["GET"],
        name="update_stream",
    ),
    Route(
        "/{project_id}/add-update-form-link",
        add_update_project_form_link,
        methods=["POST"],
        name="add_update_form_link",
    ),
    Route(
        "/{project_id}/remove-update-form-link",
        remove_update_project_form_link,
        methods=["POST"],
        name="remove_update_form_link",
    ),
    Route(
        "/{project_id}/details",
        get_project_details_component,
        methods=["GET"],
        name="get_details_component",
    ),
    Route(
        "/{project_id}/missions",
        get_project_missions_list_component,
        methods=["GET"],
        name="get_project_missions_list_component",
    ),
    Route(
        "/{project_id}/stream/{request_id}",
        stream_to_detail_page,
        methods=["GET"],
        name="detail_stream",
    ),
    Route(
        "/{project_id}",
        ProjectDetailEndpoint,
        name="detail",
    ),
]
