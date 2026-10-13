import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from src.ops.config import BotConfig
from src.ops.log import logger
from src.db.manager import DatabaseManager
from src.db.contracts import Gateway
from src.gateway.schemas import MediaAnalysis, MediaKind
from src.context.prompts import PromptBuilder


SCORE_PATTERN = re.compile(r"\b(\d{1,3})/100\b")


class MediaAnalyzer:
    """Manages media normalization with ffmpeg, inline base64 calls to Gemini, caching, and score verification."""

    def __init__(
        self,
        bot_config: BotConfig,
        db_manager: DatabaseManager,
        gateway: Gateway,
        prompt_builder: PromptBuilder,
        tmp_dir: str = "/tmp"
    ):
        self.config = bot_config
        self.db = db_manager
        self.gateway = gateway
        self.prompts = prompt_builder
        self.tmp_dir = Path(tmp_dir)

    def _cleanup_old_tmp_files(self, max_age_hours: int = 1) -> None:
        """Sweeps temp files older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        try:
            for p in self.tmp_dir.glob("smesh-*"):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Error cleaning temp files: %s", e)

    def convert_media_if_needed(self, input_path: Path, kind: str) -> Path:
        """
        Converts/compresses media with ffmpeg:
        - audio/voice -> mp3
        - video/video_note -> mp4 (first 60s)
        - compress <= 10MB if file is large
        """
        self._cleanup_old_tmp_files()
        out_path = self.tmp_dir / f"smesh-{input_path.stem}.converted"

        if kind in ("voice", "audio"):
            out_path = out_path.with_suffix(".mp3")
            cmd = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-ac", "1", "-b:a", "64k",
                str(out_path)
            ]
        elif kind in ("video", "video_note"):
            out_path = out_path.with_suffix(".mp4")
            cmd = [
                "ffmpeg", "-y", "-t", "60", "-i", str(input_path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                "-c:a", "aac", "-b:a", "64k",
                str(out_path)
            ]
        else:
            return input_path

        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if out_path.exists():
                return out_path
        except Exception as e:
            logger.warning("ffmpeg conversion failed, using original file: %s", e)

        return input_path

    async def analyze_media_step1(
        self,
        file_path: Path,
        kind: str,
        file_unique_id: str,
        caption: Optional[str] = None,
        priority: str = "P0"
    ) -> Optional[dict[str, Any]]:
        """
        Step 1: Check cache -> ffmpeg convert -> base64 inline call -> cache result in media_cache.
        """
        # 1. Check cache first
        cached = await self.db.get_media_cache(file_unique_id)
        if cached:
            logger.info("Media %s served from cache", file_unique_id)
            return cached

        # 2. Convert if needed
        converted_path = self.convert_media_if_needed(file_path, kind)

        # 3. Read file into inline base64
        try:
            with open(converted_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error("Failed to read media file for inline base64: %s", e)
            return None
        finally:
            # Clean converted tmp if separate
            if converted_path != file_path and converted_path.exists():
                converted_path.unlink(missing_ok=True)

        mime_type_map = {
            "voice": "audio/mp3",
            "audio": "audio/mp3",
            "video": "video/mp4",
            "video_note": "video/mp4",
            "photo": "image/jpeg",
            "animation": "video/mp4",
        }
        mime_type = mime_type_map.get(kind, "application/octet-stream")

        system_instruction = self.prompts.build_system_instruction(
            mode_or_modular="media_analyze",
            bot_name="Смешнявка"
        )

        parts: list[dict[str, Any]] = [
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": b64_data
                }
            }
        ]
        if caption:
            parts.append({"text": f"<caption>{caption}</caption>"})
        parts.append({"text": f'<request kind="{kind}"/>'})

        contents = [{"role": "user", "parts": parts}]

        analysis_obj: Optional[MediaAnalysis] = await self.gateway.generate(
            prompt_name="media_analyze",
            contents=contents,
            system_instruction=system_instruction,
            priority=priority,
            response_schema=MediaAnalysis,
            estimated_tokens=1500
        )

        if not analysis_obj:
            logger.warning("media_analyze call failed for file %s", file_unique_id)
            return None

        # 4. Save to SQLite media_cache
        item_dict = {
            "file_unique_id": file_unique_id,
            "kind": kind,
            "transcript": analysis_obj.transcript,
            "ocr_text": analysis_obj.ocr_text,
            "summary": analysis_obj.summary,
            "track_meta": json.dumps(analysis_obj.track_meta.model_dump(), ensure_ascii=False) if analysis_obj.track_meta else None,
            "tags": json.dumps(analysis_obj.tags, ensure_ascii=False),
            "verdict": analysis_obj.verdict,
            "score100": analysis_obj.score100,
            "score_reason": analysis_obj.score_reason,
            "unintelligible": 1 if analysis_obj.unintelligible else 0,
            "injection_suspected": 1 if analysis_obj.injection_suspected else 0,
        }

        await self.db.save_media_cache(item_dict, max_items=self.config.MEDIA_CACHE_MAX)
        return item_dict

    def verify_and_patch_reply_score(self, reply_text: str, media_cache_item: dict[str, Any]) -> str:
        """
        Step 2 post-processing per R3/TB-07:
        Checks that reply has N/100 matching the cache. If missing and score100 is present
        (and unintelligible/injection_suspected are False), appends N/100 + reason.
        """
        score100 = media_cache_item.get("score100")
        unintelligible = bool(media_cache_item.get("unintelligible"))
        injection = bool(media_cache_item.get("injection_suspected"))

        # If unintelligible or injection, no score
        if unintelligible or injection or score100 is None:
            # Strip any hallucinated N/100 score
            return SCORE_PATTERN.sub("", reply_text).strip()

        expected_score_str = f"{score100}/100"
        match = SCORE_PATTERN.search(reply_text)
        if match:
            # Check if number matches
            found_str = match.group(0)
            if found_str != expected_score_str:
                # Replace with accurate score
                reply_text = reply_text[:match.start()] + expected_score_str + reply_text[match.end():]
            return reply_text

        # Score missing entirely: append it
        reason = media_cache_item.get("score_reason") or media_cache_item.get("verdict") or ""
        reason_part = f" — {reason}" if reason else ""
        return f"{reply_text}\n{expected_score_str}{reason_part}".strip()
