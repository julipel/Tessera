from httpx import AsyncClient

from app.logs import TRACE_ID_HEADER


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_trace_id_is_propagated(client: AsyncClient) -> None:
    response = await client.get("/health", headers={TRACE_ID_HEADER: "abc123"})

    assert response.headers[TRACE_ID_HEADER] == "abc123"


async def test_trace_id_is_generated_when_missing(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")

    assert first.headers[TRACE_ID_HEADER]
    assert first.headers[TRACE_ID_HEADER] != second.headers[TRACE_ID_HEADER]
