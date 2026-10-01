"""Keep the product documentation entrance and its website links navigable."""

from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).parents[1]
SITE_URL = "https://www.raffaelefarinaro.com/ciaobot/"
REPO_URL = "https://github.com/raffaelefarinaro/ciaobot/blob/main/"


class PageLinks(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()
        self.videos: list[dict[str, str | None]] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "video":
            self.videos.append(attributes)
        if identifier := attributes.get("id"):
            self.ids.add(identifier)
        if tag == "a" and (href := attributes.get("href")):
            self.links.append(href)


def markdown_ids(text: str) -> set[str]:
    headings = re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)
    return {
        re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        for heading in headings
    }


@pytest.mark.parametrize(
    "relative",
    ["README.md", "docs/README.md", "site/index.html", "site/models.html", "site/memory.html"],
)
def test_documentation_entry_links_resolve(relative: str) -> None:
    source = ROOT / relative
    text = source.read_text(encoding="utf-8")
    links = (
        PageLinks(text).links
        if source.suffix == ".html"
        else re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    )
    assert links
    for href in links:
        if href.startswith(SITE_URL):
            base = ROOT / "site"
            href = href.removeprefix(SITE_URL)
            if not urlsplit(href).path:
                href = "index.html" + href
        elif href.startswith(REPO_URL):
            base = ROOT
            href = href.removeprefix(REPO_URL)
        else:
            base = source.parent
            if urlsplit(href).scheme or href.startswith("//"):
                continue
        url = urlsplit(href)
        target = base / unquote(url.path) if url.path else source
        if target.is_dir():
            target /= "index.html"
        assert target.is_file(), f"{relative}: missing target for {href}"
        if url.fragment:
            target_text = target.read_text(encoding="utf-8")
            ids = (
                PageLinks(target_text).ids
                if target.suffix == ".html"
                else markdown_ids(target_text)
            )
            assert unquote(url.fragment) in ids, f"{relative}: missing anchor for {href}"


def test_home_has_one_controllable_product_demo() -> None:
    text = (ROOT / "site/index.html").read_text(encoding="utf-8")
    page = PageLinks(text)
    assert "See it in action: one week later" not in text
    assert "archive" not in page.ids
    assert len(page.videos) == 1
    video = page.videos[0]
    assert "data-product-demo" in video
    for attribute in ("controls", "muted", "loop", "playsinline"):
        assert attribute in video
    # Autoplay is opted into by JS only after checking reduced-motion preferences.
    assert "autoplay" not in video
    assert video.get("aria-label")
    for attribute in ("src", "poster"):
        path = video[attribute]
        assert path is not None
        assert (ROOT / "site" / path).is_file()
