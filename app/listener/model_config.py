"""
model_config.py
----------------
Dedicated configuration file for NLU and AI model parameters.

Switching providers (e.g. Sarvam -> Grok) is a config-only change: add/edit a dict here,
nothing in app/listener/nlu_client.py needs to change. That's the whole point of
api_key_setting/auth_header below -- nlu_client.py reads a provider's api key and builds its
auth header generically from these two fields instead of a hardcoded "sarvam_api_key" /
"api-subscription-key" per call site (which is what it did before, and the reason a plain
API-key swap silently broke everything: the URL and header name are provider-specific too).
"""

# Primary NLU Configuration
PRIMARY_NLU = {
    "provider": "sarvam",                 # Human-readable label only, used in logs -- no
                                           # code branches on this string.
    "model": "sarvam-105b",               # Model name identifier
    "endpoint": "https://api.sarvam.ai/v1/chat/completions",
    # Which app.config.Settings field holds this provider's key. To add a new provider:
    # add its own `<provider>_api_key` field to Settings, then point a new config dict here
    # at it -- no other file needs to change.
    "api_key_setting": "sarvam_api_key",
    # Header name the key is sent under, e.g. "api-subscription-key". Omit (or None) for the
    # standard "Authorization: Bearer <key>" most OpenAI-compatible APIs (Grok included) use.
    "auth_header": "api-subscription-key",
    "temperature": 0.2,
    "max_tokens": 300,
    "timeout": 5.0,                      # Timeout in seconds
}

# Fallback NLU Configuration (Currently Disabled / Set to None)
FALLBACK_NLU = None
