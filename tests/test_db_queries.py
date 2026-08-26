import uuid

import pytest

from seis_lab_data import constants
from seis_lab_data.db import models
from seis_lab_data.db.queries import (
    projects as project_queries,
    recordassets as asset_queries,
    surveymissions as mission_queries,
    surveyrelatedrecords as record_queries,
)
from seis_lab_data.schemas import identifiers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_projects(sample_projects, db_session_maker):
    async with db_session_maker() as session:
        projects, total = await project_queries.list_projects(
            session, include_total=True
        )
        assert total == len(sample_projects)


@pytest.mark.parametrize(
    "project_id_filter, expected_total",
    [
        pytest.param(None, 6),
        pytest.param(
            identifiers.ProjectId(uuid.UUID("74f07051-1aa9-4c08-bc27-3ecf101ab5b3")), 3
        ),
    ],
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_survey_missions(
    sample_survey_missions, db_session_maker, project_id_filter, expected_total
):
    async with db_session_maker() as session:
        survey_missions, total = await mission_queries.list_survey_missions(
            session, project_id=project_id_filter, include_total=True
        )
        assert total == expected_total


@pytest.mark.parametrize(
    "survey_mission_ids_filter, expected_total",
    [
        pytest.param(None, 2),
        pytest.param(
            [
                identifiers.SurveyMissionId(
                    uuid.UUID("cfe10cd8-5a5e-40e4-807b-7064f94a2edf")
                )
            ],
            1,
        ),
    ],
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_survey_related_records(
    sample_survey_related_records,
    db_session_maker,
    survey_mission_ids_filter,
    expected_total,
):
    async with db_session_maker() as session:
        survey_records, total = await record_queries.list_survey_related_records(
            session, survey_mission_ids=survey_mission_ids_filter, include_total=True
        )
        assert total == expected_total


@pytest.mark.parametrize(
    "project_id_filter, expected_total",
    [
        pytest.param(None, 2),
        pytest.param(
            identifiers.ProjectId(uuid.UUID("74f07051-1aa9-4c08-bc27-3ecf101ab5b3")), 2
        ),
        pytest.param(
            identifiers.ProjectId(uuid.UUID("8f931331-15c3-4899-846c-38470f6bcb5a")), 0
        ),
    ],
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_survey_related_records_project_id_filter(
    sample_survey_related_records,
    db_session_maker,
    project_id_filter,
    expected_total,
):
    async with db_session_maker() as session:
        survey_records, total = await record_queries.list_survey_related_records(
            session, project_id=project_id_filter, include_total=True
        )
        assert total == expected_total


@pytest.mark.integration
@pytest.mark.asyncio
async def test_media_type_queries_ignore_derived_assets(
    sample_survey_related_records, db_session_maker
):
    # a preview's media type is an implementation detail: it must neither show
    # up in the media type datalist nor make its record match a search
    first_record, _second_record = sample_survey_related_records
    async with db_session_maker() as session:
        session.add(
            models.RecordAsset(
                id=uuid.uuid4(),
                name={"en": "A derived asset"},
                description={"en": ""},
                survey_related_record_id=first_record.id,
                media_type="image/webp",
                asset_type=[constants.AssetType.PREVIEW],
            )
        )
        await session.commit()

        media_types = await asset_queries.list_media_types(session)
        assert "image/webp" not in media_types
        # the media type the sample assets declare
        assert "application/octet-stream" in media_types
        assert await asset_queries.count_media_types(session) == len(media_types)

        _records, total = await record_queries.list_survey_related_records(
            session, asset_media_type_filter="image/webp", include_total=True
        )
        assert total == 0
