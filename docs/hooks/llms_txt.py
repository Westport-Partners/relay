"""
MkDocs hook: AI-first documentation features for Relay.

On every build this hook does three things:
1. Copies each page's raw Markdown source into the output site at a predictable
   path so the "View as Markdown" JS link resolves on the published site.
2. Writes site/llms.txt — a concise, link-structured index for LLMs (llmstxt.org
   format).
3. Writes site/llms-full.txt — full concatenated Markdown of all included pages.

URL-mapping scheme
------------------
MkDocs uses directory URLs by default: source file ``install.md`` becomes
``install/index.html`` at URL ``/install/``.  We emit the raw Markdown at the
*source-relative* path so it is simple and predictable:

  Source            Emitted MD          View-as-Markdown URL
  ──────────────    ──────────────────  ────────────────────────────────────
  docs/index.md     site/index.md       /index.md  (or /relay/index.md on GH)
  docs/install.md   site/install.md     /install.md
  docs/deploy.md    site/deploy.md      /deploy.md

The JS in copy-page.js maps ``window.location`` back to these paths by stripping
the trailing ``index.html`` / trailing slash and appending ``.md``.

Pages in ``aws_incident-manager/`` are excluded via ``exclude_docs`` in
mkdocs.yml; MkDocs never passes them to the hook so they are automatically
skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.pages import Page

# ──────────────────────────────────────────────────────────────────────────────
# Nav metadata used for llms.txt generation.
# Mirrors the nav structure in mkdocs.yml so descriptions can be added without
# adding a third-party plugin dependency.
# ──────────────────────────────────────────────────────────────────────────────

_SECTION_DESCRIPTIONS: dict[str, str] = {
    "Home": "Project overview and quick orientation.",
    "Architecture": "System design, topologies, and component diagram.",
    "Get started": "Install, deploy, and first-run guides.",
    "Use Relay": "Day-to-day operation, configuration, and integrations.",
    "Reference": "Domain model, feature status ledger, and AWS IM coverage map.",
    "Project": "Vision, contributing guidelines, and security policy.",
}

_PAGE_DESCRIPTIONS: dict[str, str] = {
    "index.md": "Relay overview — what it is, why it exists, and how to get started.",
    "architecture.md": "System design, federated-hub vs team topologies, and component diagram.",
    "install.md": "Install the Relay toolchain on a laptop, CI runner, or bastion host.",
    "deploy.md": "Deploy a Relay Node or Hub into your AWS account.",
    "byor.md": "Bring-Your-Own-Role deployment for locked-down AWS accounts.",
    "local-dev.md": "Run Relay locally with Docker Compose for development and testing.",
    "configure.md": "Configure rules, routing, ignore policies, and GitLab integration.",
    "operate.md": "Day-to-day incident operations: acknowledge, escalate, resolve.",
    "scheduling.md": "On-call scheduling, availability windows, and escalation chains.",
    "integrations.md": "Integrations: MS Teams, GitLab, ServiceNow, and AI investigation skills.",
    "domains.md": "Domain model reference: entities, relationships, and DynamoDB schema.",
    "status.md": "Code-verified feature status ledger.",
    "coverage.md": "Relay vs AWS Incident Manager feature coverage map.",
    "vision.md": "Product vision, OSS philosophy, and roadmap.",
    "contributing.md": "Contributing guide: setup, conventions, Definition of Done.",
    "security.md": "Security policy, disclosure process, and hardening notes.",
}


# ──────────────────────────────────────────────────────────────────────────────
# Hook state — populated incrementally during the build
# ──────────────────────────────────────────────────────────────────────────────

class _State:
    def __init__(self) -> None:
        self.pages: list[tuple[str, str, str]] = []  # (title, src_path, markdown)
        self.site_dir: str = ""
        self.site_name: str = ""
        self.site_description: str = ""
        self.site_url: str = ""


_state = _State()


# ──────────────────────────────────────────────────────────────────────────────
# Hook entry points
# ──────────────────────────────────────────────────────────────────────────────

def on_config(config: MkDocsConfig) -> None:
    """Capture site-level config for use in post-build."""
    _state.site_dir = config["site_dir"]
    _state.site_name = config.get("site_name", "Relay")
    _state.site_description = config.get(
        "site_description",
        "Open-source AWS Incident Manager replacement.",
    )
    _state.site_url = (config.get("site_url") or "").rstrip("/")
    _state.pages = []


def on_page_content(html: str, page: Page, config: MkDocsConfig, **kwargs: object) -> str:
    """
    Called after each page is rendered to HTML.  We grab the raw Markdown here
    (page.markdown is set by this point) and stash it for post-build.
    """
    src = page.file.src_path  # e.g. "install.md", "index.md"

    # Skip excluded paths — should never arrive here, but be defensive.
    if src.startswith("aws_incident-manager/"):
        return html

    markdown = page.markdown or ""
    title = page.title or src
    _state.pages.append((title, src, markdown))

    return html


def on_post_build(config: MkDocsConfig) -> None:
    """Write per-page .md files, llms.txt, and llms-full.txt into the site dir."""
    site_dir = Path(_state.site_dir)

    # ── 1. Emit per-page raw Markdown ────────────────────────────────────────
    for _title, src_path, markdown in _state.pages:
        dest = site_dir / src_path  # e.g. site/install.md, site/index.md
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(markdown, encoding="utf-8")

    # ── 2. llms.txt ─────────────────────────────────────────────────────────
    _write_llms_txt(site_dir)

    # ── 3. llms-full.txt ────────────────────────────────────────────────────
    _write_llms_full_txt(site_dir)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _md_url(src_path: str) -> str:
    """Return the absolute URL to a page's raw .md file."""
    base = _state.site_url or ""
    return f"{base}/{src_path}"


