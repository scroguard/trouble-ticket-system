"""Site-wide settings (currently the site name shown in the dashboard)."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import CurrentUser, require_admin
from app.db import SessionLocal, get_db
from app.models import DEFAULT_SITE_NAME, SiteSettings
from app.schemas import SiteSettingsOut, SiteSettingsUpdate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/settings", tags=["admin"], dependencies=[Depends(require_admin)])


def get_site_settings(db: Session, *, for_update: bool = False) -> SiteSettings:
    """The settings row. The migration creates it; recreate it with defaults if it was
    ever deleted by hand."""
    settings = db.get(SiteSettings, 1, with_for_update=for_update)
    if settings is None:
        db.execute(insert(SiteSettings).values(id=1).on_conflict_do_nothing(index_elements=["id"]))
        settings = db.get(SiteSettings, 1, with_for_update=for_update, populate_existing=True)
    return settings


def current_site_name() -> str:
    """For rendering the dashboard page; never fails the page if the DB is down."""
    try:
        with SessionLocal() as db:
            return db.scalar(select(SiteSettings.site_name).where(SiteSettings.id == 1)) or DEFAULT_SITE_NAME
    except Exception:
        log.warning("Could not read site name; using default", exc_info=True)
        return DEFAULT_SITE_NAME


@router.get("", response_model=SiteSettingsOut)
def read_settings(db: Session = Depends(get_db)) -> SiteSettingsOut:
    return SiteSettingsOut.model_validate(get_site_settings(db))


@router.patch("", response_model=SiteSettingsOut)
def update_settings(
    payload: SiteSettingsUpdate, user: CurrentUser, db: Session = Depends(get_db)
) -> SiteSettingsOut:
    settings = get_site_settings(db, for_update=True)
    settings.site_name = payload.site_name
    settings.updated_by_id = user.id
    db.flush()
    db.refresh(settings)
    return SiteSettingsOut.model_validate(settings)
