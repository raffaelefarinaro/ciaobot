from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
import uuid
from typing import Any

from ciao.config import CiaoConfig
from ciao.web.project_chats import ProjectChatManager, ChatInfo
from ciao.provider_service import ProviderService

logger = logging.getLogger(__name__)


def _uuid8() -> str:
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    provider: str
    model: str
    model_bucket: str = ""
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "model_bucket": self.model_bucket,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ProviderRoute | None:
        if not d:
            return None
        return cls(
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            model_bucket=d.get("model_bucket", ""),
            label=d.get("label", ""),
        )


@dataclass(slots=True)
class ProviderSubchatRecord:
    subchat_id: str
    parent_chat_id: str
    parent_turn_index: int
    workspace: str
    project_id: str
    owner: ProviderRoute
    participant: ProviderRoute
    participant_session_id: str = ""
    status: str = "created"  # created, running, waiting_owner, completed, cancelled, failed, interrupted
    created_at: str = ""
    started_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    active_seconds: float = 0.0
    message_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    quota_limit_hit: bool = False
    last_error: str = ""
    limit_extended_at: str = ""
    limit_messages_extended: int = 0
    limit_seconds_extended: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "subchat_id": self.subchat_id,
            "parent_chat_id": self.parent_chat_id,
            "parent_turn_index": self.parent_turn_index,
            "workspace": self.workspace,
            "project_id": self.project_id,
            "owner": self.owner.to_dict(),
            "participant": self.participant.to_dict(),
            "participant_session_id": self.participant_session_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "active_seconds": self.active_seconds,
            "message_count": self.message_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "quota_limit_hit": self.quota_limit_hit,
            "last_error": self.last_error,
            "limit_extended_at": self.limit_extended_at,
            "limit_messages_extended": self.limit_messages_extended,
            "limit_seconds_extended": self.limit_seconds_extended,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProviderSubchatRecord:
        owner_dict = d.get("owner")
        owner = ProviderRoute.from_dict(owner_dict) if owner_dict else ProviderRoute("", "")
        part_dict = d.get("participant")
        participant = ProviderRoute.from_dict(part_dict) if part_dict else ProviderRoute("", "")
        return cls(
            subchat_id=d["subchat_id"],
            parent_chat_id=d["parent_chat_id"],
            parent_turn_index=d["parent_turn_index"],
            workspace=d["workspace"],
            project_id=d["project_id"],
            owner=owner,
            participant=participant,
            participant_session_id=d.get("participant_session_id", ""),
            status=d.get("status", "created"),
            created_at=d.get("created_at", ""),
            started_at=d.get("started_at", ""),
            updated_at=d.get("updated_at", ""),
            completed_at=d.get("completed_at", ""),
            active_seconds=float(d.get("active_seconds", 0.0) or 0.0),
            message_count=int(d.get("message_count", 0) or 0),
            input_tokens=int(d.get("input_tokens", 0) or 0),
            output_tokens=int(d.get("output_tokens", 0) or 0),
            quota_limit_hit=bool(d.get("quota_limit_hit", False)),
            last_error=d.get("last_error", ""),
            limit_extended_at=d.get("limit_extended_at", ""),
            limit_messages_extended=int(d.get("limit_messages_extended", 0) or 0),
            limit_seconds_extended=float(d.get("limit_seconds_extended", 0.0) or 0.0),
        )


