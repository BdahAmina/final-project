"""Structured-output client for the local Ollama server (the only model provider).

Responses are constrained with JSON Schema and validated with Pydantic.
A valid structure does not prove factual accuracy; see validators.py.
Logs contain only provider, model, timing and error categories.
"""
import json
import logging
import os
import time
import requests
from pydantic import ValidationError

LOG = logging.getLogger(__name__)
MAX_ATTEMPTS = 2


class LLMError(ValueError):
    pass


class OllamaClient:
    provider = "ollama"
    external = False

    def __init__(self, model=None, base_url=None, timeout=900, num_ctx=16384, num_predict=None):
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen3:8b")
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict  # hard cap on generated tokens (None = model default)
        self.keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "30m")  # keep the model loaded between requests

    def warm(self):
        """Load the model into memory ahead of the first request (sends no patient data). Returns True if loaded."""
        try:
            r = requests.post(self.base_url + "/api/generate", json={"model": self.model, "prompt": "", "keep_alive": self.keep_alive},
                              timeout=(3, 300))
            return r.status_code == 200
        except requests.RequestException:
            return False

    def status(self):
        """Return (ok, message) without sending any transcript content."""
        try:
            r = requests.get(self.base_url + "/api/tags", timeout=3)
            names = {m.get("name") for m in r.json().get("models", [])}
        except (requests.RequestException, ValueError):
            return False, f"שרת Ollama אינו פועל בכתובת {self.base_url}. התקינו את Ollama והפעילו אותו (ollama serve)."
        if self.model not in names and self.model + ":latest" not in names:
            return False, f"המודל {self.model} לא הורד. הריצו: ollama pull {self.model}"
        return True, f"Ollama מקומי, מודל {self.model}"

    def _call(self, system, user, schema):
        options = {"temperature": 0, "num_ctx": self.num_ctx}
        if self.num_predict:
            options["num_predict"] = self.num_predict
        body = {"model": self.model, "stream": False, "think": False, "format": schema, "keep_alive": self.keep_alive,
                "options": options,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = requests.post(self.base_url + "/api/chat", json=body, timeout=(5, self.timeout))
        except requests.Timeout:
            raise LLMError("המודל המקומי לא הגיב בזמן. נסו תמלול קצר יותר או מודל קטן יותר.") from None
        except requests.RequestException:
            raise LLMError("לא ניתן להתחבר ל־Ollama המקומי. ודאו שהוא פועל (ollama serve).") from None
        if r.status_code == 404:
            raise LLMError(f"המודל {self.model} אינו זמין ב־Ollama. הריצו: ollama pull {self.model}")
        if r.status_code != 200:
            raise LLMError(f"Ollama החזיר שגיאה (HTTP {r.status_code}).")
        data = r.json()
        if data.get("done_reason") not in (None, "stop"):
            raise LLMError("תשובת המודל נקטעה. קצרו את התמלול ונסו שוב.")
        return data["message"]["content"]


def structured(client, system, user, model_cls):
    """Call the model and parse into `model_cls`; bounded retry on schema violations only."""
    schema = model_cls.model_json_schema()
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            content = client._call(system, user, schema)
            result = model_cls.model_validate(json.loads(content))
            LOG.info("LLM ok: provider=%s model=%s attempt=%d seconds=%.1f", client.provider, client.model, attempt, time.monotonic() - started)
            return result
        except (json.JSONDecodeError, ValidationError, KeyError, IndexError, TypeError) as exc:
            last = exc
            LOG.warning("LLM schema violation: provider=%s model=%s attempt=%d error=%s", client.provider, client.model, attempt, type(exc).__name__)
    raise LLMError(f"תשובת המודל אינה תואמת למבנה הנדרש גם אחרי {MAX_ATTEMPTS} ניסיונות ({type(last).__name__}). לא יוצג פלט.")
