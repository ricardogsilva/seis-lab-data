import uuid

import pytest

from seis_lab_data import constants
from seis_lab_data.db.commands import (
    datasetcategories as category_commands,
    projects as project_commands,
    surveymissions as mission_commands,
    surveyrelatedrecords as record_commands,
    workflowstages as stage_commands,
)
from seis_lab_data.db.queries import (
    projects as project_queries,
    surveymissions as mission_queries,
    surveyrelatedrecords as record_queries,
    datasetcategories as category_queries,
    workflowstages as stage_queries,
)
from seis_lab_data.schemas import (
    common as common_schemas,
    datasetcategories as category_schemas,
    identifiers,
    projects as project_schemas,
    surveymissions as mission_schemas,
    surveyrelatedrecords as record_schemas,
    workflowstages as stage_schemas,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_dataset_category(db, db_session_maker):
    to_create = category_schemas.DatasetCategoryCreate(
        id=identifiers.DatasetCategoryId(
            uuid.UUID("303cad6d-2e0e-447e-85e1-c284c1c882a7")
        ),
        name=common_schemas.LocalizableDraftName(en="A fake category"),
    )
    async with db_session_maker() as session:
        created = await category_commands.create_dataset_category(session, to_create)
        assert created.id == to_create.id
        assert created.name["en"] == to_create.name.en


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_dataset_category(db, db_session_maker):
    to_create = category_schemas.DatasetCategoryCreate(
        id=identifiers.DatasetCategoryId(
            uuid.UUID("26b06713-dce1-4304-bf50-fec5c3f5efe6")
        ),
        name=common_schemas.LocalizableDraftName(en="A fake category"),
    )
    async with db_session_maker() as session:
        await category_commands.create_dataset_category(session, to_create)
        assert (
            await category_queries.get_dataset_category(session, to_create.id)
            is not None
        )
        await category_commands.delete_dataset_category(session, to_create.id)
        assert (
            await category_queries.get_dataset_category(session, to_create.id) is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_workflow_stage(db, db_session_maker):
    to_create = stage_schemas.WorkflowStageCreate(
        id=identifiers.WorkflowStageId(
            uuid.UUID("24d10a9f-8b30-4866-aa1b-5fe34a2f4ecf")
        ),
        name=common_schemas.LocalizableDraftName(en="A fake workflow stage"),
    )
    async with db_session_maker() as session:
        created = await stage_commands.create_workflow_stage(session, to_create)
        assert created.id == to_create.id
        assert created.name["en"] == to_create.name.en


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_workflow_stage(db, db_session_maker):
    to_create = stage_schemas.WorkflowStageCreate(
        id=identifiers.WorkflowStageId(
            uuid.UUID("adaf887f-27a9-40da-afe4-785a169c3edd")
        ),
        name=common_schemas.LocalizableDraftName(en="A fake workflow stage"),
    )
    async with db_session_maker() as session:
        await stage_commands.create_workflow_stage(session, to_create)
        assert await stage_queries.get_workflow_stage(session, to_create.id) is not None
        await stage_commands.delete_workflow_stage(session, to_create.id)
        assert await stage_queries.get_workflow_stage(session, to_create.id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_project(db, db_session_maker, admin_user):
    to_create = project_schemas.ProjectCreate(
        id=identifiers.ProjectId(uuid.UUID("5fe24752-5919-4a05-be46-aed53a6936db")),
        owner_id=admin_user.id,
        name=common_schemas.LocalizableDraftName(
            en="A fake project", pt="Um projeto falso"
        ),
        description=common_schemas.LocalizableDraftDescription(
            en="A description for fake project",
            pt="Uma descrição para o projeto falso",
        ),
        root_path="/fake-path/to/fake-project/",
    )
    async with db_session_maker() as session:
        created = await project_commands.create_project(session, to_create)
        assert created.id == to_create.id
        assert created.owner_id == to_create.owner_id
        assert created.id == to_create.id
        assert created.name["en"] == to_create.name.en
        assert created.name["pt"] == to_create.name.pt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_project(db, db_session_maker, admin_user):
    to_create = project_schemas.ProjectCreate(
        id=identifiers.ProjectId(uuid.UUID("0637d5d9-6381-4ba8-b9ec-89750baa93a4")),
        owner_id=admin_user.id,
        name=common_schemas.LocalizableDraftName(
            en="A fake project", pt="Um projeto falso"
        ),
        description=common_schemas.LocalizableDraftDescription(
            en="A description for fake project",
            pt="Uma descrição para o projeto falso",
        ),
        root_path="/fake-path/to/fake-project/",
    )
    async with db_session_maker() as session:
        await project_commands.create_project(session, to_create)
        assert await project_queries.get_project(session, to_create.id) is not None
        await project_commands.delete_project(session, to_create.id)
        assert await project_queries.get_project(session, to_create.id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_survey_mission(db, db_session_maker, sample_projects, admin_user):
    to_create = mission_schemas.SurveyMissionCreate(
        id=identifiers.SurveyMissionId(
            uuid.UUID("1aad09c3-d606-445e-9216-d9620586c332")
        ),
        project_id=identifiers.ProjectId(sample_projects[0].id),
        owner_id=admin_user.id,
        name=common_schemas.LocalizableDraftName(
            en="A fake survey mission", pt="Uma missão falsa"
        ),
        description=common_schemas.LocalizableDraftDescription(
            en="A description for fake survey mission",
            pt="Uma descrição para a missão falsa",
        ),
        relative_path="fake-mission",
    )
    async with db_session_maker() as session:
        created = await mission_commands.create_survey_mission(session, to_create)
        assert created.id == to_create.id
        assert created.owner_id == to_create.owner_id
        assert created.id == to_create.id
        assert created.name["en"] == to_create.name.en
        assert created.name["pt"] == to_create.name.pt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_survey_mission(db, db_session_maker, sample_projects, admin_user):
    to_create = mission_schemas.SurveyMissionCreate(
        id=identifiers.SurveyMissionId(
            uuid.UUID("449a96e4-9b3b-41ad-a08b-75d31332b846")
        ),
        project_id=identifiers.ProjectId(sample_projects[0].id),
        owner_id=admin_user.id,
        name=common_schemas.LocalizableDraftName(
            en="A fake survey mission", pt="Uma missão falsa"
        ),
        description=common_schemas.LocalizableDraftDescription(
            en="A description for fake survey mission",
            pt="Uma descrição para a missão falsa",
        ),
        relative_path="fake-mission",
    )
    async with db_session_maker() as session:
        await mission_commands.create_survey_mission(session, to_create)
        assert (
            await mission_queries.get_survey_mission(session, to_create.id) is not None
        )
        await mission_commands.delete_survey_mission(session, to_create.id)
        assert await mission_queries.get_survey_mission(session, to_create.id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_survey_related_record(
    db,
    db_session_maker,
    sample_survey_missions,
    bootstrap_dataset_categories,
    bootstrap_workflow_stages,
    admin_user,
):
    dataset_category = [
        c for c in bootstrap_dataset_categories if c.name["en"] == "bathymetry"
    ][0]
    workflow_stage = [
        w for w in bootstrap_workflow_stages if w.name["en"] == "raw data"
    ][0]
    to_create = record_schemas.SurveyRelatedRecordCreate(
        id=identifiers.SurveyRelatedRecordId(
            uuid.UUID("cabe6a5f-d81c-496c-80cc-c3505b9121c2")
        ),
        survey_mission_id=identifiers.SurveyMissionId(sample_survey_missions[0].id),
        owner_id=admin_user.id,
        name=common_schemas.LocalizableDraftName(
            en="A fake survey-related record", pt="Um registo falso"
        ),
        description=common_schemas.LocalizableDraftDescription(
            en="A description for fake survey-related record",
            pt="Uma descrição para o registo falso",
        ),
        dataset_category_id=identifiers.DatasetCategoryId(dataset_category.id),
        workflow_stage_id=identifiers.WorkflowStageId(workflow_stage.id),
        relative_path="fake-record",
        assets=[
            record_schemas.RecordAssetCreate(
                id=identifiers.RecordAssetId(
                    uuid.UUID("3cf81de8-60f3-44df-89f4-6f674a7fb94f")
                ),
                name=common_schemas.LocalizableDraftName(
                    en="first asset",
                    pt="primeiro registo",
                ),
                description=common_schemas.LocalizableDraftDescription(
                    en="description for first asset",
                    pt="descrição para o primeiro recurso",
                ),
                relative_path="asset1",
            ),
            record_schemas.RecordAssetCreate(
                id=identifiers.RecordAssetId(
                    uuid.UUID("85ded7b6-a794-4746-b450-c3bdfb07e5c0")
                ),
                name=common_schemas.LocalizableDraftName(
                    en="second asset",
                    pt="segundo registo",
                ),
                description=common_schemas.LocalizableDraftDescription(
                    en="description for second asset",
                    pt="descrição para o segundo recurso",
                ),
                relative_path="asset2",
            ),
        ],
    )
    async with db_session_maker() as session:
        created = await record_commands.create_survey_related_record(session, to_create)
        assert created.id == to_create.id
        assert created.owner_id == to_create.owner_id
        assert created.id == to_create.id
        assert created.name["en"] == to_create.name.en
        assert created.name["pt"] == to_create.name.pt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_survey_related_record(
    db,
    db_session_maker,
    sample_survey_missions,
    bootstrap_dataset_categories,
    bootstrap_workflow_stages,
    admin_user,
):
    dataset_category = [
        c for c in bootstrap_dataset_categories if c.name["en"] == "bathymetry"
    ][0]
    workflow_stage = [
        w for w in bootstrap_workflow_stages if w.name["en"] == "raw data"
    ][0]
    to_create = record_schemas.SurveyRelatedRecordCreate(
        id=identifiers.SurveyRelatedRecordId(
            uuid.UUID("d0f6cb56-e942-4fd7-a0a9-083c3069d698")
        ),
        survey_mission_id=identifiers.SurveyMissionId(sample_survey_missions[0].id),
        owner_id=admin_user.id,
        name=common_schemas.LocalizableDraftName(
            en="A fake survey-related record", pt="Um registo falso"
        ),
        description=common_schemas.LocalizableDraftDescription(
            en="A description for fake survey-related record",
            pt="Uma descrição para o registo falso",
        ),
        dataset_category_id=identifiers.DatasetCategoryId(dataset_category.id),
        workflow_stage_id=identifiers.WorkflowStageId(workflow_stage.id),
        relative_path="fake-record",
    )
    async with db_session_maker() as session:
        await record_commands.create_survey_related_record(session, to_create)
        assert (
            await record_queries.get_survey_related_record(session, to_create.id)
            is not None
        )
        await record_commands.delete_survey_related_record(session, to_create.id)
        assert (
            await record_queries.get_survey_related_record(session, to_create.id)
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_update_filtered_records(
    db,
    db_session_maker,
    sample_survey_related_records,
    bootstrap_workflow_stages,
    admin_user,
):
    first_record, second_record = sample_survey_related_records
    new_stage = [
        w for w in bootstrap_workflow_stages if w.name["en"] == "quality control data"
    ][0]
    to_update = record_schemas.SurveyRelatedRecordBulkUpdate(
        workflow_stage_id=identifiers.WorkflowStageId(new_stage.id)
    )
    async with db_session_maker() as session:
        updated_count = await record_commands.bulk_update_filtered_records(
            session,
            to_update,
            identifiers.UserId(admin_user.id),
            en_name_filter="First",
        )
        assert updated_count == 1
        updated_first = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(first_record.id)
        )
        untouched_second = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        assert updated_first.workflow_stage_id == new_stage.id
        assert untouched_second.workflow_stage_id != new_stage.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_update_filtered_records_excludes_records(
    db,
    db_session_maker,
    sample_survey_related_records,
    bootstrap_workflow_stages,
    admin_user,
):
    first_record, second_record = sample_survey_related_records
    new_stage = [
        w for w in bootstrap_workflow_stages if w.name["en"] == "quality control data"
    ][0]
    to_update = record_schemas.SurveyRelatedRecordBulkUpdate(
        workflow_stage_id=identifiers.WorkflowStageId(new_stage.id)
    )
    async with db_session_maker() as session:
        updated_count = await record_commands.bulk_update_filtered_records(
            session,
            to_update,
            identifiers.UserId(admin_user.id),
            excluded_record_ids=[identifiers.SurveyRelatedRecordId(first_record.id)],
        )
        assert updated_count == 1
        untouched_first = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(first_record.id)
        )
        updated_second = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        assert untouched_first.workflow_stage_id != new_stage.id
        assert updated_second.workflow_stage_id == new_stage.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_update_manually_selected_records(
    db,
    db_session_maker,
    sample_survey_related_records,
    admin_user,
):
    first_record, second_record = sample_survey_related_records
    to_update = record_schemas.SurveyRelatedRecordBulkUpdate(
        description=common_schemas.LocalizableDraftDescription(
            en="Bulk-updated description"
        )
    )
    async with db_session_maker() as session:
        updated_count = await record_commands.bulk_update_manually_selected_records(
            session,
            to_update,
            [identifiers.SurveyRelatedRecordId(second_record.id)],
            identifiers.UserId(admin_user.id),
        )
        assert updated_count == 1
        updated_second = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        untouched_first = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(first_record.id)
        )
        assert updated_second.description["en"] == "Bulk-updated description"
        assert untouched_first.description["en"] != "Bulk-updated description"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_update_manually_selected_records_replaces_related_records(
    db,
    db_session_maker,
    sample_survey_related_records,
    admin_user,
):
    first_record, second_record = sample_survey_related_records

    add_relation = record_schemas.SurveyRelatedRecordBulkUpdate(
        related_records=[
            record_schemas.RelatedRecordCreate(
                related_record_id=identifiers.SurveyRelatedRecordId(first_record.id),
                relationship=common_schemas.LocalizableDraftRelationship(
                    en="duplicate of"
                ),
            )
        ]
    )
    async with db_session_maker() as session:
        await record_commands.bulk_update_manually_selected_records(
            session,
            add_relation,
            [identifiers.SurveyRelatedRecordId(second_record.id)],
            identifiers.UserId(admin_user.id),
        )
        related_to = await record_queries.list_survey_related_record_related_to_records(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        assert len(related_to) == 1
        relation, related_record = related_to[0]
        assert related_record.id == first_record.id
        assert relation["en"] == "duplicate of"

    clear_relations = record_schemas.SurveyRelatedRecordBulkUpdate(related_records=[])
    async with db_session_maker() as session:
        await record_commands.bulk_update_manually_selected_records(
            session,
            clear_relations,
            [identifiers.SurveyRelatedRecordId(second_record.id)],
            identifiers.UserId(admin_user.id),
        )
        related_to = await record_queries.list_survey_related_record_related_to_records(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        assert len(related_to) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_publish_valid_survey_related_records(
    db,
    db_session_maker,
    sample_survey_related_records,
    bootstrap_dataset_categories,
    bootstrap_workflow_stages,
    admin_user,
):
    # first_record and second_record belong to different survey missions
    first_record, second_record = sample_survey_related_records
    dataset_category = [
        c for c in bootstrap_dataset_categories if c.name["en"] == "bathymetry"
    ][0]
    workflow_stage = [
        w for w in bootstrap_workflow_stages if w.name["en"] == "raw data"
    ][0]
    async with db_session_maker() as session:
        # an invalid sibling record, on the same mission as first_record
        invalid_sibling = await record_commands.create_survey_related_record(
            session,
            record_schemas.SurveyRelatedRecordCreate(
                id=identifiers.SurveyRelatedRecordId(uuid.uuid4()),
                survey_mission_id=identifiers.SurveyMissionId(
                    first_record.survey_mission_id
                ),
                owner_id=admin_user.id,
                name=common_schemas.LocalizableDraftName(en="Invalid sibling record"),
                description=common_schemas.LocalizableDraftDescription(
                    en="An invalid sibling record"
                ),
                dataset_category_id=identifiers.DatasetCategoryId(dataset_category.id),
                workflow_stage_id=identifiers.WorkflowStageId(workflow_stage.id),
                relative_path="invalid-sibling",
            ),
        )
        fresh_first = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(first_record.id)
        )
        fresh_second = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        await record_commands.update_survey_related_record_validation_result(
            session, fresh_first, validation_result={"is_valid": True, "errors": None}
        )
        await record_commands.update_survey_related_record_validation_result(
            session, fresh_second, validation_result={"is_valid": True, "errors": None}
        )
        # invalid_sibling is left with its default (not valid) validation result

        published_count = (
            await record_commands.bulk_publish_valid_survey_related_records(
                session, identifiers.SurveyMissionId(first_record.survey_mission_id)
            )
        )
        assert published_count == 1

        published_first = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(first_record.id)
        )
        untouched_sibling = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(invalid_sibling.id)
        )
        untouched_second = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        assert published_first.status == constants.SurveyRelatedRecordStatus.PUBLISHED
        assert untouched_sibling.status != constants.SurveyRelatedRecordStatus.PUBLISHED
        # second_record is valid too, but belongs to a different mission
        assert untouched_second.status != constants.SurveyRelatedRecordStatus.PUBLISHED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_unpublish_survey_related_records(
    db,
    db_session_maker,
    sample_survey_related_records,
):
    # first_record and second_record belong to different survey missions
    first_record, second_record = sample_survey_related_records
    async with db_session_maker() as session:
        await record_commands.set_survey_related_record_status(
            session,
            identifiers.SurveyRelatedRecordId(first_record.id),
            constants.SurveyRelatedRecordStatus.PUBLISHED,
        )
        await record_commands.set_survey_related_record_status(
            session,
            identifiers.SurveyRelatedRecordId(second_record.id),
            constants.SurveyRelatedRecordStatus.PUBLISHED,
        )

        unpublished_count = await record_commands.bulk_unpublish_survey_related_records(
            session, identifiers.SurveyMissionId(first_record.survey_mission_id)
        )
        assert unpublished_count == 1
        updated_first = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(first_record.id)
        )
        untouched_second = await record_queries.get_survey_related_record(
            session, identifiers.SurveyRelatedRecordId(second_record.id)
        )
        assert updated_first.status == constants.SurveyRelatedRecordStatus.DRAFT
        # second_record belongs to a different mission and stays published
        assert untouched_second.status == constants.SurveyRelatedRecordStatus.PUBLISHED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_publish_valid_survey_missions_for_project(
    db,
    db_session_maker,
    sample_survey_missions,
):
    missions_by_project = {}
    for mission in sample_survey_missions:
        missions_by_project.setdefault(mission.project_id, []).append(mission)
    project_id, project_missions = next(
        (pid, ms) for pid, ms in missions_by_project.items() if len(ms) >= 2
    )
    valid_mission, invalid_mission = project_missions[0], project_missions[1]
    other_project_mission = next(
        m for m in sample_survey_missions if m.project_id != project_id
    )

    async with db_session_maker() as session:
        fresh_valid = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(valid_mission.id)
        )
        await mission_commands.update_survey_mission_validation_result(
            session, fresh_valid, validation_result={"is_valid": True, "errors": None}
        )
        # invalid_mission is left with its default (not valid) validation result

        published_ids = (
            await mission_commands.bulk_publish_valid_survey_missions_for_project(
                session, identifiers.ProjectId(project_id)
            )
        )
        assert published_ids == [identifiers.SurveyMissionId(valid_mission.id)]

        published = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(valid_mission.id)
        )
        untouched_invalid = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(invalid_mission.id)
        )
        untouched_other = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(other_project_mission.id)
        )
        assert published.status == constants.SurveyMissionStatus.PUBLISHED
        assert untouched_invalid.status != constants.SurveyMissionStatus.PUBLISHED
        # other_project_mission is valid-or-not is irrelevant - different project
        assert untouched_other.status != constants.SurveyMissionStatus.PUBLISHED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_unpublish_survey_missions_for_project(
    db,
    db_session_maker,
    sample_survey_missions,
):
    missions_by_project = {}
    for mission in sample_survey_missions:
        missions_by_project.setdefault(mission.project_id, []).append(mission)
    project_id, project_missions = next(
        (pid, ms) for pid, ms in missions_by_project.items() if len(ms) >= 2
    )
    first_mission, second_mission = project_missions[0], project_missions[1]
    other_project_mission = next(
        m for m in sample_survey_missions if m.project_id != project_id
    )

    async with db_session_maker() as session:
        await mission_commands.set_survey_mission_status(
            session,
            identifiers.SurveyMissionId(first_mission.id),
            constants.SurveyMissionStatus.PUBLISHED,
        )
        await mission_commands.set_survey_mission_status(
            session,
            identifiers.SurveyMissionId(other_project_mission.id),
            constants.SurveyMissionStatus.PUBLISHED,
        )

        unpublished_ids = (
            await mission_commands.bulk_unpublish_survey_missions_for_project(
                session, identifiers.ProjectId(project_id)
            )
        )
        assert unpublished_ids == [identifiers.SurveyMissionId(first_mission.id)]

        updated_first = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(first_mission.id)
        )
        untouched_second = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(second_mission.id)
        )
        untouched_other = await mission_queries.get_survey_mission(
            session, identifiers.SurveyMissionId(other_project_mission.id)
        )
        assert updated_first.status == constants.SurveyMissionStatus.DRAFT
        assert untouched_second.status != constants.SurveyMissionStatus.PUBLISHED
        # other_project_mission belongs to a different project and stays published
        assert untouched_other.status == constants.SurveyMissionStatus.PUBLISHED
