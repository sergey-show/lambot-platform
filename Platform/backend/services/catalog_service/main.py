from fastapi import FastAPI

from shared.contracts.api import CatalogItem

app = FastAPI(title="catalog-service", version="0.1.0")

CATALOG: list[CatalogItem] = [
    CatalogItem(
        id="saas-web-app",
        name="SaaS Web App",
        category="blueprint",
        description="Multi-tenant web app with API backend and admin dashboard.",
    ),
    CatalogItem(
        id="marketplace",
        name="Marketplace",
        category="blueprint",
        description="Buyer-seller platform with catalog, checkout, and roles.",
    ),
    CatalogItem(
        id="python-fastapi-ts-react",
        name="Python FastAPI + TS Frontend",
        category="stack",
        description="Backend on FastAPI, frontend on TypeScript stack.",
    ),
]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/catalog", response_model=list[CatalogItem])
def list_catalog() -> list[CatalogItem]:
    return CATALOG
