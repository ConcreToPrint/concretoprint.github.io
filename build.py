"""Build the ConcreTOPrint website.

    python build.py            build the site into _site/ (drafts are left out)
    python build.py --serve    build with drafts, serve on http://localhost:8000
                               and rebuild whenever a file changes

You should rarely need to edit this file: text lives in content/,
HTML lives in templates/, styles and images live in assets/.
"""

import argparse
import datetime as dt
import functools
import http.server
import os
import posixpath
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"
ASSETS = ROOT / "assets"
PUBLIC = ROOT / "public"  # copied as-is to the site root (favicon.ico, CNAME, robots.txt)
OUT = ROOT / "_site"
WATCHED = (CONTENT, TEMPLATES, ASSETS, PUBLIC)


class BuildError(Exception):
    """A problem in the content that the editor can fix; shown without a traceback."""


# ---------------------------------------------------------------- reading content

def read_yaml(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise BuildError(f"{path.relative_to(ROOT)} is not valid YAML:\n{e}") from None


def read_markdown_file(path):
    """Split a Markdown file into (front matter dict, body text)."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        raise BuildError(f"{path.relative_to(ROOT)} must start with a front matter block between '---' lines.")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        raise BuildError(f"Front matter of {path.relative_to(ROOT)} is not valid YAML:\n{e}") from None
    return meta, match.group(2)


def require(meta, keys, path):
    missing = [k for k in keys if not meta.get(k)]
    if missing:
        raise BuildError(f"{path.relative_to(ROOT)} is missing: {', '.join(missing)}")


def as_date(value, path):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise BuildError(f"{path.relative_to(ROOT)}: date must look like 2026-10-26, got {value!r}") from None


def blog_path(src):
    """Images in post front matter are written relative to content/blog/."""
    if not src or src.startswith(("/", "http://", "https://")):
        return src
    return "/blog/" + src


def load_posts(include_drafts):
    posts = []
    for path in sorted((CONTENT / "blog").glob("*.md")):
        meta, body = read_markdown_file(path)
        if meta.get("draft") and not include_drafts:
            continue
        require(meta, ["title", "date", "summary"], path)
        posts.append({
            **meta,
            "date": as_date(meta["date"], path),
            "image": blog_path(meta.get("image")),
            "slug": path.stem,
            "url": f"/blog/{path.stem}.html",
            "body": body,
        })
    posts.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)  # newest first
    return posts


def load_home():
    order = read_yaml(CONTENT / "home.yaml").get("sections") or []
    sections = {}
    for name in order:
        if name == "divider" or name in sections:
            continue
        path = CONTENT / "home" / f"{name}.yaml"
        if not path.exists():
            raise BuildError(f"content/home.yaml lists '{name}', but content/home/{name}.yaml does not exist.")
        data = read_yaml(path)
        template = f"sections/{data.get('template', name)}.html"
        if not (TEMPLATES / template).exists():
            raise BuildError(f"Section '{name}' needs templates/{template} (or set 'template: text' in its YAML).")
        sections[name] = {**data, "template": template}
    return order, sections


# ---------------------------------------------------------------- rendering helpers

MD_EXTENSIONS = ["extra", "sane_lists"]


def md(text):
    """Markdown to HTML (paragraphs, lists, links, images, tables)."""
    return Markup(markdown.markdown(str(text or ""), extensions=MD_EXTENSIONS))


def md_inline(text):
    """Markdown for a single line: no wrapping <p>."""
    html = markdown.markdown(str(text or ""), extensions=MD_EXTENSIONS)
    return Markup(re.sub(r"^<p>(.*)</p>$", r"\1", html, flags=re.S))


def long_date(d):
    return f"{d.day} {d:%B %Y}"


def placeholder(label):
    """Yellow highlighted [label] for anything still to be filled in."""
    return Markup('<span class="ph">[{}]</span>').format(label)


def is_current(href, url):
    """True when a nav link points at this page or the section it belongs to (e.g. Blog for posts)."""
    return href == url or url.startswith(href.removesuffix(".html") + "/")


def make_env(site):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(md=md, md_inline=md_inline, long_date=long_date)

    def email_link():
        if site.get("email"):
            return Markup('<a href="mailto:{0}">{0}</a>').format(site["email"])
        return placeholder("team email address")

    env.globals.update(
        site=site,
        year=dt.date.today().year,
        placeholder=placeholder,
        email_link=email_link,
        is_current=is_current,
    )
    return env


# Root-relative links ("/assets/...") are written everywhere in templates and content.
# After rendering they are rewritten relative to each page, so the finished site works
# at a domain root, in a sub-folder (GitHub Pages project sites) and from disk.
LINK_ATTR = re.compile(r'(\s(?:href|src)=")/(?!/)([^"]*)"')


def relative_links(html, page):
    page_dir = posixpath.dirname(page)

    def fix(m):
        path, _, frag = m.group(2).partition("#")
        path = path or "index.html"
        if frag and path == page:
            return f'{m.group(1)}#{frag}"'  # same page: plain anchor, no reload
        rel = posixpath.relpath(path, page_dir or ".")
        return f'{m.group(1)}{rel}{"#" + frag if frag else ""}"'

    return LINK_ATTR.sub(fix, html)


def prefixed_links(html, base):
    """For 404.html, which the host serves at any address: keep links absolute, add the base path."""
    return LINK_ATTR.sub(lambda m: f'{m.group(1)}{base}/{m.group(2)}"', html)


# ---------------------------------------------------------------- build

def build(include_drafts=False):
    site = read_yaml(CONTENT / "site.yaml")
    require(site, ["name", "url"], CONTENT / "site.yaml")
    site["url"] = site["url"].rstrip("/")
    base_path = urlsplit(site["url"]).path.rstrip("/")

    env = make_env(site)
    posts = load_posts(include_drafts)
    order, sections = load_home()

    pages = {}  # output path -> html

    def render(out, template, **ctx):
        url = "/" + out
        ctx.setdefault("canonical", site["url"] + (url.removesuffix("index.html")))
        try:
            pages[out] = env.get_template(template).render(url=url, **ctx)
        except TemplateNotFound as e:
            raise BuildError(f"Template not found: templates/{e.name}") from None

    render("index.html", "home.html", order=order, sections=sections, posts=posts)
    render("blog.html", "blog.html", title="Blog", posts=posts,
           description=f"Build notes, test results and team news from the {site['name']} team.")

    for i, post in enumerate(posts):
        render(f"blog/{post['slug']}.html", "post.html", post=post, title=post["title"],
               description=post["summary"], og_type="article",
               content=md(post["body"]),
               newer=posts[i - 1] if i > 0 else None,
               older=posts[i + 1] if i + 1 < len(posts) else None)

    for path in sorted((CONTENT / "pages").glob("*.md")):
        meta, body = read_markdown_file(path)
        require(meta, ["title"], path)
        body = env.from_string(body).render()  # lets pages use {{ email_link() }} etc.
        render(f"{path.stem}.html", "page.html", page=meta, title=meta["title"],
               description=meta.get("description"), content=md(body))

    render("404.html", "404.html", title="Page not found", noindex=True)

    # Write everything out
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ASSETS, OUT / "assets")
    if PUBLIC.exists():
        shutil.copytree(PUBLIC, OUT, dirs_exist_ok=True)
    blog_images = CONTENT / "blog" / "images"
    if blog_images.exists():
        shutil.copytree(blog_images, OUT / "blog" / "images", ignore=shutil.ignore_patterns(".gitkeep"))

    for out, html in pages.items():
        html = prefixed_links(html, base_path) if out == "404.html" else relative_links(html, out)
        target = OUT / out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    drafts = sum(1 for p in posts if p.get("draft"))
    note = f" ({drafts} draft{'s' if drafts != 1 else ''} included)" if drafts else ""
    print(f"Built {len(pages)} pages, {len(posts)} blog post{'s' if len(posts) != 1 else ''}{note} -> _site/")


def safe_build(**kwargs):
    try:
        build(**kwargs)
        return True
    except BuildError as e:
        print(f"\nBuild failed: {e}\n", file=sys.stderr)
        return False


# ---------------------------------------------------------------- local preview

def snapshot():
    return {p: p.stat().st_mtime for d in WATCHED if d.exists() for p in d.rglob("*") if p.is_file()}


def serve(port):
    safe_build(include_drafts=True)

    def watch():
        last = snapshot()
        while True:
            time.sleep(1)
            now = snapshot()
            if now != last:
                last = now
                print("Change detected, rebuilding...")
                safe_build(include_drafts=True)

    threading.Thread(target=watch, daemon=True).start()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(OUT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Preview at http://localhost:{port}  (drafts shown; refresh after saving; Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the ConcreTOPrint website.")
    parser.add_argument("--serve", action="store_true", help="preview locally with drafts and rebuild on change")
    parser.add_argument("--drafts", action="store_true", help="include draft posts in a normal build")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.serve:
        serve(args.port)
    else:
        sys.exit(0 if safe_build(include_drafts=args.drafts) else 1)
