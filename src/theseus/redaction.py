"""Redaction of secrets in text theseus stores: API keys, key hashes, sk- style tokens and email addresses. The gateway
error excerpts and every screen theseus writes to disk (the supervisor's crash-GEN.txt) go through it."""
import re

PATTERNS = ((re.compile(r"(api[_ -]?key\s*[:=]\s*)[^\s,.;]+", re.IGNORECASE), r"\1[redacted]"),
            (re.compile(r"\b(?:sk|pk|key)-[A-Za-z0-9_\-]{8,}"), "[redacted]"),
            (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "[redacted]"),
            (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"))


def redact(text):
    """text with every secret pattern replaced, its lines and spacing kept."""
    text = str(text or "")
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def excerpt(text, limit=240):
    """A redacted one-line excerpt of at most limit characters."""
    text = redact(" ".join(str(text or "").split()))
    return text if len(text) <= limit else text[:limit - 3] + "..."
