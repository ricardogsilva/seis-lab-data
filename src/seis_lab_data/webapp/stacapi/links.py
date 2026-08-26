from fastapi import Request
from stac_pydantic.links import (
    Link,
    Relations,
)

# the STAC sub-app is mounted with `Mount(..., name="stac")` in webapp/app.py;
# Starlette's named-Mount `url_for` resolution requires that prefix on route
# names looked up from inside the sub-app (see Mount.url_path_for)
MOUNT_NAME = "stac"


def build_link(
    request: Request,
    rel: str,
    route_name: str,
    media_type: str = "application/json",
    **path_params,
) -> Link:
    return Link(
        rel=rel,
        href=str(request.url_for(f"{MOUNT_NAME}:{route_name}", **path_params)),
        type=media_type,
    )


def self_link(
    request: Request,
    route_name: str,
    media_type: str = "application/json",
    **path_params,
) -> Link:
    return build_link(
        request, Relations.self.value, route_name, media_type=media_type, **path_params
    )


def root_link(request: Request) -> Link:
    return build_link(request, Relations.root.value, "landing-page")


def parent_link(
    request: Request,
    route_name: str | None = None,
    media_type: str = "application/json",
    **path_params,
) -> Link:
    if route_name is None:
        return build_link(request, Relations.parent.value, "landing-page")
    return build_link(
        request,
        Relations.parent.value,
        route_name,
        media_type=media_type,
        **path_params,
    )


def children_link(request: Request, route_name: str, **path_params) -> Link:
    return build_link(request, Relations.children.value, route_name, **path_params)


def items_link(request: Request, route_name: str, **path_params) -> Link:
    return build_link(
        request,
        Relations.items.value,
        route_name,
        media_type="application/geo+json",
        **path_params,
    )


def collection_link(request: Request, mission_id) -> Link:
    return build_link(
        request, Relations.collection.value, "collection", mission_id=mission_id
    )
