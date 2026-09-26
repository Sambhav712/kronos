"""
Groq API client for Kronos.

Uses the OpenAI-compatible Groq REST API with dense (non-MoE) models.
Primary model: llama-3.3-70b-versatile (dense 70B, best quality).
Fallback: llama-3.1-8b-instant (dense 8B, fastest).
"""

from core.user_paths import get_user_data_dir
import json
import sys
import time
import base64
import io
import logging
from pathlib import Path
from typing import Optional
import wave

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("groq_client")


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR     = _get_base_dir()
API_KEY_PATH = get_user_data_dir() / "config" / "api_keys.json"


def _load_api_key() -> str:
    try:
        with open(API_KEY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("groq_api_key", "").strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"[Groq] Failed to load API key: {e}")
    import os
    return (os.environ.get("GROQ_API_KEY", "")).strip()



# Dense (non-MoE) models only — ordered by preference
TEXT_MODELS: list[str] = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

VISION_MODELS: list[str] = [
    "qwen/qwen3.8-27b",
]

API_URL               = "https://api.groq.com/openai/v1/chat/completions"
TRANSCRIPTION_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_LANGUAGE          = "hi"
STT_PROMPT            = (
    "यह हिंदी और हिंग्लिश में Brahma Echo के voice commands हैं। "
    "Brahma Echo, Chrome, WhatsApp, Instagram, YouTube, Spotify और Windows "
    "जैसे नाम सही लिखें।"
)
DEFAULT_MAX_TOKENS    = 4096
DEFAULT_TEMPERATURE   = 0.7
REQUEST_TIMEOUT       = 60
MAX_RETRIES_PER_MODEL = 2
RETRY_DELAY           = 2
RATE_LIMIT_COOLDOWN   = 60

_rate_limited: dict[str, float] = {}


