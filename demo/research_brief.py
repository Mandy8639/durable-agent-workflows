"""Demo agent workflow: fetch -> extract -> draft -> critique -> write.

Shows the engine driving tool steps and model steps. Once a model step is
checkpointed, its (nondeterministic) output is reused on resume instead of
being regenerated, so a crash never changes the brief that comes out.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from daw.context import StepError
from daw.engine import PROJECT_ROOT
from daw.workflow import RUN_INPUT, Step, Workflow
from demo import llm

FIXTURES = PROJECT_ROOT / "demo" / "fixtures"
MAX_HTML = 400_000
MAX_SOURCE_CHARS = 8000

DEFAULT_INPUT = {
    "topic": "Write-ahead logging in SQLite",
    "url": "https://sqlite.org/wal.html",
    "out": "out/research-brief.md",
}


def build_workflow() -> Workflow:
    mod = "demo.research_brief"
    return Workflow(
        name="demo/research-brief",
        description="research brief agent: fetch, extract, draft, critique, write",
        steps=(
            Step("fetch", f"{mod}:fetch", inputs=(RUN_INPUT,), timeout=30),
            Step("extract", f"{mod}:extract", inputs=("fetch",), timeout=30),
            Step("draft", f"{mod}:draft", inputs=(RUN_INPUT, "extract"), timeout=120, backoff=5,
                 env=llm.ENV),
            Step("critique", f"{mod}:critique", inputs=("extract", "draft"), timeout=120, backoff=5,
                 env=llm.ENV),
            Step("write", f"{mod}:write", inputs=(RUN_INPUT, "fetch", "draft", "critique"), timeout=30,
                 params={"base_dir": os.getcwd()}),
        ),
    )


# --- steps ----------------------------------------------------------------

def fetch(inputs, params, ctx):
    url = inputs[RUN_INPUT]["url"]
    if url.startswith("fixture:"):
        path = FIXTURES / url.removeprefix("fixture:")
        html = path.read_text(encoding="utf-8", errors="replace")
        return {"url": url, "status": 200, "html": html[:MAX_HTML], "bytes": len(html)}
    if not url.startswith(("http://", "https://")):
        raise StepError(f"unsupported url {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "durable-agent-workflows/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(MAX_HTML + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
            status = resp.status
    except OSError as e:
        raise StepError(f"fetch failed: {e}") from None
    html = raw.decode(charset, errors="replace")
    return {"url": url, "status": status, "html": html[:MAX_HTML], "bytes": len(raw)}


class _Text(HTMLParser):
    """Collect prose paragraphs; drop scripts, navigation, menus and tables of contents."""

    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}
    SKIP_CLASSES = ("nav", "menu", "toc", "nosearch", "sidebar", "footer", "breadcrumb")
    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote", "td", "dd", "div"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
    MIN_WORDS = 8

    def __init__(self):
        super().__init__()
        self.title, self.blocks, self._buf, self._in_title = "", [], [], False
        self._stack: list[tuple[str, bool]] = []

    @property
    def _skipping(self) -> bool:
        return any(skip for _, skip in self._stack)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if tag in self.BLOCK:
            self._flush()
        if tag in self.VOID:
            return
        cls = (dict(attrs).get("class") or "").lower()
        skip = tag in self.SKIP_TAGS or any(c in cls for c in self.SKIP_CLASSES)
        self._stack.append((tag, skip))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in self.BLOCK:
            self._flush()
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skipping:
            self._buf.append(data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if len(text.split()) >= self.MIN_WORDS and text[-1:] in ".:;!?)":
            self.blocks.append(text)
        self._buf = []


def extract(inputs, params, ctx):
    p = _Text()
    p.feed(inputs["fetch"]["html"])
    p.close()
    p._flush()
    text, used = "", 0
    for block in p.blocks:
        if used + len(block) > MAX_SOURCE_CHARS:
            break
        text += block + "\n\n"
        used += len(block) + 2
    if not text:
        raise StepError("no readable text extracted")
    return {"title": " ".join(p.title.split()), "text": text.strip(), "chars": used,
            "blocks_total": len(p.blocks)}


DRAFT_SYSTEM = (
    "You write short, accurate research briefs for engineers. Use only the SOURCE text. "
    "Output Markdown with sections: Summary (2-3 sentences), Key points (4-6 bullets), "
    "Trade-offs, Open questions. Under 350 words. No preamble."
)
CRITIQUE_SYSTEM = (
    "You are a strict technical reviewer. Compare the DRAFT with the SOURCE. List, as 3-6 bullets, "
    "claims not supported by the source, important omissions, and unclear wording. No preamble."
)


def draft(inputs, params, ctx):
    src = inputs["extract"]
    user = f"Topic: {inputs[RUN_INPUT]['topic']}\nTitle: {src['title']}\n\nSOURCE:\n{src['text']}"
    return llm.complete("draft", DRAFT_SYSTEM, user)


def critique(inputs, params, ctx):
    user = f"DRAFT:\n{inputs['draft']['text']}\n\nSOURCE:\n{inputs['extract']['text']}"
    return llm.complete("critique", CRITIQUE_SYSTEM, user, max_tokens=600)


def write(inputs, params, ctx):
    run_input = inputs[RUN_INPUT]
    out = Path(run_input.get("out", DEFAULT_INPUT["out"]))
    if not out.is_absolute():
        out = Path(params["base_dir"]) / out
    d, c = inputs["draft"], inputs["critique"]
    body = (
        f"# Research brief: {run_input['topic']}\n\n"
        f"Source: {inputs['fetch']['url']}  \nModel: {d['model']} (draft), {c['model']} (critique)\n\n"
        f"{d['text']}\n\n---\n\n## Reviewer notes (critique step)\n\n{c['text']}\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, out)  # atomic: a crash never leaves a half-written brief
    return {
        "path": str(out),
        "bytes": len(body.encode()),
        "sha256": hashlib.sha256(body.encode()).hexdigest()[:16],
        "cost_usd": round(d.get("cost", 0.0) + c.get("cost", 0.0), 6),
    }
