from fastapi.testclient import TestClient
import jwt

from services.api_gateway.main import JWT_ALGORITHM, JWT_SECRET, app


def test_gateway_requires_bearer_for_user_routes():
    client = TestClient(app)
    response = client.get("/api/v1/catalog")
    assert response.status_code == 401


def test_gateway_requires_valid_superadmin_token():
    client = TestClient(app)
    response = client.get("/api/v1/admin/overview", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_gateway_rejects_non_superadmin_role_on_admin_route():
    client = TestClient(app)
    token = jwt.encode({"sub": "u1", "roles": ["Viewer"]}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    response = client.get("/api/v1/admin/overview", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
