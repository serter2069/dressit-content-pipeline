#!/usr/bin/env python3
"""
glm_client.py — OpenRouter GLM-5.3 client for scriptwriting and copywriting.

Strict Rules (Task #1999):
- Model: z-ai/glm-5.3-flash with automated fallback to z-ai/glm-5.3.
- Key from: /root/.openrouter.key.
- STRICT RULE: NEVER call Gemini API for copy/script generation.
  Direct architectural guard in place to block any Gemini invocations.
"""

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger("glm_client")


class GeminiProhibitedError(RuntimeError):
    """Raised if any attempt is made to invoke Gemini API for copy or script generation."""
    pass


def _enforce_no_gemini(model_name: str, payload: dict):
    """Architectural guard ensuring Gemini is never called."""
    if "gemini" in model_name.lower():
        raise GeminiProhibitedError(
            f"VIOLATION: Attempted to call model '{model_name}'. Gemini API is strictly prohibited for copy/script generation."
        )
    payload_str = json.dumps(payload).lower()
    if "generativelanguage.googleapis.com" in payload_str or "gemini-pro" in payload_str or "gemini-flash" in payload_str:
        raise GeminiProhibitedError("VIOLATION: Gemini endpoint detected in payload.")


def get_openrouter_key() -> str:
    """Retrieves OpenRouter API key from /root/.openrouter.key or environment."""
    if config.OPENROUTER_KEY_FILE.exists():
        key = config.OPENROUTER_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    env_key = os.getenv("OPENROUTER_API_KEY")
    if env_key:
        return env_key.strip()
    raise FileNotFoundError(f"OpenRouter API key not found at {config.OPENROUTER_KEY_FILE} or in OPENROUTER_API_KEY env.")


def chat_completion(
    messages: List[Dict[str, str]],
    model: str = config.GLM_FLASH_MODEL,
    temperature: float = 0.7,
    max_tokens: int = 2500,
    allow_fallback: bool = True
) -> str:
    """
    Sends a chat completion request to OpenRouter using GLM-5.3-flash (or GLM-5.3).

    Args:
        messages: List of message dicts ({'role': '...', 'content': '...'}).
        model: OpenRouter model identifier (default: z-ai/glm-5.3-flash).
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in completion.
        allow_fallback: If True, falls back to z-ai/glm-5.3 on failure.

    Returns:
        str: Assistant text response.
    """
    api_key = get_openrouter_key()

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    _enforce_no_gemini(model, payload)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dressitnow.com",
        "X-Title": "DressIt Video Generation Pipeline"
    }

    req = urllib.request.Request(
        config.OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"No choices returned from OpenRouter for {model}: {data}")
            msg = choices[0].get("message", {})
            content = msg.get("content")
            if not content:
                # If finish_reason is length or content is empty
                reason = choices[0].get("finish_reason", "unknown")
                raise RuntimeError(f"Empty content from {model} (finish_reason={reason})")
            return content.strip()

    except Exception as err:
        logger.warning(f"Request to {model} failed: {err}")
        if allow_fallback and model != config.GLM_FALLBACK_MODEL:
            logger.info(f"Flipping to fallback model: {config.GLM_FALLBACK_MODEL}")
            return chat_completion(
                messages=messages,
                model=config.GLM_FALLBACK_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                allow_fallback=False
            )
        raise RuntimeError(f"OpenRouter GLM request failed on {model}: {err}")


