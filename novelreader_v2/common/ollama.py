from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loguru import logger


class OllamaStructuredOutputError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("empty LLM response")

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    candidates: list[str] = []
    for left, right in (("{", "}"), ("[", "]")):
        start = text.find(left)
        end = text.rfind(right)
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError(f"failed to extract JSON from response preview={text[:500]!r}")


def _request_json(url: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def ollama_generate_json(
    *,
    url: str,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    temperature: float,
    num_predict: int,
    keep_alive: str,
    timeout_seconds: int,
    num_ctx: int | None = None,
    task_name: str = "ollama",
    max_retries: int = 1,
) -> Any:
    options: dict[str, Any] = {"temperature": temperature, "num_predict": num_predict}
    if num_ctx:
        options["num_ctx"] = num_ctx

    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "format": schema,
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": options,
    }

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 2):
        started = time.perf_counter()
        try:
            result = _request_json(url, payload, timeout_seconds)
            elapsed = time.perf_counter() - started
            done_reason = result.get("done_reason")
            response_text = result.get("response") or ""
            logger.debug(
                "Ollama {} attempt {} finished in {:.2f}s, prompt_tokens={}, eval_tokens={}, done_reason={}",
                task_name,
                attempt,
                elapsed,
                result.get("prompt_eval_count"),
                result.get("eval_count"),
                done_reason,
            )
            if result.get("done") is not True:
                raise OllamaStructuredOutputError(f"{task_name}: response did not finish")
            if done_reason == "length":
                raise OllamaStructuredOutputError(
                    f"{task_name}: response truncated at num_predict={num_predict}"
                )
            if not response_text.strip():
                raise OllamaStructuredOutputError(f"{task_name}: empty response")
            return extract_json(response_text)
        except (HTTPError, URLError, TimeoutError, ValueError, OllamaStructuredOutputError) as exc:
            last_error = exc
            logger.warning("{} attempt {} failed: {}", task_name, attempt, exc)
            if attempt <= max_retries:
                time.sleep(min(2 * attempt, 5))
    raise OllamaStructuredOutputError(f"{task_name} failed after retries: {last_error}") from last_error

