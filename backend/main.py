"""
RescueNet AI - FastAPI backend entrypoint.

Run with:
    uvicorn backend.main:app --reload --port 8000

Exposes the multi-agent pipeline as a REST API for the Streamlit dashboard
(or any other client / Postman / curl) to consume.
"""
import faulthandler
faulthandler.enable()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from secure import Secure
import os

from backend.models.schemas import DisasterTriggerRequest, SituationReport
from backend.agents import orchestrator
from backend import database

app = FastAPI(
    title="RescueNet AI",
    description="Multi-agent disaster response command center (academic simulation project).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

secure_headers = Secure()
@app.middleware("http")
async def set_secure_headers(request, call_next):
    response = await call_next(request)
    secure_headers.set_headers(response)
    return response


@app.on_event("startup")
def startup():
    database.init_db()
    # The free deployment is intentionally stateless. Avoid creating a Redis
    # client or an unbounded in-memory HTTP cache on every worker.
    from backend.rag.rag_engine import rag_engine
    if not os.environ.get("QDRANT_URL") and not rag_engine.corpus:
        rag_engine.ingest_documents([
            {"text": "Flood Evacuation: Move to higher ground immediately. Do not walk through moving water. Six inches of moving water can make you fall. Do not drive into flooded areas. If floodwaters rise around your car, abandon the car and move to higher ground if you can do so safely.", "metadata": {"source": "FEMA Flood Protocol", "type": "guideline"}},
            {"text": "Earthquake Response: Drop, Cover, and Hold On. Drop to your hands and knees. Cover your head and neck with your arms. If a sturdy table or desk is nearby, crawl underneath it for shelter. Hold on until the shaking stops.", "metadata": {"source": "Red Cross Earthquake Guide", "type": "guideline"}},
            {"text": "Wildfire Protocols: Evacuate immediately if instructed. Close all doors and windows but do not lock them. If outdoors, look for a body of water or cleared area. Keep your emergency supply kit ready.", "metadata": {"source": "National Fire Protection Association", "type": "guideline"}},
        ])

@app.get("/health")
def health_check():
    return {"status": "healthy", "version": "2.0.0"}

@app.get("/")
def root():
    return {"message": "RescueNet AI backend is running. See /docs for the API."}

from backend.rag.api import router as rag_router
app.include_router(rag_router)

# OpenTelemetry Instrumentation removed to save RAM on Render

from backend.agents.supervisor_v2 import supervisor_graph
from backend.models.schemas import AgentTrace
from backend.core.logging import logger

@app.post("/api/disaster/trigger", response_model=SituationReport)
@limiter.limit("20/minute")
def trigger_disaster(request: Request, req: DisasterTriggerRequest):
    """Runs the full multi-agent pipeline for a newly reported disaster using LangGraph."""
    logger.info("disaster_triggered", type=req.disaster_type, location=req.location_name)
    logger.metric("pipeline_start", 1, tags={"type": req.disaster_type})
    
    report = orchestrator.run_pipeline(req, database.STATE)
    
    database.save_incident(req.disaster_type, req.location_name, report.model_dump())
    
    logger.info("disaster_processed", type=req.disaster_type, location=req.location_name)
    logger.metric("pipeline_complete", 1, tags={"type": req.disaster_type})
    return report



@app.get("/api/state")
def get_state():
    """Current live state of hospitals, shelters, resources, and volunteers."""
    return database.STATE

@app.get("/api/simulation/state")
def get_simulation_state():
    """Get the current state of the dynamic simulation."""
    return database.STATE

@app.post("/api/simulation/tick")
def tick_simulation(ticks: int = 1):
    """Fast forward the disaster simulation by N ticks."""
    database.advance_simulation(ticks)
    return {"message": f"Simulation advanced by {ticks} ticks.", "state": database.STATE}


@app.post("/api/reset")
def reset():
    """Resets hospitals/shelters/resources/volunteers back to defaults (does not touch history)."""
    database.reset_state()
    return {"message": "State reset to defaults."}


@app.get("/api/incidents")
def get_incidents(limit: int = 20):
    """History of past disaster triggers (long-term memory)."""
    return database.list_incidents(limit=limit)


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: int):
    incident = database.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.post("/api/rag/search")
async def rag_search(body: dict):
    """Query the RAG knowledge base for emergency protocols and guidelines."""
    query = body.get("query", "")
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")
    
    try:
        from backend.rag.rag_engine import rag_engine
        from backend.rag.models import RAGQuery
        
        query_obj = RAGQuery(query=query, top_k=5)
        rag_response = rag_engine.retrieve(query_obj)
        
        # Format citations from RAG response
        citations = []
        for c in rag_response.citations:
            citations.append({
                "source_name": c.source_name,
                "text_snippet": c.text_snippet,
                "relevance_score": c.relevance_score,
            })
            
        return {
            "answer": rag_response.answer,
            "citations": citations
        }
    except Exception as e:
        logger.error("rag_search_failed", error=str(e))
        return {"answer": f"RAG search encountered an error: {str(e)}", "citations": []}