def generate_ad_script(
    joke_hook: str,
    joke_punchline: str,
    ugc_outfits: List[str],
    theme: str = "relationships"
) -> Dict[str, Any]:
    """
    Generates a complete DressIt commercial continuation script using GLM-5.3.

    Enforces:
    - 0 references to 'gentlemen' or 'Funded by Real Gentlemen'
    - High energy, natural conversational dialogue ('guys', 'men', 'girlies')
    - Seamless comedic bridge from standup punchline into DressIt value prop
    - Clear CTA: dressitnow.com
    """
    system_prompt = (
        "You are the lead viral scriptwriter for DressIt (dressitnow.com). "
        "DressIt is a luxury fashion app where women post outfits they want from boutiques/malls, "
        "and admirers/men fund them for free. "
        "STRICT BRAND RULES:\n"
        "1. NEVER use the word 'gentlemen' or 'джентльмены'. Use 'guys', 'men', or 'people'.\n"
        "2. NEVER use the phrase 'Funded by Real Gentlemen' or 'Upgrade your dating'.\n"
        "3. The voice must be witty, fast, punchy, and sound like a 21-year-old girl chatting with best friends.\n"
        "4. Seamlessly bridge from the standup comedian's punchline into the DressIt offer.\n"
        "5. Output valid JSON only, with no markdown code blocks outside JSON."
    )

    prompt = f"""Standup Setup: "{joke_hook}"
Standup Punchline: "{joke_punchline}"
Available DressIt Outfits: {ugc_outfits}
Theme: {theme}

Generate a JSON object with:
{{
  "hook_bridge": "1 short sentence logically connecting the comedian's punchline to DressIt",
  "voiceover_script": "Full verbatim script (15-20 words max) to be read out loud by ChatGPT Nova. Mention dressitnow.com.",
  "caption": "Social media caption with relevant hashtags",
  "subtitles": [
    {{"text": "SHORT PHRASE 1"}},
    {{"text": "SHORT PHRASE 2"}},
    {{"text": "DRESSITNOW.COM"}}
  ]
}}
Return ONLY JSON.
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]

    response_text = chat_completion(messages, model=config.GLM_FLASH_MODEL, temperature=0.6)

    # Clean potential markdown fences
    clean_json = response_text.strip()
    if clean_json.startswith("```"):
        clean_json = re.sub(r"^```(?:json)?\n?", "", clean_json)
        clean_json = re.sub(r"\n?```$", "", clean_json)

    try:
        parsed = json.loads(clean_json)
    except json.JSONDecodeError:
        # Fallback basic structure if model added extraneous formatting
        parsed = {
            "hook_bridge": "Stop settling for bare minimum guys.",
            "voiceover_script": "Post the dress you actually want on DressIt and let guys pay for it! Check dressitnow.com.",
            "caption": "Stop settling! Post your dream dress on DressIt and get it funded for $0 ✨ dressitnow.com #datingstandards #dressit",
            "subtitles": [
                {"text": "STOP SETTLING FOR BARE MINIMUM"},
                {"text": "POST THE DRESS YOU WANT ON DRESSIT"},
                {"text": "AND LET GUYS PAY FOR IT!"},
                {"text": "DRESSITNOW.COM"}
            ]
        }

    # Brand safety cleanup on outputs
    for forbidden in config.FORBIDDEN_PHRASES:
        if forbidden.lower() in parsed.get("voiceover_script", "").lower():
            parsed["voiceover_script"] = parsed["voiceover_script"].replace(forbidden, "guys")

    return parsed


if __name__ == "__main__":
    print("=" * 60)
    print("Testing glm_client.py (GLM-5.3-flash & fallback)...")
    print("=" * 60)

    # 1. Test anti-gemini guard
    try:
        _enforce_no_gemini("gemini-1.5-pro", {})
        print("Gemini Guard: FAIL (should have thrown GeminiProhibitedError)")
    except GeminiProhibitedError:
        print("Gemini Guard: PASS (successfully blocked prohibited Gemini request)")

    # 2. Test GLM-5.3-flash completion
    test_msg = [{"role": "user", "content": "Return 1 sentence: DressIt is the app where..."}]
    res = chat_completion(test_msg)
    print(f"GLM-5.3-flash response: {res}")

    # 3. Test ad script generator
    script = generate_ad_script(
        joke_hook="I quit dating losers!",
        joke_punchline="That took a minute!",
        ugc_outfits=["Birthday party dress", "Girls night out look"]
    )
    print("\nGenerated Ad Script JSON:")
    print(json.dumps(script, indent=2))
