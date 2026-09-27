from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
import uuid
from typing import Any, Callable

from discord_bot.attention import AttentionDecision, should_accept_attention_decision
from discord_bot.presence_settings import PresenceSettingsStore
from discord_bot.room_context import DiscordRoomContextBuilder
from discord_bot.room_timeline import DiscordRoomTimeline, RoomMessage

logger = logging.getLogger(__name__)


class DiscordPresenceController:
    """Debounced room observation and strictly budgeted voluntary participation."""

    def __init__(
        self, *, timeline: DiscordRoomTimeline, context_builder: DiscordRoomContextBuilder,
        attention: Any, settings: PresenceSettingsStore,
        run_worker: Callable[[Callable[[], Any]], Any],
        generate_social: Callable[..., Any], generate_initiative: Callable[..., Any],
        observe_room: Callable[..., Any] | None = None,
        send_public_message: Callable[..., Any], is_generation_busy: Callable[[], bool],
        diagnostics: Any = None,
        guild_id: str, channel_id: str, clock=time.monotonic, rng=None,
        sleep=asyncio.sleep,
    ):
        self.timeline = timeline
        self.context_builder = context_builder
        self.attention = attention
        self.settings = settings
        self.run_worker = run_worker
        self.generate_social = generate_social
        self.generate_initiative = generate_initiative
        self.observe_room = observe_room
        self.send_public_message = send_public_message
        self.is_generation_busy = is_generation_busy
        self.diagnostics = diagnostics
        self.guild_id = str(guild_id)
        self.channel_id = str(channel_id)
        self.clock = clock
        self.rng = rng or random.Random()
        self.sleep = sleep
        self._started = False
        self._closed = False
        self._social_task: asyncio.Task | None = None
        self._initiative_task: asyncio.Task | None = None
        self._summary_task: asyncio.Task | None = None
        self._room_revision = 0
        self._attention_busy = False
        self._next_summary_retry_at = 0.0
        self._last_attention_at = float("-inf")
        self._last_decision: AttentionDecision | None = None
        saved_attention = self.timeline.state(guild_id=self.guild_id, channel_id=self.channel_id).get("last_attention_at")
        if saved_attention:
            try:
                elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(saved_attention)).total_seconds()
                self._last_attention_at = self.clock() - max(0.0, elapsed)
            except (ValueError, TypeError):
                pass

    async def start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._initiative_task = asyncio.create_task(self._initiative_loop())
        self._schedule_summary()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [self._social_task, self._initiative_task, self._summary_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        self._started = False

    async def on_human_message(self, message: RoomMessage, *, direct: bool) -> None:
        if self._closed or not self.timeline.append_message(message):
            return
        self._room_revision += 1
        self.timeline.set_last_seen_message_id(
            guild_id=self.guild_id, channel_id=self.channel_id,
            message_id=message.discord_message_id,
        )
        self._schedule_summary()
        if direct:
            if self._social_task is not None:
                self._social_task.cancel()
                self._social_task = None
            return
        await self._schedule_social_evaluation()

    async def on_mita_message(self, message: RoomMessage) -> None:
        if self._closed:
            return
        self.timeline.append_message(message)
        self.timeline.set_last_seen_message_id(
            guild_id=self.guild_id, channel_id=self.channel_id,
            message_id=message.discord_message_id,
        )
        self._room_revision += 1
        if self._social_task is not None:
            self._social_task.cancel()
            self._social_task = None
        self._schedule_summary()

    def notify_direct_generation_started(self) -> None:
        self._room_revision += 1
        if self._social_task is not None:
            self._social_task.cancel()
            self._social_task = None

    def notify_direct_generation_finished(self) -> None:
        self._room_revision += 1

    async def notify_room_changed(self) -> None:
        self._room_revision += 1
        if self._social_task is not None:
            self._social_task.cancel()
            self._social_task = None
        self._schedule_summary(catch_up=True)
        await self._schedule_social_evaluation()

    async def _schedule_social_evaluation(self) -> None:
        settings = self.settings.get()
        if not settings.enabled or settings.mode not in {"social", "alive"}:
            if self._social_task is not None:
                self._social_task.cancel()
                self._social_task = None
            return
        if self._social_task is not None:
            self._social_task.cancel()
        revision = self._room_revision
        delay = self.rng.uniform(settings.debounce_min_seconds, settings.debounce_max_seconds)
        self._social_task = asyncio.create_task(self._social_debounce_worker(revision, delay))

    async def _social_debounce_worker(self, revision: int, delay: float) -> None:
        try:
            await self.sleep(delay)
            if revision == self._room_revision:
                await self._evaluate_social(revision)
        except asyncio.CancelledError:
            return
        finally:
            if self._social_task is asyncio.current_task():
                self._social_task = None

    def _local_gate(self, *, kind: str, check_eval_cooldown: bool = True) -> tuple[bool, str]:
        settings = self.settings.get()
        if self._closed or not settings.enabled:
            return False, "disabled"
        if kind == "social" and settings.mode not in {"social", "alive"}:
            return False, "mode_direct"
        if kind == "initiative" and settings.mode != "alive":
            return False, "mode_not_alive"
        state = self.timeline.state(guild_id=self.guild_id, channel_id=self.channel_id)
        pause_until = state.get("pause_until")
        if pause_until:
            try:
                if dt.datetime.fromisoformat(str(pause_until)) > dt.datetime.now(dt.timezone.utc):
                    return False, "paused"
            except (ValueError, TypeError):
                pass
        if self.is_generation_busy():
            return False, "generation_busy"
        if self._attention_busy:
            return False, "attention_busy"
        now = self.clock()
        if check_eval_cooldown and now - self._last_attention_at < settings.evaluation_min_interval_seconds:
            return False, "evaluation_cooldown"
        last_mita = self.timeline.last_mita_message(guild_id=self.guild_id, channel_id=self.channel_id)
        if last_mita:
            try:
                age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_mita.created_at)).total_seconds()
                if age < settings.cooldown_seconds:
                    return False, "voluntary_cooldown"
            except (ValueError, TypeError):
                pass
        hour_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        voluntary_count = self.timeline.count_mita_messages_since(
            guild_id=self.guild_id, channel_id=self.channel_id, since=hour_ago,
        )
        if voluntary_count >= settings.max_per_hour:
            return False, "hourly_budget"
        if kind == "initiative":
            last_human = self.timeline.last_human_message(guild_id=self.guild_id, channel_id=self.channel_id)
            if last_human is None:
                return False, "no_room_activity"
            try:
                quiet = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_human.created_at)).total_seconds()
            except (ValueError, TypeError):
                return False, "invalid_activity_time"
            if quiet < settings.quiet_start_seconds:
                return False, "room_not_quiet"
        return True, "allowed"

    async def _evaluate_social(self, revision: int) -> None:
        allowed, reason = self._local_gate(kind="social")
        if self.diagnostics is not None:
            self.diagnostics.record("presence_gate", kind="social", allowed=allowed, reason=reason)
        if not allowed:
            return
        self._attention_busy = True
        try:
            context = self.context_builder.build()
            decision = await self.run_worker(lambda: self.attention.decide_social(
                room_context=context, initiative_level=self.settings.get().initiative,
            ))
            self._record_decision(decision)
            if revision != self._room_revision:
                return
            self._attention_busy = False
            await self._run_voluntary_generation(decision, kind="social", revision=revision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "presence_evaluation_failed", kind="social",
                    exception_type=type(exc).__name__,
                )
            logger.exception("Discord social presence evaluation failed")
        finally:
            self._attention_busy = False

    async def _initiative_loop(self) -> None:
        try:
            while not self._closed:
                await self.sleep(self.rng.uniform(35.0, 75.0))
                if self.settings.get().mode == "alive":
                    await self._evaluate_initiative()
        except asyncio.CancelledError:
            return

    async def _evaluate_initiative(self) -> None:
        allowed, reason = self._local_gate(kind="initiative")
        if self.diagnostics is not None:
            self.diagnostics.record("presence_gate", kind="initiative", allowed=allowed, reason=reason)
        if not allowed:
            return
        last_human = self.timeline.last_human_message(guild_id=self.guild_id, channel_id=self.channel_id)
        if last_human is None:
            return
        now_utc = dt.datetime.now(dt.timezone.utc)
        quiet = (now_utc - dt.datetime.fromisoformat(last_human.created_at)).total_seconds()
        last_mita = self.timeline.last_mita_message(guild_id=self.guild_id, channel_id=self.channel_id)
        since_row = last_mita.row_id if last_mita and last_mita.row_id else 0
        human_count = self.timeline.human_messages_after(
            guild_id=self.guild_id, channel_id=self.channel_id, row_id=since_row,
        )
        revision = self._room_revision
        self._attention_busy = True
        try:
            context = self.context_builder.build()
            decision = await self.run_worker(lambda: self.attention.decide_initiative(
                room_context=context,
                initiative_level=self.settings.get().initiative,
                silence_seconds=quiet,
                human_messages_since_mita=human_count,
            ))
            self._record_decision(decision)
            if revision != self._room_revision:
                return
            self._attention_busy = False
            await self._run_voluntary_generation(decision, kind="initiative", revision=revision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "presence_evaluation_failed", kind="initiative",
                    exception_type=type(exc).__name__,
                )
            logger.exception("Discord initiative evaluation failed")
        finally:
            self._attention_busy = False

    def _record_decision(self, decision: AttentionDecision) -> None:
        self._last_attention_at = self.clock()
        self._last_decision = decision
        self.timeline.update_presence_state(
            guild_id=self.guild_id, channel_id=self.channel_id,
            last_attention_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            last_attention_action=decision.action,
            last_attention_reason=decision.reason_code,
        )
        if self.diagnostics is not None:
            self.diagnostics.record(
                "presence_decision", action=decision.action, desire=decision.desire,
                speak=decision.speak, reason=decision.reason_code,
                diagnostic_code=decision.diagnostic_code, status=decision.diagnostic_status,
            )

    async def _run_voluntary_generation(
        self, decision: AttentionDecision, *, kind: str, revision: int,
    ) -> bool:
        if not decision.speak:
            if self.diagnostics is not None:
                self.diagnostics.record("presence_send_skipped", reason="attention_silent")
            return False
        settings = self.settings.get()
        if not should_accept_attention_decision(
            decision, initiative_level=settings.initiative, rng=self.rng,
        ):
            if self.diagnostics is not None:
                self.diagnostics.record("presence_send_skipped", reason="desire_below_threshold")
            return False
        allowed, gate_reason = self._local_gate(kind=kind, check_eval_cooldown=False)
        if not allowed or revision != self._room_revision:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "presence_send_skipped",
                    reason=gate_reason if not allowed else "room_changed",
                )
            return False
        request_id = f"discord-{kind}-{uuid.uuid4().hex}"
        context = self.context_builder.build()
        if kind == "social":
            response = await self.generate_social(context, request_id)
        else:
            response = await self.generate_initiative(context, request_id, decision.action)
        if response is None or revision != self._room_revision:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "presence_send_skipped",
                    reason="generation_failed" if response is None else "room_changed",
                )
            return False
        text = str(getattr(response, "text", "") or "").strip()
        if not text or getattr(response, "error", "") or getattr(response, "error_message", ""):
            if self.diagnostics is not None:
                self.diagnostics.record("presence_send_skipped", reason="empty_response" if not text else "generation_failed")
            return False
        if self.diagnostics is not None:
            self.diagnostics.record("presence_generation_finished", response_chars=len(text))
        allowed, gate_reason = self._local_gate(kind=kind, check_eval_cooldown=False)
        if not allowed or revision != self._room_revision:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "presence_send_skipped",
                    reason=gate_reason if not allowed else "room_changed",
                )
            return False
        try:
            sent = await self.send_public_message(text, decision.reply_to_message_id, f"mita_{decision.action}")
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "presence_send_failed", exception_type=type(exc).__name__,
                    status=getattr(exc, "status", None),
                )
            raise
        if sent is not None and self.diagnostics is not None:
            self.diagnostics.record("presence_message_sent", action=decision.action)
        return sent is not None

    async def speak_now(self) -> bool:
        return await self._run_voluntary_generation(
            AttentionDecision(True, "join", 100, None, "invited"),
            kind="social", revision=self._room_revision,
        )

    async def test_attention(self) -> AttentionDecision:
        context = self.context_builder.build()
        decision = await self.run_worker(lambda: self.attention.decide_social(
            room_context=context, initiative_level=self.settings.get().initiative,
        ))
        self._record_decision(decision)
        return decision

    def _schedule_summary(self, *, catch_up: bool = False) -> None:
        if self._closed or self._summary_task is not None and not self._summary_task.done():
            return
        if self.clock() < self._next_summary_retry_at:
            return
        pending = self.timeline.count_unsummarized(guild_id=self.guild_id, channel_id=self.channel_id)
        if pending < self.settings.get().summary_threshold and not (
            catch_up and pending > self.context_builder.recent_limit
        ):
            return
        self._summary_task = asyncio.create_task(self.summarize_room())

    async def summarize_room(self) -> bool:
        summary_saved = False
        try:
            settings = self.settings.get()
            messages = self.timeline.messages_for_summary(
                guild_id=self.guild_id, channel_id=self.channel_id,
                keep_recent=settings.summary_keep_recent, max_items=settings.summary_max_batch,
            )
            if not messages:
                return
            previous = self.timeline.get_summary(guild_id=self.guild_id, channel_id=self.channel_id)
            summary = await self.run_worker(lambda: self.attention.summarize_room(
                previous_summary=previous, messages=messages,
            ))
            if summary:
                self.timeline.commit_summary(
                    guild_id=self.guild_id, channel_id=self.channel_id,
                    summary=summary, through_row_id=int(messages[-1].row_id or 0),
                )
                summary_saved = True
                self._next_summary_retry_at = 0.0
                if self.observe_room is not None and not self.is_generation_busy():
                    observation = await self.observe_room(
                        self.context_builder.build(), f"discord-observe-{uuid.uuid4().hex}",
                    )
                    if observation is None or getattr(observation, "error", ""):
                        logger.warning("Discord room observation did not complete cleanly")
            else:
                self._next_summary_retry_at = self.clock() + 600.0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Discord room summarization failed")
            self._next_summary_retry_at = self.clock() + 600.0
        finally:
            self._summary_task = None
            if summary_saved and not self._closed and self.timeline.count_unsummarized(
                guild_id=self.guild_id, channel_id=self.channel_id,
            ) > self.context_builder.recent_limit:
                self._schedule_summary(catch_up=True)
        return summary_saved

    def status(self) -> dict[str, Any]:
        settings = self.settings.get()
        state = self.timeline.state(guild_id=self.guild_id, channel_id=self.channel_id)
        decision = self._last_decision
        return {
            "enabled": settings.enabled,
            "mode": settings.mode,
            "initiative": settings.initiative,
            "cooldown_seconds": settings.cooldown_seconds,
            "max_per_hour": settings.max_per_hour,
            "messages_seen": self.timeline.count_messages(guild_id=self.guild_id, channel_id=self.channel_id),
            "last_attention_action": decision.action if decision else state.get("last_attention_action", ""),
            "last_attention_reason": decision.reason_code if decision else state.get("last_attention_reason", ""),
            "paused_until": state.get("pause_until"),
            "attention_busy": self._attention_busy,
            "generation_busy": self.is_generation_busy(),
        }

    def set_pause(self, minutes: int | None) -> None:
        pause_until = None
        if minutes is not None:
            pause_until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=max(1, min(1440, int(minutes))))).isoformat()
        self.timeline.update_presence_state(
            guild_id=self.guild_id, channel_id=self.channel_id, pause_until=pause_until,
        )

    def clear_room(self) -> int:
        if self._social_task is not None:
            self._social_task.cancel()
            self._social_task = None
        self._room_revision += 1
        last_seen = self.timeline.last_seen_message_id(guild_id=self.guild_id, channel_id=self.channel_id)
        return self.timeline.clear_room(
            guild_id=self.guild_id, channel_id=self.channel_id, preserve_last_seen=last_seen,
        )
