from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.services.confluence import confluence
from app.services.connector_queue import connector_queue
from app.services.embeddings import embeddings
from app.services.llm import llm
from app.services.queue import ingestion_queue


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await embeddings.close()
    await confluence.close()
    await llm.close()
    await ingestion_queue.close()
    await connector_queue.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "REST API RAG-платформы: рабочие пространства, документы, базы знаний, "
        "поиск и ответы с источниками. Бизнес-маршруты используют префикс /api/v1. "
        "В защищённом развёртывании HTTPBearer — пользовательский OIDC access token. "
        "При AUTH_ENABLED=false пользовательская авторизация отключена только для пилота. "
        "ConnectorBearer — отдельный машинный credential, выданный при регистрации "
        "коннектора; он нужен независимо от режима пользовательской авторизации. "
        "Потоковый ответ использует POST и text/event-stream, не обычный JSON."
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_request: Request, exc: RequestValidationError):
    # Pydantic's default `input` includes plaintext password fields even when
    # their declared type is SecretStr. Never reflect request values/context.
    return JSONResponse(status_code=422, content={"detail": [
        {"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]}
        for error in exc.errors()
    ]})


@app.middleware("http")
async def private_response_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/health":
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

_generated_openapi = app.openapi


def api_openapi() -> dict:
    schema = _generated_openapi()
    if settings.auth_enabled and settings.auth_mode == "password":
        cookie = "__Host-rag_session" if settings.auth_cookie_secure else "rag_session"
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["SessionCookie"] = {
            "type": "apiKey", "in": "cookie", "name": cookie,
            "description": "Opaque HttpOnly session issued by /api/v1/auth/login. Mutations require Origin and X-CSRF-Token from /auth/me.",
        }
        for path in schema.get("paths", {}).values():
            for method, operation in path.items():
                if not isinstance(operation, dict):
                    continue
                uses_session = False
                for requirement in operation.get("security", []):
                    if "HTTPBearer" in requirement:
                        requirement["SessionCookie"] = requirement.pop("HTTPBearer")
                        uses_session = True
                if uses_session and method.lower() in {"post", "put", "patch", "delete"}:
                    operation.setdefault("parameters", []).append({
                        "name": "X-CSRF-Token", "in": "header", "required": True,
                        "schema": {"type": "string"}, "description": "Session CSRF token returned by /auth/me",
                    })
    if not settings.auth_enabled:
        # User tokens are optional in the explicitly unauthenticated pilot.
        # Connector credentials still protect their own machine endpoints.
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if not isinstance(operation, dict) or "security" not in operation:
                    continue
                requirements = [
                    {name: scopes for name, scopes in requirement.items() if name != "HTTPBearer"}
                    for requirement in operation["security"]
                ]
                if any(not requirement for requirement in requirements):
                    operation.pop("security")
                else:
                    operation["security"] = requirements
    return schema


app.openapi = api_openapi


@app.get("/")
async def root() -> dict:
    return {"service": settings.app_name, "docs": app.docs_url, "health": "/api/v1/health"}
