"""Selective recommendation: calibration status, observed performance, recalibration and rollback."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Calibration
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
    """Roll back (or forward) to a stored calibration fitted for the current taste model."""
    cal = db.get(Calibration, calibration_id)
    if cal is None:
        raise HTTPException(404, "calibration not found")
    if cal.method != calibration.METHOD or cal.engine_version != calibration.pm.ENGINE_VERSION:
        raise HTTPException(409, "that calibration was fitted for an earlier taste model and cannot be used")
    calibration.activate(db, calibration_id)
    return selective.status(db)
