"""Parse JSON, Inertia data-page HTML, or ordinary SiGMA profile HTML."""

from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlparse


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(filter(None, (text(item) for item in value)))
    if isinstance(value, dict):
        return text(value.get("name") or value.get("label") or value.get("value"))
    return str(value).strip()


LABELS = {
    "bio": "bio", "biography": "bio", "about": "bio", "about me": "bio",
    "what i provide": "provide", "what i offer": "provide",
    "i'm looking for": "looking_for", "what i'm looking for": "looking_for",
    "product/service categories (provide)": "product_service_categories_provide",
    "product/service categories (looking for)": "product_service_categories_looking_for",
    "product/service categories provide": "product_service_categories_provide",
    "product/service categories looking for": "product_service_categories_looking_for",
    "continents of operation": "continents_of_operation",
    "continents of interest": "continents_of_interest",
    "languages": "languages", "booth": "booth", "linkedin": "linkedin",
    "website": "website",
}


def _label(value):
    return re.sub(r"\s+", " ", value.replace("’", "'")).strip(" :\n").casefold()


class ProfileHTML(HTMLParser):
    """Read semantic section text and links, ignoring navigation/script text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.embedded = None
        self.parts = []
        self.links = []
        self.hidden = 0
        self.scripts = []
        self._script = None
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("data-page"):
            self.embedded = json.loads(attrs["data-page"])
        if tag in ("script", "style", "nav"):
            self.hidden += 1
        if tag == "script" and attrs.get("type") == "application/json":
            self._script = []
        if tag == "a":
            self._anchor = [attrs.get("href", ""), []]

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None
        if tag in ("script", "style", "nav"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "a" and self._anchor is not None:
            self.links.append((self._anchor[0], "".join(self._anchor[1])))
            self._anchor = None
        if tag in ("p", "div", "li", "dt", "dd", "h1", "h2", "h3", "h4", "section", "label"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if not self.hidden:
            self.parts.append(data)
            if self._anchor:
                self._anchor[1].append(data)


def _attendee(data: dict) -> dict | None:
    """Explicit paths only: never mistake auth.user/userProfile for the attendee."""
    if not isinstance(data, dict):
        return None
    props = data.get("props")
    if isinstance(props, dict) and isinstance(props.get("attendee"), dict):
        return props["attendee"]
    for key in ("attendee", "profile", "data"):
        if isinstance(data.get(key), dict):
            found = _attendee(data[key])
            if found is not None:
                return found
    if any(key in data for key in ("bio", "first_name", "product_service_categories_provide")):
        return data
    return None


def parse_profile(body: str) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if data is not None:
        attendee = _attendee(data)
        if attendee is None:
            raise ValueError("SiGMA profile JSON has no recognized attendee.")
        return attendee
    parser = ProfileHTML()
    parser.feed(body)
    if parser.embedded is not None:
        attendee = _attendee(parser.embedded)
        if attendee is None:
            raise ValueError("SiGMA HTML data-page has no attendee; refusing partial import.")
        return attendee
    for script in parser.scripts:
        try:
            attendee = _attendee(json.loads(script))
        except ValueError:
            continue
        if attendee is not None:
            return attendee
    result = {}
    active = None
    for line in "".join(parser.parts).splitlines():
        line = line.strip()
        if not line:
            continue
        key = LABELS.get(_label(line))
        if key:
            active = key
        elif ":" in line and _label(line.split(":", 1)[0]) in LABELS:
            label, value = line.split(":", 1)
            active = LABELS[_label(label)]
            result[active] = value.strip()
        elif active:
            result[active] = (result.get(active, "") + "\n" + line).strip()
    for href, label in parser.links:
        url = clean_url(href)
        if url and urlparse(url).hostname in ("linkedin.com", "www.linkedin.com"):
            result["linkedin"] = url
        elif url and _label(label) in ("website", "visit website", "company website"):
            result["website"] = url
    if not result:
        raise ValueError("Unrecognized SiGMA profile HTML; refusing an empty profile import.")
    return result


def clean_url(value) -> str:
    value = text(value)
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if not re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or " " in value:
        return ""
    return value


def plain_html(value) -> str:
    parser = ProfileHTML()
    parser.feed(text(value))
    return unescape("".join(parser.parts)).strip()


def build_contact(item: dict, profile: dict) -> dict:
    """Map an attendee without inferring hidden surnames or private email fields."""
    bio = plain_html(profile.get("bio") or profile.get("biography") or "")
    notes = [bio] if bio else []
    for label, keys in (
        ("Booth", ("booth", "booth_location", "stand")),
        ("Telegram", ("telegram", "telegram_handle")),
        ("What I Provide", ("provide", "what_i_provide", "business_type")),
        ("I'm Looking For", ("looking_for", "what_im_looking_for", "what_i_am_looking_for", "vertical")),
        ("Product/Service categories (provide)", ("product_service_categories_provide",)),
        ("Product/Service categories (looking for)", ("product_service_categories_looking_for",)),
        ("Continents of Operation", ("continents_of_operation",)),
        ("Continents of Interest", ("continents_of_interest",)),
        ("Languages", ("languages",)),
    ):
        value = next((plain_html(profile[k]) for k in keys if profile.get(k)), "")
        if value:
            notes.append(f"{label}: {value}")
    # Email is taken strictly from the bio, never from account/session metadata.
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", bio)
    return {
        "name": " ".join(filter(None, (text(item.get("first_name")), text(item.get("last_name"))))),
        "first_name": text(item.get("first_name")),
        "last_name": text(item.get("last_name")),
        "company": text(item.get("company")),
        "role": text(item.get("job_title")),
        "linkedin": clean_url(profile.get("linkedin") or profile.get("linkedin_url")),
        "website": clean_url(profile.get("website") or profile.get("website_url")),
        "email": email.group(0) if email else "",
        "notes": "\n\n".join(notes),
        "sigma_url": f"https://match.sigma.world/attendees/{item['id']}",
        "business_type": item.get("business_type") or [],
    }
