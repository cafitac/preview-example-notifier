import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import UUID

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import NotificationCreate, NotificationStore, create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as value:
        yield value


def test_health_and_empty_store(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/notifications").json() == []


def test_round_trip_newest_first(client: TestClient) -> None:
    payload = {"nested": [1, True, None, {"text": "한글 😀"}]}
    receipts = []
    for event in ("a", "x" * 100):
        response = client.post("/api/notify", json={"event": event, "payload": payload})
        assert response.status_code == 202
        receipt = response.json()
        assert set(receipt) == {"id", "received_at"}
        assert UUID(receipt["id"])
        assert datetime.fromisoformat(receipt["received_at"]).utcoffset() is not None
        receipts.append(receipt)
    response = client.get("/api/notifications")
    assert response.status_code == 200
    assert response.json() == [
        {**receipts[1], "event": "x" * 100, "payload": payload},
        {**receipts[0], "event": "a", "payload": payload},
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"event": "ok"},
        {"event": "", "payload": {}},
        {"event": "x" * 101, "payload": {}},
        {"event": 1, "payload": {}},
        {"event": "secret\x00", "payload": {}},
        {"event": "\ud800", "payload": {}},
        {"event": "\udfff", "payload": {}},
        {"event": ["secret"], "payload": {}},
        {"event": "ok", "payload": []},
        {"event": "ok", "payload": None},
        {"event": "ok", "payload": "secret"},
        "secret",
    ],
)
def test_validation(client: TestClient, body: object) -> None:
    response = client.post(
        "/api/notify",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert "secret" not in response.text
    assert '"input"' not in response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail
    for error in detail:
        assert set(error) == {"loc", "msg", "type"}
    # Check the actual response schema and each of its declared constraints.
    schema = client.get("/openapi.json").json()
    response_schema = schema["paths"]["/api/notify"]["post"]["responses"]["422"][
        "content"
    ]["application/json"]["schema"]
    schemas = schema["components"]["schemas"]
    assert response_schema == {"$ref": "#/components/schemas/HTTPValidationError"}
    envelope = schemas["HTTPValidationError"]
    assert envelope["type"] == "object"
    assert set(envelope.get("required", [])) <= response.json().keys()
    assert envelope["properties"]["detail"]["type"] == "array"
    assert envelope["properties"]["detail"]["items"] == {
        "$ref": "#/components/schemas/ValidationError"
    }
    item_schema = schemas["ValidationError"]
    assert item_schema["type"] == "object"
    properties = item_schema["properties"]
    assert properties["loc"]["type"] == "array"
    assert properties["loc"]["items"] == {
        "anyOf": [{"type": "string"}, {"type": "integer"}]
    }
    for error in detail:
        assert set(item_schema["required"]) <= error.keys()
        assert isinstance(error["loc"], list)
        assert all(type(part) in (str, int) for part in error["loc"])
        for key in ("msg", "type"):
            assert properties[key]["type"] == "string"
            assert isinstance(error[key], str)
    assert client.get("/api/notifications").json() == []


@pytest.mark.parametrize("event", ["secret\x00", "\ud800", "\udfff"])
def test_invalid_event_uses_model_validation(event: str) -> None:
    with pytest.raises(ValidationError) as raised:
        NotificationCreate(event=event, payload={})
    assert raised.value.errors()[0]["type"] == "value_error"


def test_validation_message_escapes_surrogates() -> None:
    app = create_app()

    @app.get("/test-validation-error")
    def invalid() -> None:
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "event"),
                    "msg": "Invalid character \ud800",
                    "type": "value_error",
                    "input": "secret",
                    "ctx": {"error": ValueError("secret")},
                }
            ]
        )

    with TestClient(app) as client:
        response = client.get("/test-validation-error")
    assert response.status_code == 422
    assert b"\\ud800" in response.content
    assert response.json() == {
        "detail": [
            {
                "loc": ["body", "event"],
                "msg": "Invalid character \ud800",
                "type": "value_error",
            }
        ]
    }


def test_malformed_json(client: TestClient) -> None:
    response = client.post(
        "/api/notify",
        content='{"secret":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "secret" not in response.text


def reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
@pytest.mark.parametrize(
    "payload", ['{"value": NUMBER}', '{"nested": [{"values": [NUMBER]}]}']
)
def test_non_finite_payload_rejected(
    client: TestClient, number: str, payload: str
) -> None:
    response = client.post(
        "/api/notify",
        content='{"event": "ok", "payload": ' + payload.replace("NUMBER", number) + "}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert json.loads(response.content, parse_constant=reject_json_constant) == {
        "detail": [
            {
                "loc": ["body", "payload"],
                "msg": "Value error, Payload numbers must be finite",
                "type": "value_error",
            }
        ]
    }
    listing = client.get("/api/notifications")
    assert listing.status_code == 200
    assert json.loads(listing.content, parse_constant=reject_json_constant) == []


def test_list_returns_strict_json(client: TestClient) -> None:
    payload = {"nested": [1.5, -1e308, 1e308, {"text": "NaN Infinity -Infinity"}]}
    response = client.post("/api/notify", json={"event": "ok", "payload": payload})
    assert response.status_code == 202
    listing = client.get("/api/notifications")
    assert listing.status_code == 200
    entries = json.loads(listing.content, parse_constant=reject_json_constant)
    assert entries == [{**response.json(), "event": "ok", "payload": payload}]


def test_payload_surrogate_round_trip(client: TestClient) -> None:
    body = {"event": "ok", "payload": {"text": "\ud800"}}
    response = client.post(
        "/api/notify",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 202
    assert client.get("/api/notifications").json()[0]["payload"] == body["payload"]


def test_bounded_store() -> None:
    store = NotificationStore()
    for index in range(1005):
        store.add(NotificationCreate(event=str(index), payload={}))
    entries = store.list()
    assert len(entries) == 1000
    assert [entry.event for entry in entries] == [str(i) for i in range(1004, 4, -1)]


def test_store_copies_payloads() -> None:
    store = NotificationStore()
    value = NotificationCreate(event="ok", payload={"nested": {"key": "original"}})
    store.add(value)
    value.payload.clear()
    store.list()[0].payload.clear()
    assert store.list()[0].payload == {"nested": {"key": "original"}}


def test_concurrent_store_access() -> None:
    store = NotificationStore()

    def write_and_read(index: int) -> UUID:
        receipt = store.add(NotificationCreate(event=str(index), payload={}))
        assert len(store.list()) <= 1000
        return receipt.id

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(write_and_read, range(1100)))
    assert len(set(ids)) == 1100
    assert len(store.list()) == 1000
