import dataclasses
import json
import logging
import uuid

from datastar_py import ServerSentEventGenerator
from datastar_py.consts import ElementPatchMode
from datastar_py.starlette import DatastarResponse
from starlette.endpoints import HTTPEndpoint
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette_babel import gettext_lazy as _
from starlette_wtf import csrf_protect

from ... import (
    config,
    constants,
    errors,
    subscribers,
)
from ...db.queries import datasetcategories as category_queries
from ...permissions import datasetcategories as category_permissions
from ...schemas import (
    datasetcategories as category_schemas,
    identifiers,
    webui as webui_schemas,
)
from ...tasks import datasetcategories as category_tasks
from .. import (
    filters,
)
from ..forms import datasetcategories as category_forms
from ..streamhandlers import common as common_handlers
from .auth import requires_auth
from .common import (
    get_page_from_request_params,
    get_pagination_info,
)

logger = logging.getLogger(__name__)


async def get_list_component(request: Request):
    if (raw_search_params := request.query_params.get("datastar")) is not None:
        try:
            list_filters = filters.DatasetCategoryListFilters.from_json(
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
        items, num_total = await category_queries.list_dataset_categories(
            session,
            page=current_page,
            page_size=settings.pagination_page_size,
            include_total=True,
            **internal_filter_kwargs,
        )
        num_unfiltered_total = (
            await category_queries.list_dataset_categories(session, include_total=True)
        )[1]
    pagination_info = get_pagination_info(
        current_page,
        settings.pagination_page_size,
        num_total,
        num_unfiltered_total,
        collection_url=str(request.url_for("dataset_categories:list")),
    )
    serialized_items = [
        category_schemas.DatasetCategoryReadListItem.from_db_instance(i) for i in items
    ]
    template_processor = request.state.templates
    template = template_processor.get_template("datasetcategories/list-component.html")
    rendered = template.render(
        request=request,
        items=serialized_items,
        update_current_url_with=filter_query_string,
        pagination=pagination_info,
        permissions={
            "can_create": category_permissions.can_create_dataset_category(user),
            "can_update": category_permissions.can_update_dataset_category(user),
            "can_delete": category_permissions.can_delete_dataset_category(user),
        }
        if user is not None
        else {"can_create": False, "can_update": False, "can_delete": False},
        search_initial_value=list_filters.get_text_search_filter(
            request.state.language
        ),
    )

    async def event_streamer():
        yield ServerSentEventGenerator.patch_elements(
            rendered,
            selector=webui_schemas.selector_info.items_selector,
            mode=ElementPatchMode.REPLACE,
        )

    return DatastarResponse(event_streamer())


@csrf_protect
@requires_auth
async def get_creation_form(request: Request):
    """Return a form suitable for creating a new dataset category."""
    form_instance = await category_forms.DatasetCategoryCreateForm.from_formdata(
        request
    )
    form_instance.request_id.data = str(identifiers.RequestId(uuid.uuid4()))
    template_processor: Jinja2Templates = request.state.templates
    settings: config.SeisLabDataSettings = request.state.settings
    return template_processor.TemplateResponse(
        request,
        "datasetcategories/create-form-page.html",
        context={
            "form": form_instance,
            "breadcrumbs": [
                webui_schemas.BreadcrumbItem(
                    name=_("Home"),
                    url=request.url_for("home"),
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("Dataset categories"),
                    url=request.url_for("dataset_categories:list"),
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("New dataset category"),
                    icon=settings.icons.new_item,
                ),
            ],
        },
    )


@csrf_protect
@requires_auth
async def get_update_form(request: Request):
    """Return a form suitable for updating an existing dataset category."""
    session_maker = request.state.settings.get_db_session_maker()
    try:
        resource_id = identifiers.DatasetCategoryId(
            uuid.UUID(request.path_params["dataset_category_id"])
        )
    except (KeyError, ValueError) as err:
        raise HTTPException(status_code=404, detail=str(err))
    async with session_maker() as session:
        try:
            resource = await category_queries.get_dataset_category(session, resource_id)
        except errors.SeisLabDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if resource is None:
            raise HTTPException(
                status_code=404,
                detail=_(f"Dataset category {resource_id!r} not found."),
            )
    update_form = await category_forms.DatasetCategoryUpdateForm.from_formdata(
        request=request,
        data=resource.model_dump(exclude_none=True),
    )
    update_form.request_id.data = uuid.uuid4()
    template_processor: Jinja2Templates = request.state.templates
    settings: config.SeisLabDataSettings = request.state.settings
    return template_processor.TemplateResponse(
        request,
        "datasetcategories/update-form-page.html",
        context={
            "item": category_schemas.DatasetCategoryReadListItem.from_db_instance(
                resource
            ),
            "form": update_form,
            "breadcrumbs": [
                webui_schemas.BreadcrumbItem(
                    name=_("Home"),
                    url=request.url_for("home"),
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("Dataset categories"),
                    url=request.url_for("dataset_categories:list"),
                ),
                webui_schemas.BreadcrumbItem(
                    name=_("edit"),
                    icon=settings.icons.edit_item,
                ),
            ],
        },
    )


async def stream_to_list_page(request: Request):
    topic_names = [constants.NEW_TOPIC_DATASET_CATEGORIES]
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
            resource_type=constants.ResourceType.CATEGORY,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_list_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer(), status_code=200)


