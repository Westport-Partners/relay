"""Backward-compat shim. The Bedrock assistant now lives in relay.adapters.ai."""

from relay.adapters.ai.bedrock_assistant import BedrockAssistant, NoOpAIAssistant

__all__ = ["BedrockAssistant", "NoOpAIAssistant"]
