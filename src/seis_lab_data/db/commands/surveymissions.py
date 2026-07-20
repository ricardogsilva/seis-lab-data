import logging

from sqlalchemy import (
    Boolean,
    true,
    update,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from ... import errors
from ...constants import SurveyMissionStatus
from ...schemas import (
    identifiers,
    surveymissions as mission_schemas,
)
from .. import models
from ..queries import surveymissions as mission_queries
from .common import get_bbox_4326_for_db

logger = logging.getLogger(__name__)


async def create_survey_mission(
    session: AsyncSession,
    to_create: mission_schemas.SurveyMissionCreate,
) -> models.SurveyMission:
    survey_mission = models.SurveyMission(
        **to_create.model_dump(exclude={"bbox_4326"}),
        bbox_4326=(
            get_bbox_4326_for_db(bbox)
            if (bbox := to_create.bbox_4326) is not None
            else bbox
        ),
    )
    # need to ensure english name is unique for combination of project and survey mission
    if await mission_queries.get_survey_mission_by_english_name(
        session, identifiers.ProjectId(to_create.project_id), to_create.name.en
    ):
        raise errors.SeisLabDataError(
            f"There is already a survey mission with english name {to_create.name.en!r} for "
            f"the same project."
        )

    session.add(survey_mission)
    await session.commit()
    await session.refresh(survey_mission)
    return await mission_queries.get_survey_mission(session, to_create.id)


async def delete_survey_mission(
    session: AsyncSession,
    survey_mission_id: identifiers.SurveyMissionId,
) -> None:
    if survey_mission := (
        await mission_queries.get_survey_mission(session, survey_mission_id)
    ):
        await session.delete(survey_mission)
        await session.commit()
    else:
        raise errors.SeisLabDataError(
            f"Survey mission with id {survey_mission_id!r} does not exist."
        )


async def update_survey_mission(
    session: AsyncSession,
    survey_mission: models.SurveyMission,
    to_update: mission_schemas.SurveyMissionUpdate,
) -> models.SurveyMission:
    logger.debug(f"{to_update.model_dump()=}")
    for key, value in to_update.model_dump(
        exclude={"bbox_4326"}, exclude_unset=True
    ).items():
        setattr(survey_mission, key, value)
    updated_bbox_4326 = (
        get_bbox_4326_for_db(bbox)
        if (bbox := to_update.bbox_4326) is not None
        else None
    )
    survey_mission.bbox_4326 = updated_bbox_4326
    session.add(survey_mission)
    await session.commit()
    await session.refresh(survey_mission)
    return survey_mission


async def update_survey_mission_validation_result(
    session: AsyncSession,
    survey_mission: models.SurveyMission,
    validation_result: models.ValidationResult,
) -> models.SurveyMission:
    """Unconditionally sets the survey mission's validation result."""
    survey_mission.validation_result = validation_result
    session.add(survey_mission)
    await session.commit()
    await session.refresh(survey_mission)
    return await mission_queries.get_survey_mission(
        session, identifiers.SurveyMissionId(survey_mission.id)
    )


async def set_survey_mission_status(
    session: AsyncSession,
    survey_mission_id: identifiers.SurveyMissionId,
    status: SurveyMissionStatus,
) -> models.SurveyMission:
    """Unconditionally sets the survey mission's status."""
    if (
        survey_mission := (
            await mission_queries.get_survey_mission(session, survey_mission_id)
        )
    ) is None:
        raise errors.SeisLabDataError(
            f"Survey mission with id {survey_mission_id} does not exist."
        )
    survey_mission.status = status
    session.add(survey_mission)
    await session.commit()
    await session.refresh(survey_mission)
    return await mission_queries.get_survey_mission(
        session, identifiers.SurveyMissionId(survey_mission_id)
    )


async def bulk_publish_valid_survey_missions_for_project(
    session: AsyncSession,
    project_id: identifiers.ProjectId,
) -> list[identifiers.SurveyMissionId]:
    """Publish every already-valid, not-yet-published mission of a project.

    A mission's own validity does not depend on its parent project, so when
    the project (re)becomes valid there is no need to re-validate its
    missions - those already marked valid can be published directly from
    their stored validation result. Returns the ids of the missions that
    got published, so callers can further cascade to those missions' own
    records.
    """
    result = await session.execute(
        update(models.SurveyMission)
        .where(models.SurveyMission.project_id == project_id)
        .where(models.SurveyMission.status != SurveyMissionStatus.PUBLISHED)
        .where(
            models.SurveyMission.validation_result["is_valid"].astext.cast(Boolean)
            == true()
        )
        .values(status=SurveyMissionStatus.PUBLISHED)
        .returning(models.SurveyMission.id)
    )
    await session.commit()
    return [identifiers.SurveyMissionId(row) for row in result.scalars().all()]


async def bulk_unpublish_survey_missions_for_project(
    session: AsyncSession,
    project_id: identifiers.ProjectId,
) -> list[identifiers.SurveyMissionId]:
    """Unpublish every published mission of a project.

    Used when the project becomes invalid, so its missions don't stay
    published without a published parent. Returns the ids of the missions
    that got unpublished, so callers can further cascade to those missions'
    own records.
    """
    result = await session.execute(
        update(models.SurveyMission)
        .where(models.SurveyMission.project_id == project_id)
        .where(models.SurveyMission.status == SurveyMissionStatus.PUBLISHED)
        .values(status=SurveyMissionStatus.DRAFT)
        .returning(models.SurveyMission.id)
    )
    await session.commit()
    return [identifiers.SurveyMissionId(row) for row in result.scalars().all()]
