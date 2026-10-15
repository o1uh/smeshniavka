import asyncio
import copy
import json
import os
import re
import time
from typing import Any, Optional, Union
import httpx
from pydantic import BaseModel

from src.ops.config import KeyConfig, AppConfig
from src.ops.log import logger
from src.db.contracts import Gateway
from src.gateway.schemas import PersonaTextReply


class KeyBucket:
    """
    Backward-compatibility stub for legacy KeyBucket tracking.
    With Antigravity Fast Bridge, individual API keys and quotas are replaced by the bridge.
    """

    def __init__(self, key_config: Optional[KeyConfig] = None):
        self.key_env = getattr(key_config, "key_env", "ANTIGRAVITY_BRIDGE")
        self.api_key = getattr(key_config, "api_key", "bridge_key")
        self.rpm_limit = getattr(key_config, "rpm_limit", 120)
        self.rpd_limit = getattr(key_config, "rpd_limit", 50000)
        self.tpm_limit = getattr(key_config, "tpm_limit", 10000000)
        self.project = getattr(key_config, "project", "antigravity") or "antigravity"
        self.reserve = getattr(key_config, "reserve", False)

        self.key_hash = "bridge"
        self.is_dead = False
        self.cooldown_until: float = 0.0
        self.rpd_used: int = 0

    def is_in_cooldown(self, now: float = 0.0) -> bool:
        return self.is_dead or (self.cooldown_until > now)

    def current_rpm(self, now: float = 0.0) -> int:
        return 0

    def current_tpm(self, now: float = 0.0) -> int:
        return 0


