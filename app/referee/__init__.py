"""
app/referee/
------------
Layer 3 -- decides whose turn it is: an in-progress multi-turn NLU accumulation
(intent_router.py) or the current message, when it should override that state
unconditionally (flow_policy.py). See docs/architecture-components.md.
"""
