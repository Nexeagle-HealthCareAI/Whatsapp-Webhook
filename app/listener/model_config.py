"""
model_config.py
----------------
Dedicated configuration file for NLU and AI model parameters.
Allows easy updates to model names, API endpoints, temperatures, timeouts, and providers.
"""
import os

# Primary NLU Configuration
PRIMARY_NLU = {
    "provider": "sarvam",                # Options: "sarvam", "openai", "custom"
    "model": "sarvam-105b",              # Model name identifier
    "endpoint": "https://api.sarvam.ai/v1/chat/completions",
    "temperature": 0.2,
    "max_tokens": 300,
    "timeout": 5.0,                      # Timeout in seconds
}

# Fallback NLU Configuration (Currently Disabled / Set to None)
FALLBACK_NLU = None