def clean_schema_for_gemini(raw_schema: dict) -> dict:
    """
    Cleans Pydantic JSON schema:
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


def convert_contents_to_messages(
    contents: list[dict[str, Any]],
    system_instruction: Optional[str] = None
) -> list[dict[str, Any]]:
    """
    Converts Gemini-style contents and system instruction into OpenAI-compatible chat messages
    understood by the Antigravity Fast Bridge.
    """
    messages: list[dict[str, Any]] = []

    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    for item in contents:
        role = "assistant" if item.get("role") in ("model", "assistant") else "user"
        parts = item.get("parts")

        if parts is not None:
            text_blocks: list[str] = []
            structured_blocks: list[dict[str, Any]] = []
            has_media = False

            for part in parts:
                if isinstance(part, dict):
                    if "text" in part:
                        t = part["text"]
                        text_blocks.append(t)
                        structured_blocks.append({"type": "text", "text": t})
                    elif "inline_data" in part:
                        has_media = True
                        inline = part["inline_data"]
                        mime = inline.get("mime_type", "")
                        data = inline.get("data", "")
                        data_url = f"data:{mime};base64,{data}"
                        if mime.startswith("image/"):
                            structured_blocks.append({
                                "type": "image_url",
                                "image_url": {"url": data_url}
                            })
                        elif mime.startswith("audio/"):
                            structured_blocks.append({
                                "type": "audio_url",
                                "audio_url": {"url": data_url}
                            })
                        elif mime.startswith("video/"):
                            structured_blocks.append({
                                "type": "video_url",
                                "video_url": {"url": data_url}
                            })
                        else:
                            structured_blocks.append({
                                "type": "media_url",
                                "media_url": {"url": data_url}
                            })
                elif isinstance(part, str):
                    text_blocks.append(part)
                    structured_blocks.append({"type": "text", "text": part})

            if has_media:
                messages.append({"role": role, "content": structured_blocks})
            else:
                messages.append({"role": role, "content": "\n".join(text_blocks)})

        elif "content" in item:
            messages.append({"role": role, "content": item["content"]})

    return messages


class AntigravityGateway(Gateway):
    """
    Gateway connecting to Antigravity Fast Bridge on Raspberry Pi (/opt/bridge).
    Replaces multi-key rotation and direct Gemini API rate limits with the
    high-performance local bridge.
    - OpenAI-compatible chat completions interface
    - Persistent session support for low latency (<2s per turn)
    - Concurrency control (MAX_CONCURRENT=4, P0 reserve, P2 slot limits)
    - Automatic URL discovery (localhost vs rpi.ln)
    - Full support for Pydantic response schemas with automatic repair
    """

    def __init__(self, app_config: AppConfig, http_client: Optional[httpx.AsyncClient] = None):
        self.config = app_config
        self.bot_config = app_config.bot
        configured_url = getattr(self.bot_config, "BRIDGE_URL", None) or os.getenv("BRIDGE_URL", "http://127.0.0.1:8000")
        self.bridge_url = configured_url.rstrip("/")
        self._client = http_client or httpx.AsyncClient(timeout=90.0)

        self._active_url: Optional[str] = None
        self._url_lock = asyncio.Lock()

        # Concurrency & priorities (global + per-lane so media can't eat chat)
        self.max_concurrent = self.bot_config.MAX_CONCURRENT
        self.chat_max = getattr(self.bot_config, "CHAT_MAX_CONCURRENT", 2)
        self.track_max = getattr(self.bot_config, "TRACK_MAX_CONCURRENT", 1)
        self.media_max = getattr(self.bot_config, "MEDIA_MAX_CONCURRENT", 1)
        self.sys_max = getattr(self.bot_config, "SYS_MAX_CONCURRENT", 1)
        self._current_total = 0
        self._current_p2 = 0
        self._lane_counts: dict[str, int] = {"chat": 0, "track": 0, "media": 0, "sys": 0}
        self._lock = asyncio.Lock()

        # Monitoring & status metrics
        self.is_healthy = True
        self.requests_count = 0
        self.total_tokens_used = 0
        self.last_error: Optional[str] = None
        self.down_since: Optional[float] = None
        self.alert_callback = None

        # Compatibility stub for keys
        self.keys = [KeyBucket()]

    async def _resolve_bridge_url(self) -> str:
        """
        Determines reachable bridge URL. If default http://127.0.0.1:8000 fails to connect,
        probes http://rpi.ln:8000 (LAN hostname of Raspberry Pi).
        """
        if self._active_url:
            return self._active_url

        async with self._url_lock:
            if self._active_url:
                return self._active_url

            # If user explicitly configured a non-default URL, use it directly
            if self.bridge_url != "http://127.0.0.1:8000":
                self._active_url = self.bridge_url
                return self._active_url

            # Try default localhost
            try:
                resp = await self._client.get(f"{self.bridge_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    self._active_url = self.bridge_url
                    return self._active_url
            except Exception:
                pass

            # Fallback probe to rpi.ln (Raspberry Pi on local network)
            lan_url = "http://rpi.ln:8000"
            try:
                resp = await self._client.get(f"{lan_url}/health", timeout=2.5)
                if resp.status_code == 200:
                    logger.info("Antigravity Bridge discovered at %s", lan_url)
                    self._active_url = lan_url
                    return self._active_url
            except Exception:
                pass

            # Default to configured URL
            self._active_url = self.bridge_url
            return self._active_url

    def _resolve_model(self, model_id: str) -> str:
        """Maps legacy Gemini model identifiers to bridge models/aliases."""
        if not model_id:
            return "fast"
        m = model_id.strip()
        if m in ("gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"):
            return "fast"
        return m

    def get_key_status_summary(self) -> dict[str, Any]:
        """Provides status summary compatible with /alive and /status bot commands."""
        now = time.time()
        active_cnt = 1 if self.is_healthy else 0
        in_cooldown_cnt = 0 if self.is_healthy else 1

        return {
            "total_keys": 1,
            "active_keys": active_cnt,
            "reserve_keys": 0,
            "in_cooldown": in_cooldown_cnt,
            "dead_keys": in_cooldown_cnt,
            "total_rpd_cap": 999999,
            "total_rpd_used": self.requests_count,
            "is_degraded": not self.is_healthy,
            "bridge_url": self._active_url or self.bridge_url,
            "bridge_status": "ok" if self.is_healthy else "down",
            "last_error": self.last_error,
        }

    async def reset_session(self, session_id: str) -> bool:
        """Resets persistent session worker in Antigravity Bridge."""
        url = await self._resolve_bridge_url()
        try:
            resp = await self._client.post(
                f"{url}/v1/sessions/reset",
                json={"session_id": session_id},
                timeout=5.0
            )
            return resp.status_code == 200
        except Exception as e:
            logger.warning("Failed to reset session %s on bridge: %s", session_id, e)
            return False

    @staticmethod
    def lane_for_prompt(prompt_name: str, lane: Optional[str] = None) -> str:
        """Маппинг промпта на воркер-лейн. Явный lane всегда побеждает."""
        if lane in ("chat", "track", "media", "sys"):
            return lane
        if prompt_name in ("persona_media_reply", "media_analyze"):
            # без явного lane не угадаешь трек vs остальное — по дефолту media,
            # трек-коллеры передают lane="track" явно
            return "media"
        if prompt_name in ("dossier_extract", "dossier_consolidate", "summary_chunk", "summary_merge", "style_filter"):
            return "sys"
        return "chat"

    def _lane_limit(self, lane: str) -> int:
        if lane == "track":
            return self.track_max
        if lane == "media":
            return self.media_max
        if lane == "sys":
            return self.sys_max
        return self.chat_max

    def _lane_timeout(self, lane: str) -> float:
        if lane == "track":
            return float(getattr(self.bot_config, "TRACK_TIMEOUT_S", 50.0))
        if lane == "media":
            return float(getattr(self.bot_config, "MEDIA_TIMEOUT_S", 75.0))
        if lane == "sys":
            return float(getattr(self.bot_config, "SYS_TIMEOUT_S", 75.0))
        return float(getattr(self.bot_config, "CHAT_TIMEOUT_S", 35.0))

    async def _acquire_slot(self, priority: str, lane: str = "chat") -> bool:
        if lane not in self._lane_counts:
            lane = "chat"
        lane_limit = self._lane_limit(lane)
        while True:
            async with self._lock:
                available_slots = self.max_concurrent - self._current_total
                lane_used = self._lane_counts.get(lane, 0)
                if lane_used >= lane_limit:
                    pass
                elif priority == "P0":
                    if available_slots > 0:
                        self._current_total += 1
                        self._lane_counts[lane] = lane_used + 1
                        return True
                elif priority == "P1":
                    if available_slots > 1:
                        self._current_total += 1
                        self._lane_counts[lane] = lane_used + 1
                        return True
                elif priority == "P2":
                    if available_slots > 1 and self._current_p2 < 1:
                        self._current_total += 1
                        self._current_p2 += 1
                        self._lane_counts[lane] = lane_used + 1
                        return True
            await asyncio.sleep(0.05)

    async def _release_slot(self, priority: str, lane: str = "chat") -> None:
        async with self._lock:
            self._current_total = max(0, self._current_total - 1)
            if lane in self._lane_counts:
                self._lane_counts[lane] = max(0, self._lane_counts[lane] - 1)
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
        enable_search: bool = False,
        search_query: Optional[str] = None,
        session_id: Optional[str] = None,
        lane: Optional[str] = None,
    ) -> Any:
        """
        Executes generation via Antigravity Fast Bridge.
        lane: chat|track|media — явный воркер. Если не задан, выводится из prompt_name.
        """
        resolved_lane = self.lane_for_prompt(prompt_name, lane)
        timeout = self._lane_timeout(resolved_lane)
        await self._acquire_slot(priority, resolved_lane)
        try:
            return await asyncio.wait_for(
                self._execute(
                    prompt_name=prompt_name,
                    contents=contents,
                    system_instruction=system_instruction,
                    priority=priority,
                    response_schema=response_schema,
                    estimated_tokens=estimated_tokens,
                    enable_search=enable_search,
                    search_query=search_query,
                    session_id=session_id
                ),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error("Bridge call '%s' timed out after %ss (lane=%s)", prompt_name, timeout, resolved_lane)
            self._mark_failure("TIMEOUT")
            if priority == "P0":
                return {"is_degraded": True}
            return None
        finally:
            await self._release_slot(priority, resolved_lane)

    def _mark_failure(self, error_desc: str) -> None:
        self.is_healthy = False
        self.last_error = error_desc
        if not self.down_since:
            self.down_since = time.time()
        logger.warning("Antigravity Bridge marked unhealthy: %s", error_desc)

    def _mark_success(self) -> None:
        self.is_healthy = True
        self.down_since = None
        self.last_error = None

    async def _execute(
        self,
        prompt_name: str,
        contents: list[dict[str, Any]],
        system_instruction: Optional[str],
        priority: str,
        response_schema: Optional[type],
        estimated_tokens: int,
        enable_search: bool,
        search_query: Optional[str],
        session_id: Optional[str]
    ) -> Any:
        gen_params = self.config.gen.get(prompt_name, {})
        temperature = gen_params.get("temperature", 0.7)
        max_output_tokens = gen_params.get("max_output_tokens", 700)

        # Convert contents to OpenAI chat completion format
        messages = convert_contents_to_messages(contents, system_instruction)

        search_directive = None
        if search_query:
            search_directive = (
                f"\n[ОБЯЗАТЕЛЬНО вызови инструмент search_web с запросом: \"{search_query}\". "
                "Изучи реальные факты/новости/события из результатов поиска и используй их в ответе в чат!]"
            )
        elif enable_search:
            search_directive = (
                "\n[Инструкция по веб-поиску: тебе доступен инструмент search_web. "
                "Сам думай, когда нужен поиск: если в вопросе, реплике или меме есть непонятные отсылки, "
                "упоминаются города, персоны, события («в чем мем», «при чем тут...», происшествия, новости) — "
                "вызывай search_web, чтобы узнать реальный контекст и факты, а затем едко ответь в чат]"
            )

        if search_directive:
            found_system = False
            for m in messages:
                if m.get("role") == "system":
                    m["content"] = str(m.get("content", "")) + "\n" + search_directive
                    found_system = True
                    break
            if not found_system:
                messages.insert(0, {"role": "system", "content": search_directive.strip()})

            # Дублировать в user-блок — только обязательный запрос с query:
            # иначе жрём токены и внимание на каждом пинге.
            if search_query:
                for m in reversed(messages):
                    if m.get("role") == "user":
                        if isinstance(m.get("content"), str):
                            m["content"] = m["content"] + f"\n\n{search_directive}"
                        elif isinstance(m.get("content"), list):
                            m["content"].append({"type": "text", "text": f"\n\n{search_directive}"})
                        break

        target_model = self._resolve_model(self.bot_config.MODEL_ID)

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "stream": False,
        }

        if session_id:
            payload["user"] = session_id

        bridge_url = await self._resolve_bridge_url()
        endpoint = f"{bridge_url}/v1/chat/completions"

        try:
            resp = await self._client.post(endpoint, json=payload, timeout=60.0)
            status = resp.status_code

            if status == 200:
                self._mark_success()
                self.requests_count += 1

                data = resp.json()
                usage = data.get("usage", {})
                self.total_tokens_used += usage.get("total_tokens", 0)

                choices = data.get("choices", [])
                if not choices:
                    logger.warning("Empty choices returned from Antigravity Bridge")
                    return {"is_blocked": True}

                choice = choices[0]
                message_obj = choice.get("message", {})
                raw_text = message_obj.get("content", "")

                is_safety_blocked = choice.get("finish_reason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST")
                if not is_safety_blocked and raw_text:
                    lower_raw = raw_text.lower()
                    if (
                        "sensitive words" in lower_raw
                        or "prohibited use policy" in lower_raw
                        or "prompt could not be submitted" in lower_raw
                        or "violate google's" in lower_raw
                        or "violate generative ai" in lower_raw
                    ):
                        is_safety_blocked = True

                if is_safety_blocked:
                    logger.warning("Generation blocked by safety filter: %.120s", raw_text)
                    if session_id:
                        asyncio.create_task(self.reset_session(session_id))
                    return {"is_blocked": True}

                # Parse JSON schema if expected
                if response_schema and issubclass(response_schema, BaseModel):
                    try:
                        parsed_json = json.loads(raw_text, strict=False)
                        return response_schema.model_validate(parsed_json)
                    except Exception:
                        pass

                    try:
                        cleaned_text = re.sub(
                            r"^```(?:json)?\s*|\s*```$",
                            "",
                            raw_text.strip(),
                            flags=re.MULTILINE
                        )
                        parsed_json = json.loads(cleaned_text, strict=False)
                        return response_schema.model_validate(parsed_json)
                    except Exception:
                        pass

                    # Robust extraction for PersonaTextReply (e.g. unclosed trailing quotes)
                    if response_schema is PersonaTextReply:
                        m = re.search(r'"reply"\s*:\s*"(.*?)(?:"\s*\}|\}\s*$|$)', raw_text, re.DOTALL)
                        if m:
                            val = m.group(1).rstrip('"\n }')
                            return PersonaTextReply(reply=val)

                    # Try regex pattern matching first JSON object {...}
                    m_json = re.search(r"(\{.*\})", raw_text, re.DOTALL)
                    if m_json:
                        try:
                            parsed_json = json.loads(m_json.group(1), strict=False)
                            return response_schema.model_validate(parsed_json)
                        except Exception:
                            pass

                    # Try repairing unclosed trailing quote before }
                    if raw_text.rstrip().endswith("}") and not raw_text.rstrip().endswith('"}'):
                        try:
                            cand = raw_text.rstrip()[:-1] + '"}'
                            parsed_json = json.loads(cand, strict=False)
                            return response_schema.model_validate(parsed_json)
                        except Exception:
                            pass

                    logger.error(
                        "JSON schema validation failed for '%s': (raw: %.100s)",
                        prompt_name, raw_text
                    )
                    return None

                return raw_text

            else:
                err_msg = f"HTTP {status}: {resp.text[:200]}"
                logger.warning("Antigravity Bridge returned error: %s", err_msg)
                self._mark_failure(err_msg)
                if priority == "P0":
                    return {"is_degraded": True}
                return None

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.TimeoutException) as net_err:
            err_msg = f"Network error connecting to bridge ({bridge_url}): {net_err}"
            logger.warning(err_msg)
            self._mark_failure(err_msg)
            if priority == "P0":
                return {"is_degraded": True}
            return None
        except Exception as e:
            logger.error("Unexpected exception during bridge call: %s", e)
            self._mark_failure(str(e))
            if priority == "P0":
                return {"is_degraded": True}
            return None


# Backward-compatible alias
GeminiGateway = AntigravityGateway
