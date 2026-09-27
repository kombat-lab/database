"""Explicit public dependencies shared by legacy regression fixtures."""
from database import db
from public_catalog import PublicCatalogHandlers
from public_presentation import PublicContext

app = PublicCatalogHandlers(PublicContext(db))
app.dp = app.router