@requires_auth
async def stream_to_new_page(request: Request):
    """Stream relevant updates for the new dataset category page."""
    try:
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid request id") from err

    # TODO: should we update the form fields with handlers too?
    topic_names = [constants.NEW_TOPIC_DATASET_CATEGORIES]
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
            resource_type=constants.ResourceType.CATEGORY,
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
    """Stream relevant updates for the update dataset category page."""
    try:
        resource_id = request.path_params["dataset_category_id"]
    except (KeyError, ValueError) as err:
        raise HTTPException(status_code=400, detail="Invalid resource id") from err

    try:
        request_id = identifiers.RequestId(uuid.UUID(request.path_params["request_id"]))
    except (KeyError, ValueError) as err:
        raise HTTPException(status_code=400, detail="Invalid request id") from err

    # TODO: should we update the form fields with handlers too?
    topic_names = [constants.NEW_TOPIC_DATASET_CATEGORIES]
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
            target_page=constants.PageType.RESOURCE_UPDATE,
            resource_type=constants.ResourceType.CATEGORY,
            resource_id=resource_id,
        ),
        {
            "resource_modified": common_handlers.handle_resource_modification_edit_page,
        },
    )

    async def event_streamer():
        async for sse_event in subscription:
            yield sse_event

    return DatastarResponse(event_streamer())


class DatasetCategoryCollectionEndpoint(HTTPEndpoint):
    async def get(self, request: Request):
        settings: config.SeisLabDataSettings = request.state.settings
        user = request.user if request.user.is_authenticated else None
        current_page = get_page_from_request_params(request)
        current_language = request.state.language
        list_filters = filters.DatasetCategoryListFilters.from_params(
            request.query_params, current_language
        )
        async with settings.get_db_session_maker()() as session:
            (
                items,
                num_total,
            ) = await category_queries.list_dataset_categories(
                session,
                page=current_page,
                page_size=settings.pagination_page_size,
                include_total=True,
                **list_filters.as_kwargs(),
            )
            num_unfiltered_total = (
                await category_queries.list_dataset_categories(
                    session, include_total=True
                )
            )[1]
        template_processor = request.state.templates
        pagination_info = get_pagination_info(
            current_page=current_page,
            page_size=settings.pagination_page_size,
            total_filtered_items=num_total,
            total_unfiltered_items=num_unfiltered_total,
            collection_url=str(request.url_for("dataset_categories:list")),
        )

        serialized_items = [
            category_schemas.DatasetCategoryReadListItem.from_db_instance(i)
            for i in items
        ]
        return template_processor.TemplateResponse(
            request,
            "datasetcategories/list.html",
            context={
                "items": serialized_items,
                "pagination": pagination_info,
                "breadcrumbs": [
                    webui_schemas.BreadcrumbItem(
                        name=_("Home"),
                        url=request.url_for("home"),
                        icon=settings.icons.home,
                    ),
                    webui_schemas.BreadcrumbItem(
                        name=_("Dataset categories"),
                        icon=settings.icons.dataset_category,
                    ),
                ],
                "permissions": {
                    "can_create": category_permissions.can_create_dataset_category(
                        user
                    ),
                    "can_update": category_permissions.can_update_dataset_category(
                        user
                    ),
                    "can_delete": category_permissions.can_delete_dataset_category(
                        user
                    ),
                }
                if user is not None
                else {"can_create": False, "can_update": False, "can_delete": False},
                "search_initial_value": list_filters.get_text_search_filter(
                    current_language
                ),
            },
        )

    @csrf_protect
    @requires_auth
    async def post(self, request: Request):
        """Create a new dataset category."""
        template_processor: Jinja2Templates = request.state.templates
        user = request.user
        form_instance = (
            await category_forms.DatasetCategoryCreateForm.get_validated_form_instance(
                request
            )
        )
        if form_instance.has_validation_errors():
            logger.debug("form did not validate")

            async def validation_event_streamer():
                yield ServerSentEventGenerator.patch_signals({"submitting": False})
                template = template_processor.get_template(
                    "datasetcategories/create-form.html"
                )
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
            return DatastarResponse(validation_event_streamer(), status_code=200)

        to_create = category_schemas.DatasetCategoryCreate(
            id=identifiers.DatasetCategoryId(uuid.uuid4()),
            name=form_instance.name.data,
        )
        request_id = identifiers.RequestId(uuid.UUID(form_instance.request_id.data))
        category_tasks.create_dataset_category.send(
            raw_request_id=str(request_id),
            raw_to_create=to_create.model_dump_json(),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)


