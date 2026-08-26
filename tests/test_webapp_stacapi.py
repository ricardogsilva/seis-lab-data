import pytest
import pytest_asyncio
from starlette.testclient import TestClient

from seis_lab_data import constants
from seis_lab_data.db import models
from seis_lab_data.db.commands import (
    projects as project_commands,
    surveymissions as mission_commands,
    surveyrelatedrecords as record_commands,
)
from seis_lab_data.schemas import identifiers
from seis_lab_data.webapp.stacapi import conformance

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def published_sample_data(
    db_session_maker,
    sample_survey_related_records,
    sample_survey_missions,
    sample_projects,
):
    async with db_session_maker() as session:
        for project in sample_projects:
            await project_commands.set_project_status(
                session,
                identifiers.ProjectId(project.id),
                constants.ProjectStatus.PUBLISHED,
            )
        for mission in sample_survey_missions:
            await mission_commands.set_survey_mission_status(
                session,
                identifiers.SurveyMissionId(mission.id),
                constants.SurveyMissionStatus.PUBLISHED,
            )
        for record in sample_survey_related_records:
            await record_commands.set_survey_related_record_status(
                session,
                identifiers.SurveyRelatedRecordId(record.id),
                constants.SurveyRelatedRecordStatus.PUBLISHED,
            )
    yield {
        "projects": sample_projects,
        "missions": sample_survey_missions,
        "records": sample_survey_related_records,
    }


@pytest_asyncio.fixture
async def draft_project(db_session_maker, admin_user):
    from seis_lab_data.schemas import (
        common as common_schemas,
        projects as project_schemas,
    )
    import uuid

    to_create = project_schemas.ProjectCreate(
        id=identifiers.ProjectId(uuid.uuid4()),
        owner_id=identifiers.UserId(admin_user.id),
        name=common_schemas.LocalizableDraftName(en="a draft project"),
        description=common_schemas.LocalizableDraftDescription(en="draft"),
        root_path="projects/a-draft-project",
    )
    async with db_session_maker() as session:
        project = await project_commands.create_project(session, to_create)
    yield project


@pytest_asyncio.fixture
async def sample_asset(db_session_maker, published_sample_data):
    record = published_sample_data["records"][0]
    asset = models.RecordAsset(
        name={"en": "a data file"},
        description={"en": "a description"},
        survey_related_record_id=record.id,
        relative_path="some/file.tif",
        media_type="image/tiff",
        asset_type=[constants.AssetType.DATA],
    )
    async with db_session_maker() as session:
        session.add(asset)
        await session.commit()
        await session.refresh(asset)
    yield asset, record


def test_landing_page(stac_api_client: TestClient):
    response = stac_api_client.get("/stac/")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "Catalog"
    assert body["conformsTo"] == conformance.CONFORMS_TO
    rels = {link["rel"] for link in body["links"]}
    assert {"self", "root", "conformance", "search", "data", "children"} <= rels


def test_conformance(stac_api_client: TestClient):
    response = stac_api_client.get("/stac/conformance")
    assert response.status_code == 200
    assert response.json()["conformsTo"] == conformance.CONFORMS_TO


def test_children_lists_only_published_projects(
    stac_api_client: TestClient, published_sample_data, draft_project
):
    response = stac_api_client.get("/stac/children")
    assert response.status_code == 200
    body = response.json()
    ids = {child["id"] for child in body["children"]}
    assert str(published_sample_data["projects"][0].id) in ids
    assert str(draft_project.id) not in ids


