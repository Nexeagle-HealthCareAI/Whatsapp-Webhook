"""
model_config.py
----------------
Dedicated configuration file for NLU and AI model parameters.

Switching providers (e.g. Sarvam -> Groq) is a config-only change: add/edit a dict here,
nothing in app/listener/nlu_client.py needs to change. That's the whole point of
api_key_setting/auth_header below -- nlu_client.py reads a provider's api key and builds its
auth header generically from these two fields instead of a hardcoded "sarvam_api_key" /
"api-subscription-key" per call site (which is what it did before, and the reason a plain
API-key swap silently broke everything: the URL and header name are provider-specific too).
"""

# Primary NLU Configuration
PRIMARY_NLU = {
    "provider": "groq",                   # Human-readable label only, used in logs -- no
                                           # code branches on this string. NOTE: Groq (Groq
                                           # Inc., LPU-hosted open-weight models, api.groq.com)
                                           # is a DIFFERENT company from xAI's Grok -- an
                                           # earlier version of this config pointed at Grok's
                                           # api.x.ai by mistake, which is why a real Groq key
                                           # got rejected there as "Incorrect API key provided."
    "model": "openai/gpt-oss-20b",        # Model name identifier
    "endpoint": "https://api.groq.com/openai/v1/chat/completions",
    # Which app.config.Settings field holds this provider's key. To add a new provider:
    # add its own `<provider>_api_key` field to Settings, then point a new config dict here
    # at it -- no other file needs to change.
    "api_key_setting": "groq_api_key",
    # Header name the key is sent under, e.g. Sarvam's "api-subscription-key". Omitted here
    # -- Groq's API is OpenAI-compatible and takes the standard "Authorization: Bearer <key>",
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
    # Extra request-body fields this provider's API expects/accepts, merged in on top of the
    # shared body nlu_client.py builds. Provider-specific on purpose: sending Sarvam's
    # "reasoning_effort": null unconditionally to EVERY provider (including whatever's
    # PRIMARY_NLU) broke that provider with a 400 the first time PRIMARY_NLU pointed
    # elsewhere -- an OpenAI-compatible API doesn't expect this field at all.
    "extra_body": {"reasoning_effort": None},
}
