"""Route package: split from one 757-line routes.py into query.py
(data routes) and auth.py (auth routes). Re-exports both routers so
`from core.app.api.routes import router, auth_router` still works
unchanged for app.py."""

from core.app.api.routes.auth import auth_router
from core.app.api.routes.query import router

__all__ = ["router", "auth_router"]
