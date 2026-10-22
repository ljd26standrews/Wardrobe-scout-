"""Which source a piece of evidence came from: one review, post or page, however its link was copied.

The same post shared from an app, opened on a phone, or forwarded with tracking parameters must count once; two
different posts (or two forum topics that differ only in a ``?t=`` parameter) must count twice. So a link becomes a
canonical key:

* scheme, letter case of the host, ``www.``/``m.``/``mobile.``/``amp.`` host forms, a fragment and a trailing slash
  make no difference;
* known tracking and sharing parameters (``utm_*``, ``fbclid``, ``si``, ``spm``, ``xsec_token`` and so on) are
  dropped, and every other parameter is kept, sorted, because it may identify the content;
* on platforms with post or item identifiers (Reddit, YouTube, X/Twitter, Instagram, TikTok, Xiaohongshu, Weibo,
  Taobao/Tmall, Weidian, Yupoo, Pinterest, Facebook) the identifier is the key, so a short link, a mobile link and a
  link with a different title slug agree. A Reddit comment is its own source (another person), keyed by post and
  comment.

Share links that only redirect (``xhslink.com``, ``vm.tiktok.com``, Reddit ``/s/`` links) cannot be resolved offline;
they are keyed as written.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

TRACKING = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid", "ttclid", "igshid", "igsh",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "ref", "ref_src", "ref_url", "referrer", "referer", "si",
    "feature", "share", "share_id", "share_source", "share_medium", "share_from", "share_relation", "sharer",
    "share_token", "share_from_user_hidden", "spm", "scm", "pvid", "abbucket", "ns", "xsec_token", "xsec_source",
    "app_platform", "app_version", "apptime", "wfr", "wm", "sourcetype", "context", "rdt", "_branch_match_id",
    "_branch_referrer", "epik", "srsltid", "is_from_webapp", "sender_device", "web_id", "amp", "outputtype", "cmpid",
    "ocid", "sr_share", "ab_channel", "pp", "uid",
})  # fmt: skip
TRACKING_PREFIXES = ("utm_", "pk_", "hsa_", "mtm_")
HOST_FORMS = ("www.", "m.", "mobile.", "amp.")
REDDIT_FORMS = ("old.", "new.", "np.", "i.")


def _tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING or k.startswith(TRACKING_PREFIXES)


def canonical_source(url: str | None) -> str | None:
    """A stable key for the source behind ``url`` (see the module docstring), or None for a blank link."""
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    stripped = True
    while stripped:
        stripped = False
        for form in HOST_FORMS:
            if host.startswith(form) and host.count(".") > 1:
                host, stripped = host[len(form) :], True
    if host.endswith(".reddit.com") and host.startswith(REDDIT_FORMS):
        host = "reddit.com"
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", unquote(parts.path)).rstrip("/")
    path = path.removesuffix("/amp")
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _tracking(k)]
    platform = _platform(host, path, {k.lower(): v for k, v in params})
    if platform:
        return platform
    query = urlencode(sorted(params))
    return host + path + (f"?{query}" if query else "")


def _platform(host: str, path: str, q: dict[str, str]) -> str | None:
    """The post or item identifier on platforms that have one."""
    if host == "redd.it" and (m := re.fullmatch(r"/([a-z0-9]+)", path, re.I)):
        return f"reddit:{m[1].lower()}"
    if host.endswith("reddit.com"):
        m = re.match(r"(?:/r/[^/]+|/user/[^/]+|/u/[^/]+)?/comments/([a-z0-9]+)(?:/[^/]*(?:/([a-z0-9]+))?)?", path, re.I)
        if m:
            return f"reddit:{m[1].lower()}" + (f":{m[2].lower()}" if m[2] else "")
        return None
    if host in ("youtube.com", "youtube-nocookie.com", "music.youtube.com") or host == "youtu.be":
        if host == "youtu.be" and (m := re.fullmatch(r"/([\w-]{6,})", path)):
            return f"youtube:{m[1]}"
        if path == "/watch" and q.get("v"):
            return f"youtube:{q['v']}"
        if m := re.fullmatch(r"/(?:shorts|embed|live|v)/([\w-]{6,})", path):
            return f"youtube:{m[1]}"
        return None
    if host in ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "nitter.net"):
        if m := re.search(r"/status(?:es)?/(\d+)", path):
            return f"x:{m[1]}"
        return None
    if host == "instagram.com" and (m := re.match(r"(?:/[^/]+)?/(?:p|reel|reels|tv)/([\w-]+)", path)):
        return f"instagram:{m[1]}"
    if host == "tiktok.com" and (m := re.search(r"/video/(\d+)", path)):
        return f"tiktok:{m[1]}"
    if host == "xiaohongshu.com" and (m := re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-f]+)", path, re.I)):
        return f"xiaohongshu:{m[1].lower()}"
    if host in ("weibo.cn", "weibo.com"):
        if m := re.fullmatch(r"/(?:status|detail)/(\w+)", path):
            return f"weibo:{m[1]}"
        if m := re.fullmatch(r"/\d+/(\w+)", path):
            return f"weibo:{m[1]}"
        return None
    if (host.endswith("taobao.com") or host.endswith("tmall.com")) and q.get("id", "").isdigit():
        return f"taobao:{q['id']}"  # Taobao and Tmall share one item numbering
    if host.endswith("weidian.com"):
        item = q.get("itemid") or q.get("item_id")
        return f"weidian:{item}" if item else None
    if host.endswith(".x.yupoo.com") and (m := re.fullmatch(r"/albums/(\d+)", path)):
        return f"yupoo:{host.split('.')[0]}:{m[1]}"
    if re.fullmatch(r"(?:[a-z]{2}\.)?pinterest\.[a-z.]+", host) and (m := re.fullmatch(r"/pin/(\d+)", path)):
        return f"pinterest:{m[1]}"
    if host == "facebook.com":
        if m := re.search(r"/posts/(\w+)", path):
            return f"facebook:{m[1]}"
        if path == "/permalink.php" and q.get("story_fbid"):
            return f"facebook:{q['story_fbid']}"
    return None
