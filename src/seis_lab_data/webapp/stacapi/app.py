"""FastAPI app for a readonly STAC API.

NOTE: We do not use any lifespan-related functionality when setting up this
FastAPI application because the way that it gets used at runtime is by being
mounted by our main starlette-based app. Therefore, lifespan is configured
in the starlette app.
"""

from fastapi import (
    FastAPI,
    Request,
)
from starlette.responses import JSONResponse

from ... import (
    config,
    errors,
)
from .routers import router


def create_api_app() -> FastAPI:
    return create_api_app_from_settings(config.get_settings())


def create_api_app_from_settings(settings: config.SeisLabDataSettings) -> FastAPI:
    app = FastAPI(
        title="SeisLabData STAC API",
        description="SeisLabData",
        contact={
            "name": "",
            "email": "",
            "url": settings.public_url,
        },
        license_info={
            "name": "",
            "url": settings.public_url,
        },
        openapi_tags=[
            {"name": "", "description": ""},
        ],
        summary="SLD STAC-API server",
        servers=[{"url": f"{settings.public_url}/stac"}],
        root_path_in_servers=False,
    )

    @app.exception_handler(errors.UserNotAllowedError)
    async def _private_resource_as_not_found(
        request: Request, exc: errors.UserNotAllowedError
    ) -> JSONResponse:
        # a private resource must be indistinguishable from a nonexistent one
        # to an anonymous STAC client
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    app.include_router(router)
    return app
