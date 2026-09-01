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
    "provider": "grok",                   # Human-readable label only, used in logs -- no
                                           # code branches on this string.
    "model": "grok-4",                    # Model name identifier
    "endpoint": "https://api.x.ai/v1/chat/completions",
    # Which app.config.Settings field holds this provider's key. To add a new provider:
    # add its own `<provider>_api_key` field to Settings, then point a new config dict here
    # at it -- no other file needs to change.
    "api_key_setting": "grok_api_key",
    # Header name the key is sent under, e.g. Sarvam's "api-subscription-key". Omitted here
    # -- Grok's API is OpenAI-compatible and takes the standard "Authorization: Bearer <key>",
    # which is nlu_client.py's default when auth_header isn't set.
    "temperature": 0.2,
    "max_tokens": 300,
    "timeout": 5.0,                      # Timeout in seconds
}

# Fallback NLU Configuration -- tried only if PRIMARY_NLU has no configured key or its call
# fails/errors (see classify_message in nlu_client.py). Sarvam was the previous primary, kept
# here as a live safety net rather than dropped, since it's already a proven, working config.
FALLBACK_NLU = {
    "provider": "sarvam",
    "model": "sarvam-105b",
    "endpoint": "https://api.sarvam.ai/v1/chat/completions",
    "api_key_setting": "sarvam_api_key",
    "auth_header": "api-subscription-key",
    "temperature": 0.2,
    "max_tokens": 300,
    "timeout": 5.0,
}
