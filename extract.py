"""Extract links from text, AstrBot components, and nested share cards."""

import html
import json
import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

BV = re.compile(r"(?<![A-Za-z0-9])BV[1-9A-HJ-NP-Za-km-z]{10}(?![A-Za-z0-9])")
URL = re.compile(
    r"https?://[^\s<>\"'\]\[{}\\]+|(?<![\w./])(?:www\.|m\.)?bilibili\.com/video/[^\s<>\"'\]\[{}\\]+|(?<![\w./])b23\.tv/[A-Za-z0-9]+",
    re.I,
)


def kind(component):
    value = (
        component.get("type", "") if isinstance(component, dict) else getattr(component, "type", "")
    )
    return str(getattr(value, "value", value)).lower()


def field(component, name, default=None):
    if isinstance(component, dict):
        return component.get(
            name,
            component.get("data", {}).get(name, default)
            if isinstance(component.get("data"), dict)
            else default,
        )
    return getattr(component, name, default)


def text_values(value, depth=0):
    if depth > 8:
        return
    if isinstance(value, str):
        value = html.unescape(value).replace(r"\/", "/")
        yield value
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if not isinstance(decoded, str):
            yield from text_values(decoded, depth + 1)
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_values(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from text_values(item, depth + 1)
    else:
        for key in ("text", "message_str", "data", "url", "content", "chain", "message"):
            item = getattr(value, key, None)
            if item is not None:
                yield from text_values(item, depth + 1)


def allowed_url(url, domains):
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        return (
            p.scheme in ("http", "https")
            and bool(host)
            and not p.username
            and not p.password
            and p.port in (None, 80, 443)
            and any(
                host == d.lower().strip(".") or host.endswith("." + d.lower().strip("."))
                for d in domains
                if d.strip(".")
            )
        )
    except ValueError:
        return False


def canonical_url(url):
    p = urlsplit(url)
    host = (p.hostname or "").lower().rstrip(".")
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        match = BV.search(p.path)
        if match:
            page = parse_qs(p.query).get("p", ["1"])[0]
            page = str(max(1, int(page))) if page.isdigit() else "1"
            return (
                "https://www.bilibili.com/video/" + match[0] + ("?p=" + page if page != "1" else "")
            )
    if host == "youtu.be":
        return "https://www.youtube.com/watch?" + urlencode({"v": p.path.strip("/")})
    if host.endswith("youtube.com") and p.path == "/watch":
        return "https://www.youtube.com/watch?" + urlencode(
            {"v": parse_qs(p.query).get("v", [""])[0]}
        )
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def find_links(value, domains, bare_bv=False):
    links = []
    for text in text_values(value):
        text = unquote(text)[:100000]
        spans = []
        for match in URL.finditer(text):
            url = match[0].rstrip(".,;!?，。；！？、）)」】")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            spans.append(match.span())
            if allowed_url(url, domains):
                links.append(canonical_url(url))
        if bare_bv:
            for match in BV.finditer(text):
                if not any(a <= match.start() < b for a, b in spans):
                    url = "https://www.bilibili.com/video/" + match[0]
                    if allowed_url(url, domains):
                        links.append(url)
    return list(dict.fromkeys(links))


def current_text(components):
    return " ".join(str(field(c, "text", "")) for c in components if kind(c) in ("plain", "text"))


def mentioned(components, self_id):
    return any(
        kind(c) == "at" and str(field(c, "qq", field(c, "id", ""))) == str(self_id)
        for c in components
    )


def triggered(text, config, pattern=None):
    if config["trigger_mode"] == "auto":
        return True
    if config["trigger_mode"] == "regex":
        try:
            return bool(pattern.search(text[:2000], timeout=0.05))
        except TimeoutError:
            return False
    if any(k and k in text for k in config["keywords"]):
        return True
    return bool(config["bare_bv"] and BV.fullmatch(text.strip()))