class ProviderSubchatManager:
    def __init__(self, config: CiaoConfig, pcm: ProjectChatManager, path: Path):
        self.config = config
        self.pcm = pcm
        self.path = path
        self._records: dict[str, ProviderSubchatRecord] = {}
        self._services: dict[str, ProviderService] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for sid, d in data.get("records", {}).items():
                record = ProviderSubchatRecord.from_dict(d)
                # Reconcile running states to interrupted on startup
                if record.status in ("created", "running"):
                    record.status = "interrupted"
                self._records[sid] = record
        except Exception:
            logger.exception("Failed to load provider sub-chats from %s", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        try:
            payload = {
                "records": {sid: r.to_dict() for sid, r in self._records.items()}
            }
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
        except Exception:
            logger.exception("Failed to save provider sub-chats to %s", self.path)
            if temp_path.exists():
                temp_path.unlink()

    def get_record(self, subchat_id: str) -> ProviderSubchatRecord | None:
        return self._records.get(subchat_id)

    def list_records(self, parent_chat_id: str) -> list[ProviderSubchatRecord]:
        return [
            r for r in self._records.values() if r.parent_chat_id == parent_chat_id
        ]

    def delete_subchat(self, subchat_id: str) -> None:
        if subchat_id in self._records:
            self._records.pop(subchat_id)
            self._save()
        # Delete transcript
        t_file = self._transcript_path(subchat_id)
        if t_file.exists():
            try:
                t_file.unlink()
            except Exception:
                logger.exception("Failed to delete transcript for sub-chat %s", subchat_id)

    def delete_parent_subchats(self, parent_chat_id: str) -> None:
        to_delete = [
            sid for sid, r in self._records.items() if r.parent_chat_id == parent_chat_id
        ]
        for sid in to_delete:
            self.delete_subchat(sid)

    def _transcript_path(self, subchat_id: str) -> Path:
        return self.config.workspace_root / ".runtime" / "provider_subchats" / f"{subchat_id}.jsonl"

    def append_event(self, subchat_id: str, event: dict[str, Any]) -> None:
        t_file = self._transcript_path(subchat_id)
        t_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(t_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            logger.exception("Failed to append event to sub-chat transcript %s", subchat_id)

    def get_events(self, subchat_id: str) -> list[dict[str, Any]]:
        t_file = self._transcript_path(subchat_id)
        if not t_file.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            for line in t_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    # Ignore malformed historical lines
                    continue
        except Exception:
            logger.exception("Failed to read transcript for sub-chat %s", subchat_id)
        return events

    def create_subchat(
        self,
        parent_chat_id: str,
        parent_turn_index: int,
        owner: ProviderRoute,
        participant: ProviderRoute,
    ) -> ProviderSubchatRecord:
        for r in self._records.values():
            if r.parent_chat_id == parent_chat_id and r.status not in ("completed", "cancelled", "failed", "interrupted"):
                raise ValueError("Only one active provider sub-chat is allowed per chat")

        parent_chat = self.pcm._chats.get(parent_chat_id)
        if parent_chat is None:
            raise KeyError("Parent chat not found")

        project = self.pcm._projects.get(parent_chat.project_id)
        workspace = project.workspace if project else ""
        subchat_id = f"sub-{_uuid8()}"

        record = ProviderSubchatRecord(
            subchat_id=subchat_id,
            parent_chat_id=parent_chat_id,
            parent_turn_index=parent_turn_index,
            workspace=workspace,
            project_id=parent_chat.project_id,
            owner=owner,
            participant=participant,
            status="created",
            created_at=_now_iso(),
        )
        self._records[subchat_id] = record
        self._save()

        self.pcm._events.publish({
            "type": "provider_subchat_created",
            "subchat_id": subchat_id,
            "parent_chat_id": parent_chat_id,
            "record": record.to_dict(),
        })
        return record

    async def run_consultation_turn(
        self,
        subchat_id: str,
        prompt: str,
        *,
        user_authorized: bool = False,
    ) -> dict[str, Any]:
        record = self._records.get(subchat_id)
        if record is None:
            raise KeyError("Sub-chat not found")
        if record.status in ("completed", "cancelled", "failed", "interrupted"):
            raise ValueError("Sub-chat is in a terminal state")
        if record.status == "running":
            # A turn runs to completion synchronously up to the first await
            # below, where status is flipped to "running"; a second concurrent
            # call therefore observes it and is rejected rather than sharing the
            # single ProviderService and corrupting the turn/token counters.
            raise ValueError("Sub-chat is already processing a turn")

        if getattr(self.pcm, "_restart_draining", False):
            from ciao.web.project_chats import RestartDrainingError

            raise RestartDrainingError()

        # Check limits
        limit_messages = 12 + record.limit_messages_extended
        if record.message_count >= limit_messages:
            if user_authorized:
                record.limit_messages_extended += 12
                record.limit_extended_at = _now_iso()
            else:
                record.quota_limit_hit = True
                self._save()
                raise ValueError("Message limit reached. Extension requires user authorization.")

        limit_seconds = 1800.0 + record.limit_seconds_extended
        if record.active_seconds >= limit_seconds:
            if user_authorized:
                record.limit_seconds_extended += 1800.0
                record.limit_extended_at = _now_iso()
            else:
                record.quota_limit_hit = True
                self._save()
                raise ValueError("Active execution time limit reached. Extension requires user authorization.")

        record.quota_limit_hit = False
        record.status = "running"
        record.started_at = record.started_at or _now_iso()
        record.updated_at = _now_iso()
        self._save()

        # Publish status
        self.pcm._events.publish({
            "type": "provider_subchat_status",
            "subchat_id": subchat_id,
            "parent_chat_id": record.parent_chat_id,
            "status": record.status,
            "record": record.to_dict(),
        })

        # Append owner message to transcript and publish
        owner_event = {
            "type": "message",
            "role": "owner",
            "content": prompt,
            "timestamp": _now_iso(),
        }
        self.append_event(subchat_id, owner_event)
        self.pcm._events.publish({
            "type": "provider_subchat_event",
            "subchat_id": subchat_id,
            "parent_chat_id": record.parent_chat_id,
            "event": owner_event,
        })

        if subchat_id not in self._services:
            self._services[subchat_id] = ProviderService(self.config, provider=record.participant.provider)

        service = self._services[subchat_id]

        parent_chat = self.pcm._chats.get(record.parent_chat_id)
        transient_chat = ChatInfo(
            chat_id=subchat_id,
            project_id=record.project_id,
            provider=record.participant.provider,
            model=record.participant.model,
            mode=parent_chat.mode if parent_chat else "auto",
            thinking_level=parent_chat.thinking_level if parent_chat else "",
            control_surface=parent_chat.control_surface if parent_chat else "",
        )

        instruction = (
            "You are the participant in an agent handoff. "
            "You are talking to the owner agent, not directly to the user. "
            "Ask the owner for missing information when needed. "
            "Do not start another handoff."
        )
        full_prompt = f"{instruction}\n\n{prompt}"

        request = self.pcm.build_agent_request(
            transient_chat,
            prompt=full_prompt,
            display_prompt=prompt,
            resume_session=record.participant_session_id or None,
        )

        # Environment tags to detect nesting and link context
        request.extra_env["CIAO_PROVIDER_SUBCHAT_ID"] = subchat_id
        request.extra_env["CIAO_PARENT_CHAT_ID"] = record.parent_chat_id

        t0 = time.time()
        had_error = False
        last_err_msg = ""
        response_text = ""

        try:
            async for event in service.execute_streaming(request):
                from ciao.web.chat_broker import apply_file_touches_to_payload, event_to_json
                event_json = event_to_json(event)
                if event_json is None:
                    continue
                apply_file_touches_to_payload(
                    event_json,
                    workspace_root=getattr(
                        getattr(self.pcm, "_config", None), "workspace_root", None
                    ),
                )

                self.pcm._events.publish({
                    "type": "provider_subchat_event",
                    "subchat_id": subchat_id,
                    "parent_chat_id": record.parent_chat_id,
                    "event": event_json,
                })

                if event_json.get("type") != "thinking":
                    self.append_event(subchat_id, event_json)

                if event_json.get("type") == "text_delta":
                    response_text += event_json.get("text", "")
                elif event_json.get("type") == "result":
                    response_text = event_json.get("text", response_text)
                    if event_json.get("is_error"):
                        had_error = True
                        last_err_msg = response_text or "Participant execution failed"
                    if event_json.get("usage"):
                        usage_dict = event_json.get("usage", {})
                        record.input_tokens += int(usage_dict.get("input_tokens", 0) or 0)
                        record.output_tokens += int(usage_dict.get("output_tokens", 0) or 0)

            sess_id = service.current_session_id
            if sess_id:
                record.participant_session_id = sess_id

        except Exception as exc:
            had_error = True
            last_err_msg = str(exc)
            logger.exception("Provider sub-chat turn failed for %s", subchat_id)
            err_event = {"type": "error", "message": last_err_msg, "timestamp": _now_iso()}
            self.append_event(subchat_id, err_event)
            self.pcm._events.publish({
                "type": "provider_subchat_event",
                "subchat_id": subchat_id,
                "parent_chat_id": record.parent_chat_id,
                "event": err_event,
            })
        finally:
            dt = time.time() - t0
            record.active_seconds += dt
            record.message_count += 2

        if had_error:
            record.status = "failed"
            record.last_error = last_err_msg
            record.completed_at = _now_iso()
            await service.disconnect()
            self._services.pop(subchat_id, None)
        else:
            record.status = "waiting_owner"

        record.updated_at = _now_iso()
        self._save()

        self.pcm._events.publish({
            "type": "provider_subchat_status",
            "subchat_id": subchat_id,
            "parent_chat_id": record.parent_chat_id,
            "status": record.status,
            "record": record.to_dict(),
        })

        if had_error:
            raise RuntimeError(last_err_msg)

        return {
            "subchat_id": subchat_id,
            "status": record.status,
            "reply": response_text,
            "usage": {"input_tokens": record.input_tokens, "output_tokens": record.output_tokens},
            "error": record.last_error,
        }

    async def cancel_subchat(self, subchat_id: str) -> None:
        record = self._records.get(subchat_id)
        if record is None:
            raise KeyError("Sub-chat not found")
        if record.status in ("completed", "cancelled", "failed", "interrupted"):
            return

        record.status = "cancelled"
        record.completed_at = _now_iso()
        record.updated_at = _now_iso()
        self._save()

        self.pcm._events.publish({
            "type": "provider_subchat_status",
            "subchat_id": subchat_id,
            "parent_chat_id": record.parent_chat_id,
            "status": record.status,
            "record": record.to_dict(),
        })

        service = self._services.pop(subchat_id, None)
        if service is not None:
            await service.stop_active()
            await service.disconnect()

    def close_subchat(self, subchat_id: str) -> None:
        record = self._records.get(subchat_id)
        if record is None:
            raise KeyError("Sub-chat not found")
        if record.status in ("completed", "cancelled", "failed", "interrupted"):
            return

        record.status = "completed"
        record.completed_at = _now_iso()
        record.updated_at = _now_iso()
        self._save()

        self.pcm._events.publish({
            "type": "provider_subchat_status",
            "subchat_id": subchat_id,
            "parent_chat_id": record.parent_chat_id,
            "status": record.status,
            "record": record.to_dict(),
        })

        asyncio.create_task(self._disconnect_service(subchat_id))

    async def _disconnect_service(self, subchat_id: str) -> None:
        service = self._services.pop(subchat_id, None)
        if service is not None:
            await service.disconnect()

    def extend_subchat(self, subchat_id: str, user_authorized: bool = False) -> None:
        record = self._records.get(subchat_id)
        if record is None:
            raise KeyError("Sub-chat not found")
        if not user_authorized:
            raise ValueError("User authorization required to extend sub-chat")

        record.limit_messages_extended += 12
        record.limit_seconds_extended += 1800.0
        record.limit_extended_at = _now_iso()
        record.quota_limit_hit = False
        record.updated_at = _now_iso()
        self._save()

        self.pcm._events.publish({
            "type": "provider_subchat_status",
            "subchat_id": subchat_id,
            "parent_chat_id": record.parent_chat_id,
            "status": record.status,
            "record": record.to_dict(),
        })

    def respond_permission(
        self,
        subchat_id: str,
        *,
        request_id: str,
        approved: bool,
        reason: str = "",
    ) -> bool:
        service = self._services.get(subchat_id)
        if service is None or service.provider is None:
            return False
        provider = service.provider
        responder = getattr(provider, "send_permission_response", None)
        if callable(responder):
            return bool(responder(request_id, approved))
        gate = getattr(provider, "permission_gate", None)
        if gate is not None:
            return gate.answer(request_id, approved=approved, reason=reason)
        return False

    def respond_question(
        self,
        subchat_id: str,
        *,
        request_id: str,
        answers: dict[str, list[str]],
    ) -> bool:
        service = self._services.get(subchat_id)
        if service is None or service.provider is None:
            return False
        provider = service.provider
        responder = getattr(provider, "send_question_response", None)
        if callable(responder):
            return bool(responder(request_id, answers))
        gate = getattr(provider, "question_gate", None)
        if gate is not None:
            return gate.answer(request_id, answers=answers)
        return False