def test_project_catalog_published(stac_api_client: TestClient, published_sample_data):
    project = published_sample_data["projects"][0]
    response = stac_api_client.get(f"/stac/catalogs/{project.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(project.id)
    rels = {link["rel"] for link in body["links"]}
    assert {"self", "root", "parent", "children"} <= rels


def test_project_catalog_draft_is_404(stac_api_client: TestClient, draft_project):
    response = stac_api_client.get(f"/stac/catalogs/{draft_project.id}")
    assert response.status_code == 404


def test_project_catalog_missing_is_404(stac_api_client: TestClient, db):
    import uuid

    response = stac_api_client.get(f"/stac/catalogs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_project_children_lists_missions(
    stac_api_client: TestClient, published_sample_data
):
    project = published_sample_data["projects"][0]
    response = stac_api_client.get(f"/stac/catalogs/{project.id}/children")
    assert response.status_code == 200
    body = response.json()
    ids = {child["id"] for child in body["children"]}
    expected = {
        str(m.id)
        for m in published_sample_data["missions"]
        if m.project_id == project.id
    }
    assert expected <= ids


def test_collections_lists_published_missions(
    stac_api_client: TestClient, published_sample_data
):
    response = stac_api_client.get("/stac/collections")
    assert response.status_code == 200
    body = response.json()
    ids = {c["id"] for c in body["collections"]}
    assert str(published_sample_data["missions"][0].id) in ids


def test_collection_detail_published(
    stac_api_client: TestClient, published_sample_data
):
    mission = published_sample_data["missions"][0]
    response = stac_api_client.get(f"/stac/collections/{mission.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(mission.id)
    assert body["type"] == "Collection"
    assert body["extent"]["spatial"]["bbox"]


def test_collection_items_lists_published_records(
    stac_api_client: TestClient, published_sample_data
):
    mission = published_sample_data["missions"][0]
    response = stac_api_client.get(f"/stac/collections/{mission.id}/items")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    expected = {
        str(r.id)
        for r in published_sample_data["records"]
        if r.survey_mission_id == mission.id
    }
    ids = {f["id"] for f in body["features"]}
    assert expected <= ids


def test_item_detail_published(stac_api_client: TestClient, published_sample_data):
    record = published_sample_data["records"][0]
    mission_id = record.survey_mission_id
    response = stac_api_client.get(f"/stac/collections/{mission_id}/items/{record.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(record.id)
    assert body["type"] == "Feature"
    assert body["collection"] == str(mission_id)


def test_item_detail_wrong_mission_is_404(
    stac_api_client: TestClient, published_sample_data
):
    record = published_sample_data["records"][0]
    other_mission = next(
        m for m in published_sample_data["missions"] if m.id != record.survey_mission_id
    )
    response = stac_api_client.get(
        f"/stac/collections/{other_mission.id}/items/{record.id}"
    )
    assert response.status_code == 404


def test_search_get_by_ids(stac_api_client: TestClient, published_sample_data):
    record = published_sample_data["records"][0]
    response = stac_api_client.get("/stac/search", params={"ids": str(record.id)})
    assert response.status_code == 200
    body = response.json()
    assert {f["id"] for f in body["features"]} == {str(record.id)}


def test_search_get_by_collections(stac_api_client: TestClient, published_sample_data):
    mission = published_sample_data["missions"][0]
    response = stac_api_client.get(
        "/stac/search", params={"collections": str(mission.id), "limit": 100}
    )
    assert response.status_code == 200
    body = response.json()
    expected = {
        str(r.id)
        for r in published_sample_data["records"]
        if r.survey_mission_id == mission.id
    }
    ids = {f["id"] for f in body["features"]}
    assert expected <= ids


def test_search_post_by_ids(stac_api_client: TestClient, published_sample_data):
    record = published_sample_data["records"][0]
    response = stac_api_client.post("/stac/search", json={"ids": [str(record.id)]})
    assert response.status_code == 200
    body = response.json()
    assert {f["id"] for f in body["features"]} == {str(record.id)}


def test_asset_href_matches_jinja_filter(
    stac_api_client: TestClient, settings, sample_asset
):
    asset, record = sample_asset
    mission_id = record.survey_mission_id
    response = stac_api_client.get(f"/stac/collections/{mission_id}/items/{record.id}")
    assert response.status_code == 200
    body = response.json()
    stac_href = body["assets"][str(asset.id)]["href"]
    # jinjafilters.get_url_for_asset expects RecordAssetReadDetailEmbedded /
    # SurveyRelatedRecordReadDetail-shaped objects with survey_mission.project
    # already available on the record - build the same join manually here to
    # cross-check byte-for-byte, since constructing those schema objects would
    # require a full read-schema round trip.
    expected_href = "/".join(
        (
            settings.public_url,
            record.survey_mission.project.root_path,
            record.survey_mission.relative_path,
            asset.relative_path,
        )
    )
    assert stac_href == expected_href
