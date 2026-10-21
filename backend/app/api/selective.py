"""Selective recommendation: calibration status, observed performance, recalibration and rollback."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import calibration, jobs, selective

router = APIRouter(prefix="/api/selective", tags=["selective"])


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Modes and thresholds, calibration state and diagnostics, observed approval by mode, calibration history."""
    return selective.status(db)


@router.post("/recalibrate")
def recalibrate():
    """Refit the approval calibration now (in the background), even if few decisions changed."""
    return {"job_id": jobs.enqueue("recalibrate", {"force": True})}


@router.post("/calibrations/{calibration_id}/activate")
def activate(calibration_id: int, db: Session = Depends(get_db)):
    """Roll back (or forward) to a stored calibration, only if it could be used now: fitted under the current taste
    model, photo representation, profile and reading of your decisions, on decisions that all still stand."""
    try:
        calibration.activate(db, calibration_id)
    except LookupError as e:
        raise HTTPException(404, "calibration not found") from e
    except calibration.NotUsable as e:
        raise HTTPException(409, f"That calibration cannot be used now: {e}.") from e
    return selective.status(db)
