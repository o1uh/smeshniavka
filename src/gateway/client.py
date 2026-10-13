import asyncio
import copy
import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo
import httpx
from pydantic import BaseModel

from src.ops.config import KeyConfig, AppConfig
from src.ops.log import logger
from src.db.contracts import Gateway


class KeyBucket:
    """Tracks RPM, RPD, TPM and cooldown state for a key or project."""

    def __init__(self, key_config: KeyConfig):
        self.key_env = key_config.key_env
        self.api_key = key_config.api_key
        self.rpm_limit = key_config.rpm_limit
        self.rpd_limit = key_config.rpd_limit
        self.tpm_limit = key_config.tpm_limit
        self.project = key_config.project or key_config.key_env
        self.reserve = key_config.reserve

        self.key_hash = hashlib.sha256(self.api_key.encode()).hexdigest()[:12]
        self.is_dead = False  # 401 or 403
        self.cooldown_until: float = 0.0
        self.minute_429_count: int = 0

        # Usage tracking
        self.rpd_used: int = 0
        self.last_pt_date: str = ""
        self.request_timestamps: list[float] = []
        self.token_timestamps: list[tuple[float, int]] = []

    def check_reset_pt(self, now_pt_date: str) -> None:
        """Resets daily counter at midnight Pacific Time."""
        if self.last_pt_date != now_pt_date:
            self.last_pt_date = now_pt_date
            self.rpd_used = 0
            self.minute_429_count = 0
            # If cooldown was daily, clear it
            if self.cooldown_until > time.time() and self.minute_429_count > 2:
                self.cooldown_until = 0.0

    def prune_sliding_windows(self, now: float) -> None:
        cutoff = now - 60.0
        self.request_timestamps = [t for t in self.request_timestamps if t > cutoff]
        self.token_timestamps = [item for item in self.token_timestamps if item[0] > cutoff]

    def current_rpm(self, now: float) -> int:
        self.prune_sliding_windows(now)
        return len(self.request_timestamps)

    def current_tpm(self, now: float) -> int:
        self.prune_sliding_windows(now)
        return sum(item[1] for item in self.token_timestamps)

    def is_in_cooldown(self, now: float) -> bool:
        return self.is_dead or (self.cooldown_until > now)

    def can_accept(self, priority: str, estimated_tokens: int, rpm_p0_reserve: int, now: float) -> bool:
        if self.is_in_cooldown(now):
            return False

        # Daily limit check (if rpd_limit > 0)
        if self.rpd_limit > 0 and self.rpd_used >= self.rpd_limit:
            return False

        # RPM check
        rpm = self.current_rpm(now)
        available_rpm = self.rpm_limit - rpm
        if priority != "P0":
            if available_rpm <= rpm_p0_reserve:
                return False
        else:
            if available_rpm <= 0:
                return False

        # TPM check
        tpm = self.current_tpm(now)
        if (tpm + estimated_tokens) > self.tpm_limit:
            return False

        return True

    def record_usage(self, estimated_tokens: int, now: float) -> None:
        self.request_timestamps.append(now)
        self.token_timestamps.append((now, estimated_tokens))
        self.rpd_used += 1


def clean_schema_for_gemini(raw_schema: dict) -> dict:
    """
    Cleans Pydantic JSON schema for Google Gemini REST API:
    - Inlines all $ref definitions from $defs
    - Removes $defs, additionalProperties, title, $schema, default
    - Converts anyOf: [{type: T}, {type: null}] to nullable: true
    """
    schema = copy.deepcopy(raw_schema)
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                target = defs.get(ref_name, {})
                return resolve(target)
            res = {}
            for k, v in node.items():
                if k in ("title", "additionalProperties", "$schema", "default"):
                    continue
                if k == "anyOf" and isinstance(v, list):
                    non_null = [item for item in v if item.get("type") != "null"]
                    if len(non_null) == 1:
                        resolved_child = resolve(non_null[0])
                        res.update(resolved_child)
                        res["nullable"] = True
                        continue
                res[k] = resolve(v)
            return res
        elif isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


