import datetime as dt
import json

import shapely
import stac_pydantic
import stac_pydantic.api as stac_api
import stac_pydantic.collection as stac_collection
import stac_pydantic.shared as stac_shared
from fastapi import Request
from geoalchemy2 import WKBElement

from ... import config
from ...db import models
from . import (
    conformance,
    links as link_helpers,
)


# extent isn't always computed yet (e.g. before a record/mission is validated),
# and STAC Collection.extent.spatial.bbox is a required field, so this is the
# conventional fallback for an as-yet-unknown spatial extent
_WORLD_BBOX = (-180.0, -90.0, 180.0, 90.0)


def bbox_and_geometry(
    wkbelement: WKBElement | None,
) -> tuple[tuple[float, ...] | None, dict | None]:
    if wkbelement is None:
        return None, None
    geom = shapely.from_wkb(bytes(wkbelement.data))
    return geom.bounds, json.loads(shapely.to_geojson(geom))


def _to_utc_datetime(value: dt.date | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)


def catalog_from_project(
    project: models.Project, request: Request
) -> stac_pydantic.Catalog:
    return stac_pydantic.Catalog(
        type="Catalog",
        id=str(project.id),
        title=project.name.get("en"),
        # STAC requires a non-empty description; the app allows an empty one
        description=project.description.get("en") or project.name.get("en"),
        stac_version=conformance.STAC_VERSION,
        links=[
            link_helpers.self_link(request, "project-catalog", project_id=project.id),
            link_helpers.root_link(request),
            link_helpers.parent_link(request),
            link_helpers.children_link(
                request, "project-children", project_id=project.id
            ),
        ],
    )


def collection_from_survey_mission(
    mission: models.SurveyMission, request: Request
) -> stac_api.Collection:
    bbox, _ = bbox_and_geometry(mission.bbox_4326)
    extent = stac_collection.Extent(
        spatial=stac_collection.SpatialExtent(bbox=[bbox or _WORLD_BBOX]),
        temporal=stac_collection.TimeInterval(
            interval=[
                [
                    _to_utc_datetime(mission.temporal_extent_begin),
                    _to_utc_datetime(mission.temporal_extent_end),
                ]
            ]
        ),
    )
    return stac_api.Collection(
        type="Collection",
        id=str(mission.id),
        title=mission.name.get("en"),
        # STAC requires a non-empty description; the app allows an empty one
        description=mission.description.get("en") or mission.name.get("en"),
        stac_version=conformance.STAC_VERSION,
        license="other",
        extent=extent,
        links=[
            link_helpers.self_link(request, "collection", mission_id=mission.id),
            link_helpers.root_link(request),
            link_helpers.parent_link(
                request, "project-catalog", project_id=mission.project_id
            ),
            link_helpers.items_link(request, "collection-items", mission_id=mission.id),
        ],
    )


def asset_dict_from_records(
    assets: list[models.RecordAsset],
    project: models.Project,
    mission: models.SurveyMission,
    request: Request,
    settings: config.SeisLabDataSettings,
) -> dict[str, stac_shared.Asset]:
    result = {}
    for asset in assets:
        if not asset.relative_path:
            # no file in the archive to point a STAC href at
            continue
        href = "/".join(
            (
                settings.public_url,
                project.root_path,
                mission.relative_path,
                asset.relative_path,
            )
        )
        result[str(asset.id)] = stac_shared.Asset(
            href=href,
            type=asset.media_type,
            title=asset.name.get("en") or None,
            description=asset.description.get("en") or None,
            roles=[asset_type.value for asset_type in asset.asset_type],
        )
    return result


def item_from_survey_related_record(
    record: models.SurveyRelatedRecord,
    mission: models.SurveyMission,
    project: models.Project,
    request: Request,
    settings: config.SeisLabDataSettings,
) -> stac_api.Item:
    bbox, geometry = bbox_and_geometry(record.bbox_4326)
    start_datetime = _to_utc_datetime(record.temporal_extent_begin)
    end_datetime = _to_utc_datetime(record.temporal_extent_end)
    # STAC requires `datetime` whenever start/end aren't both known; the
    # record's own temporal extent may not be computed yet. created_at is
    # stored without a timezone (implicitly UTC), so it needs one attached.
    instant = (
        None
        if (start_datetime or end_datetime)
        else record.created_at.replace(tzinfo=dt.timezone.utc)
    )
    properties = stac_pydantic.ItemProperties(
        datetime=instant,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        **{
            "sld:status": record.status.value,
            "sld:is_valid": record.is_valid,
            "sld:validation_result": record.validation_result,
            "sld:owner_id": record.owner_id,
            "sld:dataset_category_id": (
                str(record.dataset_category_id) if record.dataset_category_id else None
            ),
            "sld:workflow_stage_id": (
                str(record.workflow_stage_id) if record.workflow_stage_id else None
            ),
        },
    )
    return stac_api.Item(
        type="Feature",
        id=str(record.id),
        stac_version=conformance.STAC_VERSION,
        collection=str(mission.id),
        bbox=bbox,
        geometry=geometry,
        properties=properties,
        assets=asset_dict_from_records(
            record.assets, project, mission, request, settings
        ),
        links=[
            link_helpers.self_link(
                request,
                "item",
                media_type="application/geo+json",
                mission_id=mission.id,
                item_id=record.id,
            ),
            link_helpers.root_link(request),
            link_helpers.parent_link(request, "collection", mission_id=mission.id),
            link_helpers.collection_link(request, mission.id),
        ],
    )
