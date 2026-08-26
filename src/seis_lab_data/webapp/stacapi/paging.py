import pydantic
import shapely
import shapely.geometry
import stac_pydantic.shared as stac_shared
from fastapi import (
    HTTPException,
    Request,
)
from pydantic import TypeAdapter
from stac_pydantic.api.search import Intersection
from stac_pydantic.links import (
    Link,
    Relations,
)

from ...schemas import filters as filter_schemas
from ..routes.common import get_page_count
from .links import MOUNT_NAME


def parse_bbox_param(raw: str | None) -> shapely.Geometry | None:
    if raw is None:
        return None
    try:
        values = [float(v) for v in raw.split(",")]
    except ValueError as err:
        raise HTTPException(400, f"Invalid bbox parameter: {raw!r}") from err
    if len(values) == 6:
        # a 3D bbox is valid per spec; this app's geometries are 2D-only,
        # so the vertical axis (minz/maxz) is simply ignored
        minx, miny, _minz, maxx, maxy, _maxz = values
    elif len(values) == 4:
        minx, miny, maxx, maxy = values
    else:
        raise HTTPException(
            400,
            f"bbox must have 4 or 6 values (minx,miny[,minz],maxx,maxy[,maxz]): {raw!r}",
        )
    if miny > maxy:
        raise HTTPException(400, f"bbox has miny > maxy: {raw!r}")
    return shapely.box(minx, miny, maxx, maxy)


_intersection_adapter = TypeAdapter(Intersection)


def parse_intersects_param(raw: str | None) -> shapely.Geometry | None:
    if raw is None:
        return None
    try:
        geometry = _intersection_adapter.validate_json(raw)
    except pydantic.ValidationError as err:
        raise HTTPException(400, f"Invalid intersects geometry: {raw!r}") from err
    return shapely.geometry.shape(geometry.model_dump(mode="json"))


def parse_datetime_param(
    raw: str | None,
) -> filter_schemas.TemporalExtentFilterValue | None:
    if raw is None:
        return None
    try:
        stac_shared.validate_datetime(raw)
        dates = stac_shared.str_to_datetimes(raw)
    except (ValueError, pydantic.ValidationError) as err:
        raise HTTPException(400, f"Invalid datetime parameter: {raw!r}") from err
    if len(dates) == 1:
        dates = [dates[0], dates[0]]
    begin, end = dates
    if begin is None and end is None:
        # a range that's open on both ends filters nothing, which isn't a
        # meaningful `datetime` value per the Item Search conformance suite
        raise HTTPException(400, f"Invalid datetime parameter: {raw!r}")
    return filter_schemas.TemporalExtentFilterValue(
        begin=begin.date() if begin else None, end=end.date() if end else None
    )


def parse_limit_param(raw: int | None, default: int, max_: int = 10_000) -> int:
    if raw is None:
        return default
    if raw < 1:
        raise HTTPException(400, f"limit must be a positive integer: {raw!r}")
    return min(raw, max_)


def build_next_link(
    request: Request,
    route_name: str,
    current_page: int,
    page_size: int,
    total_items: int,
    path_params: dict | None = None,
    extra_params: dict | None = None,
) -> Link | None:
    total_pages = get_page_count(total_items, page_size)
    if current_page >= total_pages:
        return None
    query = {**(extra_params or {}), "page": current_page + 1, "limit": page_size}
    href = str(
        request.url_for(
            f"{MOUNT_NAME}:{route_name}", **(path_params or {})
        ).include_query_params(**query)
    )
    return Link(rel=Relations.next.value, href=href, type="application/json")


def build_prev_link(
    request: Request,
    route_name: str,
    current_page: int,
    page_size: int,
    path_params: dict | None = None,
    extra_params: dict | None = None,
) -> Link | None:
    if current_page <= 1:
        return None
    query = {**(extra_params or {}), "page": current_page - 1, "limit": page_size}
    href = str(
        request.url_for(
            f"{MOUNT_NAME}:{route_name}", **(path_params or {})
        ).include_query_params(**query)
    )
    return Link(rel=Relations.prev.value, href=href, type="application/json")