class GeminiGateway(Gateway):
    """
    Production-grade Gemini Gateway:
    - Multi-key rotation with priority P0/P1/P2
    - Immediate 429 failover to next key
    - Midnight PT quota resets
    - Concurrency control (MAX_CONCURRENT=4, P0 reserve, P2 <= 1)
    - 60s timeout with guaranteed slot release
    - DEGRADED mode notification when all keys are down
    - Structured Pydantic outputs with schema validation
    """

    def __init__(self, app_config: AppConfig, http_client: Optional[httpx.AsyncClient] = None):
        self.config = app_config
        self.bot_config = app_config.bot
        self.keys = [KeyBucket(k) for k in app_config.keys]
        self._client = http_client or httpx.AsyncClient(timeout=60.0)

        # Concurrency & priorities
        self.max_concurrent = self.bot_config.MAX_CONCURRENT
        self._current_total = 0
        self._current_p2 = 0
        self._lock = asyncio.Lock()

        # Shared project buckets (keys with the same project share limits)
        self.project_buckets: dict[str, KeyBucket] = {}
        for k in self.keys:
            if k.project not in self.project_buckets:
                self.project_buckets[k.project] = k

        # Alert callback for down keys
        self.all_keys_down_since: Optional[float] = None
        self.alert_callback = None

    def _get_pt_date_str(self) -> str:
        pt_dt = datetime.now(ZoneInfo("America/Los_Angeles"))
        return pt_dt.strftime("%Y-%m-%d")

    def _sync_all_buckets(self) -> None:
        now_pt = self._get_pt_date_str()
        for k in self.keys:
            k.check_reset_pt(now_pt)

    def get_key_status_summary(self) -> dict[str, Any]:
        """Provides status for all keys (for monitoring / /alive command)."""
        self._sync_all_buckets()
        now = time.time()
        active = [k for k in self.keys if not k.reserve and not k.is_dead]
        reserve = [k for k in self.keys if k.reserve and not k.is_dead]
        in_cooldown = [k for k in self.keys if k.is_in_cooldown(now) and not k.is_dead]
        dead = [k for k in self.keys if k.is_dead]

        total_rpd_cap = sum(k.rpd_limit for k in active if k.rpd_limit > 0)
        total_rpd_used = sum(k.rpd_used for k in active)

        return {
            "total_keys": len(self.keys),
            "active_keys": len(active),
            "reserve_keys": len(reserve),
            "in_cooldown": len(in_cooldown),
            "dead_keys": len(dead),
            "total_rpd_cap": total_rpd_cap,
            "total_rpd_used": total_rpd_used,
            "is_degraded": len(in_cooldown) == (len(active) + len(reserve)),
        }

    def _check_budget(self, priority: str) -> bool:
        """Checks daily budget for P1 and P2 based on active non-reserve keys."""
        active_keys = [k for k in self.keys if not k.reserve and not k.is_dead]
        if not active_keys:
            return False

        total_limit = sum(k.rpd_limit for k in active_keys if k.rpd_limit > 0)
        if total_limit == 0:
            return True  # Unlimited

        total_used = sum(k.rpd_used for k in active_keys)
        pct = (total_used / total_limit) * 100.0

        if priority == "P2" and pct >= self.bot_config.BUDGET_P2_MAX:
            logger.info("P2 budget exceeded (%.1f%% >= %d%%)", pct, self.bot_config.BUDGET_P2_MAX)
            return False
        if priority == "P1" and pct >= self.bot_config.BUDGET_P1_MAX:
            logger.info("P1 budget exceeded (%.1f%% >= %d%%)", pct, self.bot_config.BUDGET_P1_MAX)
            return False
        return True

    def _select_available_key(self, priority: str, estimated_tokens: int) -> Optional[KeyBucket]:
        self._sync_all_buckets()
        now = time.time()
        reserve_reserve_rpm = self.bot_config.RPM_P0_RESERVE

        # 1. Try active keys first
        active_candidates = [
            k for k in self.keys
            if not k.reserve and k.can_accept(priority, estimated_tokens, reserve_reserve_rpm, now)
        ]
        if active_candidates:
            # Pick the key with least used RPD / RPM
            return min(active_candidates, key=lambda k: (k.rpd_used, k.current_rpm(now)))

        # 2. If P0 and active keys are in cooldown / exhausted, try reserve keys
        if priority == "P0":
            reserve_candidates = [
                k for k in self.keys
                if k.reserve and k.can_accept(priority, estimated_tokens, 0, now)
            ]
            if reserve_candidates:
                return min(reserve_candidates, key=lambda k: (k.rpd_used, k.current_rpm(now)))

        return None

    def _mark_key_cooldown(self, key_bucket: KeyBucket, error_type: str, retry_delay: Optional[float] = None) -> None:
        now = time.time()
        if error_type == "RATE_DAILY":
            # Cooldown until midnight PT
            pt_now = datetime.now(ZoneInfo("America/Los_Angeles"))
            midnight_pt = pt_now.replace(hour=23, minute=59, second=59).timestamp()
            key_bucket.cooldown_until = midnight_pt + 60.0
            logger.warning("Key %s hit daily quota limit! Cooldown until PT midnight", key_bucket.key_env)
        elif error_type == "RATE_MINUTE":
            key_bucket.minute_429_count += 1
            if retry_delay and retry_delay > 0:
                cooldown_sec = retry_delay
            elif key_bucket.minute_429_count == 1:
                cooldown_sec = 60.0
            elif key_bucket.minute_429_count == 2:
                cooldown_sec = 300.0  # 5 min
            else:
                # Escalate to PT midnight
                pt_now = datetime.now(ZoneInfo("America/Los_Angeles"))
                cooldown_sec = max(600.0, pt_now.replace(hour=23, minute=59, second=59).timestamp() - now)

            key_bucket.cooldown_until = now + cooldown_sec
            logger.warning(
                "Key %s hit minute rate limit (attempt %d)! Cooldown %.0fs",
                key_bucket.key_env, key_bucket.minute_429_count, cooldown_sec
            )
        elif error_type in ("AUTH_ERROR", "INVALID_KEY"):
            key_bucket.is_dead = True
            logger.error("Key %s invalid or unauthorized (401/403)! Marked DEAD", key_bucket.key_env)

    async def _acquire_slot(self, priority: str) -> bool:
        while True:
            async with self._lock:
                # Slot conditions:
                # 1. Total active < max_concurrent
                # 2. P0 always allowed if total < max_concurrent
                # 3. Non-P0 cannot consume the last slot (reserve 1 slot for P0)
                # 4. P2 cannot exceed 1 concurrent slot
                available_slots = self.max_concurrent - self._current_total
                if priority == "P0":
                    if available_slots > 0:
                        self._current_total += 1
                        return True
                elif priority == "P1":
                    if available_slots > 1:
                        self._current_total += 1
                        return True
                elif priority == "P2":
                    if available_slots > 1 and self._current_p2 < 1:
                        self._current_total += 1
                        self._current_p2 += 1
                        return True
            await asyncio.sleep(0.05)

    async def _release_slot(self, priority: str) -> None:
        async with self._lock:
            self._current_total = max(0, self._current_total - 1)
            if priority == "P2":
                self._current_p2 = max(0, self._current_p2 - 1)

    async def generate(
        self,
        prompt_name: str,
        contents: list[dict[str, Any]],
        system_instruction: Optional[str] = None,
        priority: str = "P0",
        response_schema: Optional[type] = None,
        estimated_tokens: int = 1000,
        enable_search: bool = False
    ) -> Any:
        """
        Executes generation with concurrency slot, key rotation, and immediate failover.
        """
        # 1. Budget check for P1 / P2
        if not self._check_budget(priority):
            return None

        # 2. Acquire concurrency slot
        await self._acquire_slot(priority)

        try:
            return await asyncio.wait_for(
                self._execute_with_rotation(
                    prompt_name=prompt_name,
                    contents=contents,
                    system_instruction=system_instruction,
                    priority=priority,
                    response_schema=response_schema,
                    estimated_tokens=estimated_tokens,
                    enable_search=enable_search
                ),
                timeout=60.0
            )
        except asyncio.TimeoutError:
            logger.error("Gemini gateway call '%s' timed out after 60s", prompt_name)
            return None
        finally:
            await self._release_slot(priority)

    async def _execute_with_rotation(
        self,
        prompt_name: str,
        contents: list[dict[str, Any]],
        system_instruction: Optional[str],
        priority: str,
        response_schema: Optional[type],
        estimated_tokens: int,
        enable_search: bool
    ) -> Any:
        attempted_keys = set()
        model_id = self.bot_config.MODEL_ID

        # Prepare generation config from config/gen.yaml
        gen_params = self.config.gen.get(prompt_name, {})
        temperature = gen_params.get("temperature", 1.0)
        max_output_tokens = gen_params.get("max_output_tokens", 700)

        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        }

        if response_schema:
            generation_config["responseMimeType"] = "application/json"

        # Safety settings: All 4 OFF per R9
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
            "safetySettings": safety_settings,
        }

        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        if enable_search:
            payload["tools"] = [{"googleSearch": {}}]

        candidate_models = [model_id]
        for fallback in ("gemini-3.5-flash", "gemini-3.1-flash-lite-preview", "gemini-flash-latest", "gemini-3.8-flash"):
            if fallback not in candidate_models:
                candidate_models.append(fallback)

        # Rotation loop
        while True:
            key_bucket = self._select_available_key(priority, estimated_tokens)
            if not key_bucket or key_bucket.key_hash in attempted_keys:
                # All keys in cooldown or unavailable!
                if priority == "P0":
                    logger.warning("DEGRADED: All keys in cooldown/exhausted for P0 request '%s'", prompt_name)
                    return {"is_degraded": True}
                else:
                    logger.info("All keys in cooldown for %s request '%s'", priority, prompt_name)
                    return None

            attempted_keys.add(key_bucket.key_hash)
            now = time.time()
            key_bucket.record_usage(estimated_tokens, now)

            resp = None
            status = 0
            for curr_model in candidate_models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{curr_model}:generateContent?key={key_bucket.api_key}"
                try:
                    resp = await self._client.post(url, json=payload)
                    status = resp.status_code
                    if status not in (404, 503):
                        break
                    logger.warning("Model %s returned %d on %s, trying fallback...", curr_model, status, key_bucket.key_env)
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as net_err:
                    logger.warning("Network error on model %s key %s: %s", curr_model, key_bucket.key_env, net_err)
                    break

            if resp is None:
                continue

            try:

                if status == 200:
                    data = resp.json()

                    # Check for prompt-level block
                    prompt_feedback = data.get("promptFeedback", {})
                    if prompt_feedback.get("blockReason"):
                        logger.warning("Prompt BLOCKED by Gemini filter: %s", prompt_feedback)
                        return {"is_blocked": True}

                    candidates = data.get("candidates", [])
                    if not candidates:
                        logger.warning("Empty candidates returned (potential filter block)")
                        return {"is_blocked": True}

                    candidate = candidates[0]
                    finish_reason = candidate.get("finishReason")
                    if finish_reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
                        logger.warning("Candidate BLOCKED by finishReason: %s", finish_reason)
                        return {"is_blocked": True}

                    content = candidate.get("content", {})
                    parts = content.get("parts", [])
                    if not parts:
                        return {"is_blocked": True}

                    raw_text = parts[0].get("text", "")

                    # Parse JSON schema if expected
                    if response_schema and issubclass(response_schema, BaseModel):
                        try:
                            parsed_json = json.loads(raw_text)
                            return response_schema.model_validate(parsed_json)
                        except Exception:
                            try:
                                cleaned_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
                                parsed_json = json.loads(cleaned_text)
                                return response_schema.model_validate(parsed_json)
                            except Exception as e:
                                logger.error("JSON schema validation failed for '%s': %s (raw: %.100s)", prompt_name, e, raw_text)
                                return None

                    return raw_text

                elif status == 429:
                    # Rate limit error: parse PerMinute vs PerDay
                    resp_body = resp.text
                    is_daily = "PerDay" in resp_body or "quota exceeded per day" in resp_body.lower()
                    error_type = "RATE_DAILY" if is_daily else "RATE_MINUTE"
                    self._mark_key_cooldown(key_bucket, error_type)
                    # Immediate retry with next key in rotation!
                    continue

                elif status in (401, 403):
                    self._mark_key_cooldown(key_bucket, "AUTH_ERROR")
                    continue

                elif status >= 500:
                    logger.warning("Gemini 5xx server error (%d) on key %s", status, key_bucket.key_env)
                    # Retry with next key
                    continue

                else:
                    logger.error("Gemini unexpected error %d: %s", status, resp.text)
                    return None

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as net_err:
                logger.warning("Network error on key %s: %s", key_bucket.key_env, net_err)
                continue
            except Exception as e:
                logger.error("Unexpected exception during Gemini call: %s", e)
                return None
