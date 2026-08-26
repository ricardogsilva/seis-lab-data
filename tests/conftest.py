import pytest
import pytest_asyncio

import sqlmodel
from anyio import Path
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from seis_lab_data import (
    config,
    constants,
)
from seis_lab_data.cliapp import (
    sampledata,
    utils,
)
from seis_lab_data.db.commands import (
    datasetcategories as category_commands,
    discovery as discovery_commands,
    projects as project_commands,
    surveymissions as mission_commands,
    surveyrelatedrecords as record_commands,
    users as user_commands,
    workflowstages as stage_commands,
)
from seis_lab_data.db.engine import (
    get_engine,
    get_session_maker,
    get_sync_engine,
)
from seis_lab_data.schemas.user import User
from seis_lab_data.schemas.identifiers import UserId
from seis_lab_data.webapp.app import create_app_from_settings
from seis_lab_data.webapp.stacapi.app import create_api_app_from_settings
from seis_lab_data.webapp.stacapi.dependencies import get_settings as stac_get_settings


@pytest_asyncio.fixture(scope="session")
async def bootstrap_data():
    bootstrap_data_path = (
        Path(__file__).parents[1] / "src/seis_lab_data/cliapp/bootstrapdata.toml"
    )
    result = await utils.get_bootstrap_data(bootstrap_data_path)
    yield result


@pytest.fixture
def settings():
    original_settings = config.get_settings()
    original_settings.message_broker_dsn = None
    original_settings.database_dsn = original_settings.test_database_dsn
    return original_settings


@pytest.fixture
def sync_db_engine(settings: config.SeisLabDataSettings):
    yield get_sync_engine(settings)


@pytest.fixture()
def db_engine(settings: config.SeisLabDataSettings):
    yield get_engine(settings.database_dsn.unicode_string(), debug=False)


@pytest.fixture()
def db_session_maker(db_engine):
    yield get_session_maker(db_engine)


@pytest.fixture()
def db(sync_db_engine):
    """Provides a clean database."""
    sqlmodel.SQLModel.metadata.create_all(sync_db_engine)
    yield
    sqlmodel.SQLModel.metadata.drop_all(sync_db_engine)


@pytest.fixture
def app(settings):
    return create_app_from_settings(settings)


@pytest.fixture
def test_client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def stac_api_client(settings):
    """A TestClient for the STAC FastAPI app, mounted the same way it is in
    production (`Mount("/stac", app=api_app, name="stac")` - see
    webapp/app.py), but without any of the main Starlette app's middleware
    or lifespan (auth, sessions, message broker, jinja/translations), since
    none of that is relevant to the STAC API. The named Mount is kept
    because STAC's own link-building relies on `url_for()` resolving
    through it (see `stacapi/links.py`).

    `get_settings` is overridden instead of relying on
    `request.state.settings`, which is normally populated by the main app's
    lifespan.
    """
    api_app = create_api_app_from_settings(settings)
    api_app.dependency_overrides[stac_get_settings] = lambda: settings
    wrapper_app = Starlette(routes=[Mount("/stac", app=api_app, name="stac")])
    with TestClient(wrapper_app) as client:
        yield client


@pytest_asyncio.fixture
async def admin_user(db, db_session_maker):
    admin_user = User(
        id=UserId("testeradmin"),
        username="tester-admin",
        email="testeradmin@tests.dev",
        roles=[constants.ROLE_SYSTEM_ADMIN],
    )
    async with db_session_maker() as session:
        await user_commands.upsert_user(session, admin_user)
    yield admin_user


@pytest_asyncio.fixture
async def bootstrap_dataset_categories(db, db_session_maker, bootstrap_data):
    created = []
    async with db_session_maker() as session:
        for to_create in bootstrap_data[constants.ResourceType.CATEGORY]:
            created.append(
                await category_commands.create_dataset_category(session, to_create)
            )
    yield created


@pytest_asyncio.fixture
async def bootstrap_workflow_stages(db, db_session_maker, bootstrap_data):
    created = []
    async with db_session_maker() as session:
        for to_create in bootstrap_data[constants.ResourceType.WORKFLOW_STAGE]:
            created.append(
                await stage_commands.create_workflow_stage(session, to_create)
            )
    yield created


@pytest_asyncio.fixture
async def bootstrap_asset_discovery_configurations(
    db,
    db_session_maker,
    bootstrap_data,
    bootstrap_dataset_categories,
    bootstrap_workflow_stages,
):
    created = []
    async with db_session_maker() as session:
        for to_create in bootstrap_data[constants.ResourceType.ASSET_DISCOVERY_CONFIG]:
            created.append(
                await discovery_commands.create_asset_discovery_configuration(
                    session, to_create
                )
            )
    yield created


@pytest_asyncio.fixture
async def sample_projects(db, db_session_maker, admin_user):
    created = []
    async with db_session_maker() as session:
        for project_to_create in sampledata.get_projects_to_create(admin_user):
            created.append(
                await project_commands.create_project(session, project_to_create)
            )
    yield created


@pytest_asyncio.fixture
async def sample_survey_missions(db, db_session_maker, sample_projects, admin_user):
    created = []
    async with db_session_maker() as session:
        for survey_mission_to_create in sampledata.get_survey_missions_to_create(
            admin_user
        ):
            created.append(
                await mission_commands.create_survey_mission(
                    session, survey_mission_to_create
                )
            )
    yield created


@pytest_asyncio.fixture
async def sample_survey_related_records(
    db,
    db_session_maker,
    sample_survey_missions,
    bootstrap_dataset_categories,
    bootstrap_workflow_stages,
    admin_user,
):
    created = []
    async with db_session_maker() as session:
        for survey_record_to_create in sampledata.get_survey_related_records_to_create(
            dataset_categories={c.name["en"]: c for c in bootstrap_dataset_categories},
            workflow_stages={w.name["en"]: w for w in bootstrap_workflow_stages},
            owner=admin_user,
        ):
            created.append(
                await record_commands.create_survey_related_record(
                    session, survey_record_to_create
                )
            )
    yield created
