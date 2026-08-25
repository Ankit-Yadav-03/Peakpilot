"""
api/app.py
FastAPI REST + WebSocket API for the Peakpilot pipeline.
Fixed: async broadcast task creation, result_to_dict field names,
       missing months_md field, DB-not-found guard, pipeline callback wiring.
"""
from __future__ import annotations
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List
 
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from runtime.pipeline import Pipeline
 
logger = logging.getLogger(__name__)
 
 
def create_app(pipeline: Pipeline = None, system_config = None, db_path: str = "Peakpilot.db") -> FastAPI:
 
    app = FastAPI(
        title="Peakpilot — Industrial Decision Engine",
        description="DERC HT Industrial Real-Time MDI Optimization",
        version="1.0.0",
    )
 
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
 
    # ------------------------------------------------------------------
    # WebSocket connection manager
    # ------------------------------------------------------------------
    class ConnectionManager:
        def __init__(self):
            self.active: List[WebSocket] = []
 
        async def connect(self, ws: WebSocket):
            await ws.accept()
            self.active.append(ws)
 
        def disconnect(self, ws: WebSocket):
            if ws in self.active:
                self.active.remove(ws)
 
        async def broadcast(self, data: dict):
            dead = []
            for ws in self.active:
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.append(ws)
            for d in dead:
                self.disconnect(d)
 
    manager = ConnectionManager()

    decision_engine = pipeline._decision_engine if pipeline else None
 
    # ------------------------------------------------------------------
    # Result serialisation — field names must exactly match index.html
    # ------------------------------------------------------------------
    def _result_to_dict(result) -> Dict[str, Any]:
        if result is None:
            return {}
 
        tick    = result.tick
        risk    = result.risk_state
        cost    = result.cost_state
        rec     = result.final_recommendation
        tariff  = result.tariff_state
        anomaly = result.anomaly_state
 
        tick_d = {
            "kw":              tick.kw,
            "kva":             tick.kva,
            "kvar":            tick.kvar,
            "pf":              tick.pf,
            "frequency":       tick.frequency,
            "kvah_cumulative": tick.kvah_cumulative,
            "voltage_l1":      tick.voltage_l1,
            "voltage_l2":      tick.voltage_l2,
            "voltage_l3":      tick.voltage_l3,
            "source":          tick.source,
            "data_quality":    tick.data_quality,
        }
 
        # risk — note model uses capital MDI / kVAh / MD attribute names
        risk_d: Dict[str, Any] = {}
        if risk:
            risk_d = {
                "risk_level":              risk.risk_level,
                "projected_mdi_kva":       risk.projected_MDI_kva,
                "contract_demand_kva":     risk.contract_demand_kva,
                "headroom_kva":            risk.headroom_kva,
                "elapsed_minutes":         risk.elapsed_minutes,
                "remaining_minutes":       risk.remaining_minutes,
                "accumulated_kvah":        risk.accumulated_kVAh,
                "months_md_so_far_kva":    risk.months_MD_so_far_kva,
                "will_set_new_monthly_md": risk.will_set_new_monthly_MD,
                "billing_cycle_day":       risk.billing_cycle_day,
                "escalation_reasons":      risk.escalation_reasons,
            }
 
        cost_d: Dict[str, Any] = {}
        if cost:
            cost_d = {
                "projected_monthly_bill":      cost.projected_monthly_bill,
                "instantaneous_rate_per_hour": cost.instantaneous_cost_rate_per_hour,
                "demand_charge":               cost.demand_charge,
                "excess_surcharge":            cost.excess_surcharge,
                "energy_charge":               cost.energy_charge,
                "drrs":                        cost.drrs,
                "pension_trust":               cost.pension_trust,
                "ppac":                        cost.ppac,
                "electricity_duty":            cost.electricity_duty,
            }
 
        tariff_d: Dict[str, Any] = {}
        if tariff:
            tariff_d = {
                "tod_window":            tariff.tod_window,
                "effective_energy_rate": tariff.effective_energy_rate,
                "tod_multiplier":        tariff.tod_multiplier,
                "is_tod_applicable":     tariff.is_tod_applicable,
            }
 
        anomaly_d: Dict[str, Any] = {}
        if anomaly:
            anomaly_d = {
                "inrush_detected":           anomaly.inrush_detected,
                "inrush_suppression_active": anomaly.inrush_suppression_active,
                "load_creep_detected":       anomaly.load_creep_detected,
                "stale_data_detected":       anomaly.stale_data_detected,
                "flags":                     anomaly.anomaly_flags,
            }
 
        rec_d: Dict[str, Any] = {}
        if rec:
            rec_d = {
                "decision_id":                rec.decision_id,
                "type":                       rec.recommendation_type,
                "risk_level":                 rec.risk_level,
                "loads_selected":             rec.loads_selected,
                "expected_mdi_reduction_kva": rec.expected_mdi_reduction_kva,
                "economic_impact": (
                    {
                        "prevented_md_kva": (
                            rec.economic_impact.prevented_md_kva
                        ),

                        "projected_saving_rupees": (
                            rec.economic_impact.projected_saving_rupees
                        ),

                        "economic_status": (
                            rec.economic_impact.economic_status
                        ),

                        "saving_basis": (
                            rec.economic_impact.saving_basis
                        ),
                    }
                    if rec.economic_impact is not None
                    else None
                ),
                "confidence":                 rec.confidence,
                "display_action":             rec.display_action,
                "message":                    rec.message,
                "suppressed":                 rec.suppressed,
                "trigger":                    rec.trigger,
                "conflict_resolved":          rec.conflict_resolved,
                "intelligent_override":       rec.intelligent_layer_override,
            }

        execution_d: Dict[str, Any] = {}
        if rec and decision_engine:
            execution_d = decision_engine.get_execution_status(rec.decision_id)
 
        return {
            "timestamp":      tick.timestamp.isoformat(),
            "tick":           tick_d,
            "risk":           risk_d,
            "cost":           cost_d,
            "tariff":         tariff_d,
            "anomaly":        anomaly_d,
            "recommendation": rec_d,
            "execution":      execution_d,
            "validation": {
                "valid":    result.validation.valid,
                "warnings": result.validation.warnings,
                "errors":   result.validation.errors,
            },
            "tick_count": pipeline.tick_count if pipeline else 0,
        }
 
    # ------------------------------------------------------------------
    # Wire broadcast into pipeline.
    # on_result is called from inside the running asyncio loop (pipeline
    # is async), so create_task() is always safe here.
    # ------------------------------------------------------------------
    def _on_result_callback(result):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(manager.broadcast(_result_to_dict(result)))
        except Exception as e:
            logger.debug(f"Broadcast schedule error (non-fatal): {e}")
 
    if pipeline is not None:
        pipeline._on_result = _on_result_callback
 
    # ------------------------------------------------------------------
    # DB helper — returns [] if DB file does not exist yet
    # ------------------------------------------------------------------
    def _query_db(sql: str, params: tuple = ()) -> list:
        db_file = Path(db_path)
        if not db_file.exists():
            return []
        
        try:
            conn = sqlite3.connect(str(db_file))

            try:
                conn.row_factory = sqlite3.Row

                rows = conn.execute(
                    sql,
                    params,
                ).fetchall()

                return [
                    dict(r)
                    for r in rows
                ]

            finally:
                conn.close()

        except Exception as e:
            logger.warning(
                f"DB query failed: {e}"
            )

            return []
 
    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------
 
    @app.get("/health")
    def health():
        return {
            "status":     "running" if pipeline and pipeline._running else "stopped",
            "tick_count": pipeline.tick_count if pipeline else 0,
            "facility":   pipeline._facility.facility_name if pipeline else "N/A",
        }
 
    @app.get("/status")
    def status():
        if not pipeline or not pipeline.latest_result:
            return {"status": "no data yet", "tick_count": 0}
        return _result_to_dict(pipeline.latest_result)
 
    @app.get("/latest")
    def latest():
        if not pipeline or not pipeline.latest_result:
            raise HTTPException(status_code=503, detail="No data available yet.")
        return _result_to_dict(pipeline.latest_result)
 
    @app.get("/history")
    def history(limit: int = 100):
        buf = pipeline.results_buffer if pipeline else []
        recent = list(buf)[-limit:]
        return [_result_to_dict(r) for r in recent]
 
    @app.get("/decisions")
    def decisions(limit: int = 50):
        return _query_db(
            "SELECT * FROM decision_events ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
 
    @app.get("/telemetry")
    def telemetry(limit: int = 100):
        return _query_db(
            "SELECT * FROM telemetry_events ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
 
    @app.get("/outcomes")
    def outcomes(limit: int = 50):
        return _query_db(
            "SELECT * FROM outcome_events ORDER BY reconciled_at DESC LIMIT ?", (limit,)
        )

    @app.get("/executions")
    def executions(limit: int = 50):
        return _query_db(
            "SELECT * FROM execution_events ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
 
    @app.get("/config")
    def get_config():
        if not pipeline:
            return {}
        return {
            "facility_id":               pipeline._facility.facility_id,
            "facility_name":             pipeline._facility.facility_name,
            "contract_demand_kva":       pipeline._facility.contract_demand_kva,
            "discom":                    pipeline._facility.discom,
            "voltage_level":             pipeline._facility.voltage_level,
            "billing_cycle_day":         pipeline._billing_cycle_day,
            "safety_margin_kva":         pipeline._system.safety_margin_kva,
            "decision_cooldown_minutes": pipeline._system.decision_cooldown_minutes,
        }
    
    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
 
    @app.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket):
        await manager.connect(websocket)
        logger.info("WebSocket client connected.")
        try:
            if pipeline and pipeline.latest_result:
                await websocket.send_json(_result_to_dict(pipeline.latest_result))
            while True:
                await asyncio.sleep(60)
        except WebSocketDisconnect:
            manager.disconnect(websocket)
            logger.info("WebSocket client disconnected.")
        except Exception as e:
            logger.debug(f"WebSocket error: {e}")
            manager.disconnect(websocket)
 
    # ------------------------------------------------------------------
    # Dashboard UI
    # ------------------------------------------------------------------
 
    @app.get("/ui", response_class=HTMLResponse)
    def ui():
        candidates = [
            Path(__file__).parent.parent / "ui" / "index.html",
            Path("ui") / "index.html",
            Path("Peakpilot") / "ui" / "index.html",
        ]
        for p in candidates:
            if p.exists():
                return HTMLResponse(content=p.read_text(encoding="utf-8"))
        return HTMLResponse(
            content="<h1>UI not found.</h1><p>Expected ui/index.html relative to project root.</p>",
            status_code=404,
        )
 
    return app
