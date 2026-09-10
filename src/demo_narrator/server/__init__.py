"""Single-tenant web server: FastAPI API + background job worker over the engine.

See docs/saas-mvp-architecture.md (M1). The filesystem workspace stays the
source of truth for profiles/flows/videos; SQLite holds jobs and the render
library index.
"""
