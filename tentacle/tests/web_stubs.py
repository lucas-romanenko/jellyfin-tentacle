"""Minimal FastAPI/pydantic stand-ins so router modules import in the CI job,
which installs only requests + sqlalchemy (not a test module).

Real packages are used whenever they are installed.
"""
import sys
import types


def _ensure_web_stubs():
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        return
    except ImportError:
        pass

    class _Router:
        def __init__(self, *a, **k):
            pass

        def _deco(self, *a, **k):
            return lambda f: f
        get = post = put = patch = delete = websocket = api_route = _deco

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None, **k):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _Router
    fastapi.Depends = lambda *a, **k: None
    fastapi.Query = lambda default=None, *a, **k: default
    fastapi.HTTPException = HTTPException
    for name in ("Request", "Response", "BackgroundTasks", "UploadFile", "File", "Form"):
        setattr(fastapi, name, type(name, (), {}))
    responses = types.ModuleType("fastapi.responses")
    for name in ("StreamingResponse", "Response", "JSONResponse", "FileResponse",
                 "HTMLResponse", "RedirectResponse"):
        setattr(responses, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    fastapi.responses = responses

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kw):
            for klass in reversed(type(self).__mro__):
                for key in getattr(klass, "__annotations__", {}):
                    if hasattr(klass, key):
                        setattr(self, key, getattr(klass, key))
            for key, value in kw.items():
                setattr(self, key, value)
    pydantic.BaseModel = BaseModel
    pydantic.Field = lambda default=None, *a, **k: default
    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("pydantic", pydantic)
