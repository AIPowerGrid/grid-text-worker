# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared local-backend prompts and response cleanup."""

import re

ENLISTMENT_PROMPT = (
    "You are {model}, an AI model being enlisted for service on AI Power Grid, "
    "a decentralized network where AI workers serve real user requests and earn rewards. "
    "This is your enlistment. Report for duty in 2-3 sentences. "
    "State your name, what you're good at, and that you're ready to serve. Show some personality."
)


def strip_thinking_tags(text: str) -> str:
    """Remove common reasoning wrappers from a local test response."""
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL).strip()