class GroqClient:

    def __init__(self) -> None:
        self.api_key  = _load_api_key()
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    def reload_key(self) -> None:
        """Reload API key from disk (e.g. after user edits settings)."""
        self.api_key = _load_api_key()
        self._headers["Authorization"] = f"Bearer {self.api_key}"

    def is_configured(self) -> bool:
        """Return True if a Groq API key is present."""
        return bool(self.api_key)

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """Transcribe a mono int16 PCM utterance with Whisper Large V3 Turbo."""
        if not self.api_key:
            raise PermissionError("[Groq STT] API key is missing. Add a valid gsk_ key in Settings.")
        if not pcm:
            return ""
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(int(sample_rate))
            wav_file.writeframes(pcm)
        response = requests.post(
            TRANSCRIPTION_API_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "json",
                "temperature": "0",
                "language": STT_LANGUAGE,
                "prompt": STT_PROMPT,
            },
            files={"file": ("brahma-voice.wav", wav_bytes.getvalue(), "audio/wav")},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"[Groq STT] HTTP {response.status_code}: {response.text[:200]}")
        return str(response.json().get("text") or "").strip()

    def _is_rate_limited(self, model: str) -> bool:
        ts = _rate_limited.get(model)
        if ts is None:
            return False
        if time.time() - ts > RATE_LIMIT_COOLDOWN:
            del _rate_limited[model]
            return False
        return True

    def _mark_rate_limited(self, model: str) -> None:
        _rate_limited[model] = time.time()
        logger.warning(
            f"[Groq] Rate limited: {model} — "
            f"cooling down for {RATE_LIMIT_COOLDOWN}s"
        )

    def _call(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        payload: dict = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        if not self.api_key:
            raise PermissionError(
                "[Groq] API key is missing. Add a valid gsk_ key in config/api_keys.json."
            )

        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                resp = requests.post(
                    API_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code == 401:
                    raise PermissionError(
                        f"[Groq] Authentication failed for model {model}. "
                        "Check your API key in config/api_keys.json."
                    )

                if resp.status_code == 403:
                    raise PermissionError(
                        f"[Groq] Access denied for model {model} (HTTP 403). "
                        "Check your account permissions."
                    )

                if resp.status_code == 429:
                    self._mark_rate_limited(model)
                    return None

                if resp.status_code == 200:
                    data    = resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                    )
                    return content.strip() if content else None

                logger.warning(
                    f"[Groq] {model} → HTTP {resp.status_code} "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )

            except requests.exceptions.Timeout:
                logger.warning(
                    f"[Groq] {model} → Timeout "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )
            except PermissionError:
                raise
            except Exception as e:
                logger.error(f"[Groq] {model} → Unexpected error: {e}")

            if attempt < MAX_RETRIES_PER_MODEL:
                time.sleep(RETRY_DELAY)

        return None

    def _call_with_fallback(
        self,
        pool: list[str],
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> str:
        if model and not self._is_rate_limited(model):
            try:
                result = self._call(model, messages, max_tokens, temperature, response_format)
                if result:
                    return result
                logger.info(
                    f"[Groq] Requested model failed, "
                    f"falling back to pool: {model}"
                )
            except PermissionError:
                raise

        for m in pool:
            if self._is_rate_limited(m):
                continue
            logger.info(f"[Groq] Trying: {m}")
            result = self._call(m, messages, max_tokens, temperature, response_format)
            if result:
                logger.info(f"[Groq] ✓ Success: {m}")
                return result

        raise RuntimeError(
            "[Groq] All models failed or are rate-limited. "
            "Check your API key and network connection."
        )

    # ── Public API (mirrors OpenRouterClient) ──────────────────────

    def chat(
        self,
        prompt: str,
        system: str = (
            "You are a component of Kronos, an open-source personal assistant. "
            "Be concise, helpful, and precise."
        ),
        history: Optional[list[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        return self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature
        )

    def chat_json(
        self,
        prompt: str,
        system: str = (
            "Return ONLY valid JSON. "
            "No markdown fences, no extra text, no explanation."
        ),
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]
        raw = self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature=0.2,
            response_format={"type": "json_object"},
        )

        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("`").strip()

        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            logger.error(
                f"[Groq] JSON parse failed: {e}\n"
                f"Raw response (first 300 chars): {raw[:300]}"
            )
            raise ValueError(
                f"Model returned unparseable JSON: {e}\n"
                f"Raw output: {raw[:200]}"
            )

    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        ]
        return self._call_with_fallback(
            VISION_MODELS, messages, model, max_tokens, temperature=0.2
        )

    def vision_from_file(
        self,
        prompt: str,
        image_path: str,
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        path = Path(image_path)
        mime_map = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif":  "image/gif",
        }
        mime = mime_map.get(path.suffix.lower(), "image/png")

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        return self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature
        )

    def available_models(self) -> dict:
        return {
            "text_models":   TEXT_MODELS,
            "vision_models": VISION_MODELS,
            "rate_limited":  list(_rate_limited.keys()),
            "total_text":    len(TEXT_MODELS),
            "total_vision":  len(VISION_MODELS),
        }


client = GroqClient()


if __name__ == "__main__":
    print("=" * 55)
    print("  Brahma Echo — Groq Client Self-Test")
    print("=" * 55)

    print("\n[TEST 1] Basic chat...")
    try:
        reply = client.chat("Introduce yourself in one sentence.")
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 2] JSON mode...")
    try:
        data = client.chat_json(
            'List 3 programming languages. Format: {"languages": ["a", "b", "c"]}',
            system="Return only valid JSON. No extra text."
        )
        print(f"  Response : {data}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 3] Multi-turn conversation...")
    try:
        history = [
            {"role": "system",    "content": "You are a helpful assistant. Be brief."},
            {"role": "user",      "content": "My name is User."},
            {"role": "assistant", "content": "Hello User, how can I help you?"},
            {"role": "user",      "content": "What is my name?"},
        ]
        reply = client.multi_turn(history)
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 4] Model pool info...")
    info = client.available_models()
    print(f"  Text models   : {info['total_text']}")
    print(f"  Vision models : {info['total_vision']}")
    print(f"  Rate limited  : {info['rate_limited'] or 'none'}")
    print(f"  Status        : PASS ✓")

    print("\n" + "=" * 55)
    print("  All tests complete.")
    print("=" * 55)
