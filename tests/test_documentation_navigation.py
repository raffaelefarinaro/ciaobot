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


def test_home_and_readme_share_the_primary_slogan() -> None:
    slogan = "Your agents do the work. Ciaobot remembers."
    home = (ROOT / "site/index.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<title>{slogan}</title>" in home
    assert f'<meta property="og:title" content="{slogan}">' in home
    assert f'<meta name="twitter:title" content="{slogan}">' in home
    assert "Your agents do the work. <span>Ciaobot remembers.</span>" in home
    assert f"**{slogan}**" in readme


def test_site_install_picker_offers_all_platforms() -> None:
    # macOS offers the native client plus the engine one-liner, Windows has its
    # PowerShell one-liner, and Linux opens its dedicated administrator guide.
    mac = "curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh"
    windows = "irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1 | iex"
    linux = "https://github.com/raffaelefarinaro/ciaobot/blob/main/docs/LINUX.md"
    for relative in ("site/index.html", "site/guide.html"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "data-install" in text and "data-install-os" in text, relative
        assert f'data-cmd="{mac}"' in text and f'data-cmd="{windows}"' in text, relative
        assert f'<code id="install-cmd">{mac}</code>' in text, relative
        assert f'data-url="{linux}"' in text and ">Linux</option>" in text, relative
        assert "data-mac-client" in text and "ciaobot-mac/releases" in text, relative
        assert "Windows (preview)" not in text and "Windows is a preview" not in text, relative
        noscript = text.split("<noscript>", 1)[1]
        assert windows in noscript and linux in noscript, relative
        assert "docs/WINDOWS.md" in text, relative
    script = (ROOT / "site/assets/site.js").read_text(encoding="utf-8")
    assert "[data-install-os]" in script and "option.dataset.cmd" in script
    assert "option.dataset.url" in script and "data-install-guide" in script
    assert "ciaobot-mac/releases?per_page=20" in script and "browser_download_url" in script