class DatasetCategoryDetailEndpoint(HTTPEndpoint):
    """Manage a single dataset category."""

    @csrf_protect
    @requires_auth
    async def put(self, request: Request):
        """Update an existing dataset category."""
        template_processor: Jinja2Templates = request.state.templates
        user = request.user
        session_maker = request.state.settings.get_db_session_maker()
        try:
            resource_id = identifiers.DatasetCategoryId(
                uuid.UUID(request.path_params["dataset_category_id"])
            )
        except (KeyError, ValueError) as err:
            return HTTPException(status_code=404, detail=str(err))

        async with session_maker() as session:
            if (
                resource := await category_queries.get_dataset_category(
                    session, resource_id
                )
            ) is None:
                raise HTTPException(
                    404,
                    f"Dataset category {resource_id!r} not found.",
                )
        form_instance = (
            await category_forms.DatasetCategoryUpdateForm.get_validated_form_instance(
                request, disregard_id=resource_id
            )
        )

        if form_instance.has_validation_errors():
            logger.debug("form did not validate")
            logger.debug(f"{form_instance.errors=}")

            async def event_streamer():
                yield ServerSentEventGenerator.patch_signals({"updating": False})
                template = template_processor.get_template(
                    "datasetcategories/update-form.html"
                )
                rendered = template.render(
                    request=request,
                    item=category_schemas.DatasetCategoryReadListItem.from_db_instance(
                        resource
                    ),
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

        category_tasks.update_dataset_category.send(
            raw_request_id=str(form_instance.request_id.data),
            raw_resource_id=str(resource_id),
            raw_to_update=category_schemas.DatasetCategoryUpdate(
                name=form_instance.name.data,
            ).model_dump_json(),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)

    @csrf_protect
    @requires_auth
    async def delete(self, request: Request):
        """Delete a dataset category."""
        request_id = identifiers.RequestId(uuid.uuid4())
        user = request.user
        session_maker = request.state.settings.get_db_session_maker()
        try:
            resource_id = identifiers.DatasetCategoryId(
                uuid.UUID(request.path_params["dataset_category_id"])
            )
        except (KeyError, ValueError) as err:
            return HTTPException(status_code=404, detail=str(err))

        async with session_maker() as session:
            resource = await category_queries.get_dataset_category(
                session,
                resource_id,
            )
            if resource is None:
                raise HTTPException(
                    status_code=404,
                    detail=_(f"Dataset category {resource_id!r} not found."),
                )
        category_tasks.delete_dataset_category.send(
            raw_request_id=str(request_id),
            raw_resource_id=str(resource_id),
            raw_initiator=json.dumps(dataclasses.asdict(user)),
        )  # noqa
        return Response(status_code=200)


routes = [
    Route("/", DatasetCategoryCollectionEndpoint, name="list"),
    Route("/stream", stream_to_list_page, name="list_stream"),
    Route("/search", get_list_component, name="get_list_component"),
    Route(
        "/new",
        get_creation_form,
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
        "/{dataset_category_id}/update",
        get_update_form,
        methods=["GET"],
        name="get_update_form",
    ),
    Route(
        "/{dataset_category_id}/update/stream/{request_id}",
        stream_to_update_page,
        methods=["GET"],
        name="update_stream",
    ),
    Route(
        "/{dataset_category_id}",
        DatasetCategoryDetailEndpoint,
        name="detail",
    ),
]
