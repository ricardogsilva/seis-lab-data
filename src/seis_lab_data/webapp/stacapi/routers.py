import logging

from fastapi import APIRouter

from .responses import ERROR_RESPONSES

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/",
    name="landing-page",
    responses=ERROR_RESPONSES,
)
async def landing_page():
    return {"msg": "hi there"}
