"""Persisted bot and provider session state."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ciao.models import (
    BotState,
    BridgeMode,
    ChatContext,
    ContextState,
    ProviderSessionData,
)


class StateStore:
    """JSON-backed state storage with per-context conversation state."""

    def __init__(
        self,
        path: Path,
        workspace_root: Path,
        media_root: Path,
        *,
        default_model: str = "sonnet",
        default_mode: BridgeMode = "auto",
    ) -> None:
        self._path = path
        self._workspace_root = workspace_root
        self._media_root = media_root
        self._default_model = default_model
        self._default_mode = default_mode
        self.bot_state, self._contexts = self._load()

    def reload(self) -> None:
        """Refresh in-memory state from disk."""
        self.bot_state, self._contexts = self._load()

    # ── Context access ────────────────────────────────────────────────────

    def get_context(self, ctx: ChatContext) -> ContextState:
        """Return ContextState for *ctx*, creating from defaults if missing."""
        key = ctx.key
        if key not in self._contexts:
            # Bind the "default" key (from v1/v2 migration) to the first real ctx
            if "default" in self._contexts:
                self._contexts[key] = self._contexts.pop("default")
            else:
                self._contexts[key] = self._make_default_context()
            self.save()
        return self._contexts[key]

    # ── Per-context methods ───────────────────────────────────────────────

    def set_active_model(self, model: str, ctx: ChatContext) -> None:
        cs = self.get_context(ctx)
        cs.active_model = model
        self.save()

    def set_last_effective_model(self, model: str, ctx: ChatContext) -> None:
        self.get_context(ctx).last_effective_model = model
        self.save()

    def get_selected_model(self, ctx: ChatContext) -> str:
        return self.get_context(ctx).active_model

    def get_mode(self, ctx: ChatContext) -> BridgeMode:
        return self.get_context(ctx).mode

    def get_session_id(self, ctx: ChatContext) -> str:
        return self.get_context(ctx).session.session_id

    def update_session(self, session_id: str | None, ctx: ChatContext) -> None:
        if session_id:
            data = self.get_context(ctx).session
            data.session_id = session_id
            data.message_count += 1
            self.save()

    def set_mode(self, mode: BridgeMode, ctx: ChatContext) -> None:
        self.get_context(ctx).mode = mode
        self.save()

    def reset_active_session(self, ctx: ChatContext) -> None:
        cs = self.get_context(ctx)
        cs.session = ProviderSessionData()
        self.save()

    # ── Global methods ────────────────────────────────────────────────────

    def set_usage(self, usage: dict[str, str]) -> None:
        self.bot_state.usage = usage
        self.save()

    def set_quota(self, quota: dict[str, str]) -> None:
        bucket = quota.get("rateLimitType", "unknown")
        self.bot_state.quota_buckets[bucket] = quota
        self.bot_state.quota = quota
        self.save()

    def add_cost(self, cost_usd: float) -> None:
        self.bot_state.cost += cost_usd
        self.save()

    def reset_costs(self) -> None:
        self.bot_state.cost = 0.0
        self.save()

    # ── Persistence ───────────────────────────────────────────────────────

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 3,
            "bot_state": asdict(self.bot_state),
            "contexts": {key: asdict(cs) for key, cs in self._contexts.items()},
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def _load(self) -> tuple[BotState, dict[str, ContextState]]:
        if not self._path.exists():
            return self._make_default_bot_state(), {}

        data = json.loads(self._path.read_text(encoding="utf-8"))
        version = data.get("version", 1)
        if version >= 3:
            return self._load_v3(data)
        if version == 2:
            return self._migrate_v2(data)
        return self._migrate_v1(data)

    def _load_v3(self, data: dict) -> tuple[BotState, dict[str, ContextState]]:
        bot_data = data.get("bot_state", {})
        bot_state = BotState(
            workspace_root=bot_data.get("workspace_root", str(self._workspace_root)),
            media_root=bot_data.get("media_root", str(self._media_root)),
            usage=bot_data.get("usage", {}),
            quota=bot_data.get("quota", {}),
            quota_buckets=bot_data.get("quota_buckets", {}),
            cost=float(bot_data.get("cost", 0.0)),
        )
        contexts: dict[str, ContextState] = {}
        for key, cs_data in data.get("contexts", {}).items():
            raw_session = cs_data.get("session", {})
            contexts[key] = ContextState(
                active_model=cs_data.get("active_model", self._default_model),
                last_effective_model=cs_data.get("last_effective_model", ""),
                mode=cs_data.get("mode", self._default_mode),
                session=ProviderSessionData(
                    session_id=raw_session.get("session_id", ""),
                    message_count=raw_session.get("message_count", 0),
                ),
                context_mode=cs_data.get("context_mode", "auto"),
            )
        return bot_state, contexts

    def _migrate_v2(self, data: dict) -> tuple[BotState, dict[str, ContextState]]:
        """Migrate v2 state (per-provider dicts) to v3 (Claude-only flat fields)."""
        bot_data = data.get("bot_state", {})
        bot_state = BotState(
            workspace_root=bot_data.get("workspace_root", str(self._workspace_root)),
            media_root=bot_data.get("media_root", str(self._media_root)),
            usage=bot_data.get("provider_usage", {}).get("claude", {}),
            quota=bot_data.get("provider_quota", {}).get("claude", {}),
            cost=float(bot_data.get("provider_costs", {}).get("claude", 0.0)),
        )
        contexts: dict[str, ContextState] = {}
        for key, cs_data in data.get("contexts", {}).items():
            raw_sessions = cs_data.get("sessions", {})
            claude_session = raw_sessions.get("claude", {})
            contexts[key] = ContextState(
                active_model=cs_data.get("selected_models", {}).get(
                    "claude", cs_data.get("active_model", self._default_model)
                ),
                last_effective_model=cs_data.get("last_effective_model", ""),
                mode=cs_data.get("provider_modes", {}).get("claude", self._default_mode),
                session=ProviderSessionData(
                    session_id=claude_session.get("session_id", ""),
                    message_count=claude_session.get("message_count", 0),
                ),
                context_mode=cs_data.get("context_mode", "auto"),
            )
        return bot_state, contexts

    def _migrate_v1(self, data: dict) -> tuple[BotState, dict[str, ContextState]]:
        """Migrate v1 state.json (global BotState + SessionState) to v3."""
        bot_data = data.get("bot_state", {})
        bot_state = BotState(
            workspace_root=bot_data.get("workspace_root", str(self._workspace_root)),
            media_root=bot_data.get("media_root", str(self._media_root)),
            usage=bot_data.get("provider_usage", {}).get("claude", {}),
            quota=bot_data.get("provider_quota", {}).get("claude", {}),
            cost=float(bot_data.get("provider_costs", {}).get("claude", 0.0)),
        )

        raw_sessions = data.get("session_state", {}).get("provider_sessions", {})
        claude_session = raw_sessions.get("claude", {})
        default_ctx = ContextState(
            active_model=bot_data.get("selected_models", {}).get(
                "claude", bot_data.get("active_model", self._default_model)
            ),
            last_effective_model=bot_data.get("last_effective_model", ""),
            mode=bot_data.get("provider_modes", {}).get("claude", self._default_mode),
            session=ProviderSessionData(
                session_id=claude_session.get("session_id", ""),
                message_count=claude_session.get("message_count", 0),
            ),
        )
        return bot_state, {"default": default_ctx}

    # ── Helpers ────────────────────────────────────────────────────────────

    def _make_default_bot_state(self) -> BotState:
        return BotState(
            workspace_root=str(self._workspace_root),
            media_root=str(self._media_root),
        )

    def _make_default_context(self) -> ContextState:
        return ContextState(
            active_model=self._default_model,
            mode=self._default_mode,
        )
