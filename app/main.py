import json
import math
from collections import deque
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, JsonValue, field_validator


class NotificationCreate(BaseModel):
    event: str = Field(min_length=1, max_length=100)
    payload: dict[str, JsonValue]

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pending: list[JsonValue] = list(value.values())
        while pending:
            item = pending.pop()
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Payload numbers must be finite")
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return value

    @field_validator("event", mode="before")
    @classmethod
    def validate_event(cls, value: object) -> object:
        if isinstance(value, str) and (
            "\x00" in value or any("\ud800" <= char <= "\udfff" for char in value)
        ):
            raise ValueError("Event contains invalid characters")
        return value


class Receipt(BaseModel):
    id: UUID
    received_at: datetime


class Notification(Receipt):
    event: str
    payload: dict[str, JsonValue]


class NotificationStore:
    def __init__(self) -> None:
        self._entries: deque[Notification] = deque(maxlen=1000)
        self._lock = Lock()

    def add(self, value: NotificationCreate) -> Receipt:
        with self._lock:
            entry = Notification(
                id=uuid4(),
                received_at=datetime.now(UTC),
                event=value.event,
                payload=value.model_copy(deep=True).payload,
            )
            self._entries.appendleft(entry)
            return Receipt(id=entry.id, received_at=entry.received_at)

    def list(self) -> list[Notification]:
        with self._lock:
            return [entry.model_copy(deep=True) for entry in self._entries]


def create_app() -> FastAPI:
    app = FastAPI(title="Preview notifier API")
    store = NotificationStore()

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        # Keep the documented error fields without echoing input or error context.
        errors = [
            {key: error[key] for key in ("loc", "msg", "type")}
            for error in exc.errors()
        ]
        return Response(
            content=json.dumps({"detail": errors}, ensure_ascii=True, allow_nan=False),
            status_code=422,
            media_type="application/json",
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/notify", status_code=202, response_model=Receipt)
    def notify(value: NotificationCreate) -> Receipt:
        return store.add(value)

    @app.get("/api/notifications", response_model=list[Notification])
    def notifications() -> Response:
        # Escaping also preserves payload strings containing lone surrogates.
        return Response(
            content=json.dumps(
                jsonable_encoder(store.list()), ensure_ascii=True, allow_nan=False
            ),
            media_type="application/json",
        )

    return app


app = create_app()