def _page_desc(src_path: str) -> str:
    return _PAGE_DESCRIPTIONS.get(src_path, "")


def _write_llms_txt(site_dir: Path) -> None:
    """
    Generate llms.txt following the llmstxt.org convention:
      # Site Name
      > Short description
      ## Section
      - [Title](url): description
    """
    lines: list[str] = []
    lines.append(f"# {_state.site_name}")
    lines.append("")
    lines.append(f"> {_state.site_description}")
    lines.append("")

    # Build a quick lookup from src_path → (title, markdown)
    page_map: dict[str, tuple[str, str]] = {
        src: (title, md) for title, src, md in _state.pages
    }

    # Walk the nav structure from mkdocs.yml (mirrored here) so sections match.
    nav_structure: list[tuple[str, list[str]]] = [
        ("Home", ["index.md"]),
        ("Architecture", ["architecture.md"]),
        ("Get started", ["install.md", "deploy.md", "byor.md", "local-dev.md"]),
        ("Use Relay", ["configure.md", "operate.md", "scheduling.md", "integrations.md"]),
        ("Reference", ["domains.md", "status.md", "coverage.md"]),
        ("Project", ["vision.md", "contributing.md", "security.md"]),
    ]

    for section_name, src_paths in nav_structure:
        section_desc = _SECTION_DESCRIPTIONS.get(section_name, "")
        lines.append(f"## {section_name}")
        if section_desc:
            lines.append(f"> {section_desc}")
            lines.append("")
        for src in src_paths:
            if src not in page_map:
                continue
            title, _ = page_map[src]
            url = _md_url(src)
            desc = _page_desc(src)
            if desc:
                lines.append(f"- [{title}]({url}): {desc}")
            else:
                lines.append(f"- [{title}]({url})")
        lines.append("")

    (site_dir / "llms.txt").write_text("\n".join(lines), encoding="utf-8")


def _write_llms_full_txt(site_dir: Path) -> None:
    """
    Generate llms-full.txt: full concatenated Markdown of every included page,
    each preceded by a separator header so an LLM knows where each document
    starts and ends.
    """
    chunks: list[str] = []
    for title, src_path, markdown in _state.pages:
        sep = "=" * 72
        chunks.append(f"{sep}\n# {title}\n# Source: {src_path}\n{sep}\n\n{markdown}")

    (site_dir / "llms-full.txt").write_text(
        "\n\n".join(chunks) + "\n",
        encoding="utf-8",
    )
