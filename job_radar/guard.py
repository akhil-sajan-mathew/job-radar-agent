"""Posting guard: a job description is third-party text that goes into the scoring prompt next to the resume.

Hidden HTML and invisible characters are removed (the model never sees them); lines that address an AI are
flagged, not removed, because a real posting can say similar things. The model's instruction to treat the
posting as data is the second layer (see scorer.PROMPT).
"""
import html
import re
from html.parser import HTMLParser

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:;|$)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)", re.I)
_SKIP = {"script", "style", "noscript", "template"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# zero-width, soft hyphen, BOM: common copy-paste noise, removed silently
_ZERO_WIDTH = re.compile("[­​-‍⁠﻿]")
# bidi controls and Unicode tag characters (invisible ASCII look-alikes a model can read): removed and flagged
_TAMPER = re.compile("[‪-‮⁦-⁩\U000e0000-\U000e007f]")
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier)"
    r"\s+(?:instructions|prompts?|rules)"
    r"|\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|llm|language\s+model|chatbot|chatgpt|gemini|claude)\b"
    r"|\b(?:system|developer)\s+prompt\b"
    r"|\b(?:score|rate|rank)\s+(?:this|the)\s+(?:candidate|applicant|resume|cv)\b"
    r"|\bnew\s+instructions\s*:"
    r"|^\s*assistant\s*:", re.I | re.M)  # not "system:", real postings write "System: SAP"


class _Text(HTMLParser):
    """Text of an HTML fragment without hidden elements, script/style, or comments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        # Track only the hidden element's own tag name: HTML lets <p>/<li> go unclosed, so counting every
        # tag would leave the rest of the posting hidden.
        self._tag, self._hide = "", 0

    def handle_starttag(self, tag, attrs):
        if self._hide:
            self._hide += tag == self._tag
            return
        a = dict(attrs)
        if tag in _VOID:  # no end tag and no text: hiding one would hide the rest of the posting
            return
        if tag in _SKIP or "hidden" in a or (a.get("aria-hidden") or "").lower() == "true" \
                or _HIDDEN_STYLE.search(a.get("style") or ""):
            self._tag, self._hide = tag, 1

    def handle_startendtag(self, tag, attrs):  # <div hidden/> hides nothing
        pass

    def handle_endtag(self, tag):
        if self._hide and tag == self._tag:
            self._hide -= 1

    def handle_data(self, data):
        (self.hidden if self._hide else self.parts).append(data)


def _flag_instructions(text: str, where: str) -> list[str]:
    return [f'{where}instruction-like text: "{m.group(0).strip()[:60]}"' for m in _INSTRUCTION.finditer(text)]


def clean(raw: str | None) -> tuple[str, list[str]]:
    """Returns (plain text safe to show the model, flags for the user)."""
    if not raw:
        return "", []
    p = _Text()
    p.feed(html.unescape(raw))  # Greenhouse sends escaped HTML; the parser decodes the second level
    p.close()
    text = " ".join(p.parts)
    flags = _flag_instructions(" ".join(p.hidden), "hidden ")
    tamper = len(_TAMPER.findall(text))
    if tamper:
        flags.append(f"{tamper} invisible control/tag characters removed")
    text = _ZERO_WIDTH.sub("", _TAMPER.sub("", text))
    flags += _flag_instructions(text, "")
    return re.sub(r"\s+", " ", text).strip(), flags
