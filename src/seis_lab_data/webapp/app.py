import contextlib
from typing import (
    AsyncIterator,
    TypedDict,
)

import jinja2
import shapely
from pygments.formatters import HtmlFormatter
from authlib.integrations.starlette_client import OAuth
from redis import asyncio as aioredis
from starlette.applications import Starlette
from starlette_babel.contrib.jinja import configure_jinja_env
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import (
    Mount,
    Route,
)
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette_babel import (
    get_translator,
    LocaleMiddleware,
)
from starlette_wtf import (
    CSRFProtectMiddleware,
    csrf_token,
)

from .. import (
    config,
    constants,
)
from ..auth import (
    AuthConfig,
    get_oauth_manager,
)
from ..tasks.broker import setup_broker

from . import jinjafilters
from .auth_backend import OIDCAuthBackend
from .middleware import PublicURLMiddleware
from .routes import (
    auth,
    base,
)
from .routes import datalist
from .routes.projects import routes as projects_routes
from .routes.surveymissions import routes as missions_routes
from .routes.surveyrelatedrecords import routes as records_routes
from .routes.discovery import routes as discovery_routes
from .routes.datasetcategories import routes as dataset_category_routes
from .routes.workflowstages import routes as workflow_stage_routes
from .stacapi.app import create_api_app_from_settings


class State(TypedDict):
    settings: config.SeisLabDataSettings
    templates: Jinja2Templates
    auth_config: AuthConfig
    oauth_manager: OAuth
    redis_client: aioredis.Redis


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[State]:
    settings = config.get_settings()
    auth_config = AuthConfig.from_settings(settings)
    shared_translator = get_translator()
    shared_translator.load_from_directory(settings.translations_dir)
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(settings.templates_dir), autoescape=True
    )
    default_bbox = shapely.from_wkt(settings.webmap_default_bbox_wkt)
    min_lon, min_lat, max_lon, max_lat = default_bbox.bounds
    print(f"{min_lon=}, {min_lat=}, {max_lon=}, {max_lat=}")
    jinja_env.globals.update(
        {
            "csrf_token": csrf_token,
            "icons": settings.icons.model_dump(),
            "item_limits": {
                "ASSET_MAX_LINKS": constants.ASSET_MAX_LINKS,
                "PROJECT_MAX_LINKS": constants.PROJECT_MAX_LINKS,
                "SURVEY_MISSION_MAX_LINKS": constants.SURVEY_MISSION_MAX_LINKS,
                "SURVEY_RELATED_RECORD_MAX_LINKS": constants.SURVEY_RELATED_RECORD_MAX_LINKS,
                "SURVEY_RELATED_RECORD_MAX_ASSETS": constants.SURVEY_RELATED_RECORD_MAX_ASSETS,
                "SURVEY_RELATED_RECORD_MAX_RELATED": constants.SURVEY_RELATED_RECORD_MAX_RELATED,
            },
            "settings": settings,
            "AssetType": constants.AssetType,
            "ProjectStatus": constants.ProjectStatus,
            "SurveyRelatedRecordStatus": constants.SurveyRelatedRecordStatus,
            "default_webmap_bounds": {
                "min_lon": min_lon,
                "max_lon": max_lon,
                "min_lat": min_lat,
                "max_lat": max_lat,
            },
            "pygments_css": HtmlFormatter().get_style_defs(".highlight"),
        }
    )
    jinja_env.filters["translate_localizable_string"] = (
        jinjafilters.translate_localizable_string
    )
    jinja_env.filters["secondary_language"] = jinjafilters.get_secondary_language_value
    jinja_env.filters["translate_enum"] = jinjafilters.translate_enum
    jinja_env.filters["status_icon"] = jinjafilters.get_status_icon_name
    jinja_env.filters["is_deletable"] = jinjafilters.is_deletable
    jinja_env.filters["is_publishable"] = jinjafilters.is_publishable
    jinja_env.filters["is_updatable"] = jinjafilters.is_updatable
    jinja_env.filters["is_unpublishable"] = jinjafilters.is_unpublishable
    jinja_env.filters["highlight_json"] = jinjafilters.highlight_json
    jinja_env.filters["asset_url"] = jinjafilters.get_url_for_asset
    configure_jinja_env(jinja_env)
    templates = Jinja2Templates(env=jinja_env)
    yield State(
        settings=settings,
        templates=templates,
        auth_config=auth_config,
        oauth_manager=get_oauth_manager(auth_config),
        redis_client=aioredis.from_url(settings.message_broker_dsn.unicode_string()),
    )


def create_app_from_settings(settings: config.SeisLabDataSettings) -> Starlette:
    setup_broker(settings)
    api_app = create_api_app_from_settings(settings)
    app = Starlette(
        debug=settings.debug,
        routes=[
            Route("/", base.home, name="home"),
            Route("/login", auth.login),
            Route("/oauth2/callback", auth.auth_callback),
            Route("/logout", auth.logout),
            Route("/profile", base.profile),
            Route("/set-language/{lang}", base.set_language, name="set_language"),
            Mount("/projects", name="projects", routes=projects_routes),
            Mount("/survey-missions", name="survey_missions", routes=missions_routes),
            Mount(
                "/survey-related-records",
                name="survey_related_records",
                routes=records_routes,
            ),
            Mount(
                "/asset-discovery-configurations",
                name="asset_discovery_configurations",
                routes=discovery_routes,
            ),
            Mount(
                "/dataset-categories",
                name="dataset_categories",
                routes=dataset_category_routes,
            ),
            Mount(
                "/workflow-stages",
                name="workflow_stages",
                routes=workflow_stage_routes,
            ),
            Route(
                "/asset-media-types",
                datalist.get_registered_media_types,
                name="asset_media_types",
            ),
            Mount("/stac", app=api_app, name="stac"),
        ],
        lifespan=lifespan,
        middleware=[
            Middleware(PublicURLMiddleware, public_url=str(settings.public_url)),
            Middleware(
                LocaleMiddleware,
                locales=settings.locales,
                default_locale=settings.locales[0],
            ),
            Middleware(
                SessionMiddleware,
                secret_key=settings.session_secret_key,
            ),
            Middleware(AuthenticationMiddleware, backend=OIDCAuthBackend(settings)),
            Middleware(
                CSRFProtectMiddleware,
                csrf_secret=settings.csrf_secret,
            ),
            Middleware(GZipMiddleware, minimum_size=1000, compresslevel=9),
        ],
    )
    settings.static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    return app


def create_app() -> Starlette:
    settings = config.get_settings()
    return create_app_from_settings(settings)
