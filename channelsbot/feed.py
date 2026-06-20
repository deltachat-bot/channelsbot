import datetime
import json
import re
import time
from multiprocessing.pool import ThreadPool
from typing import Optional

import bs4
import feedparser
import requests
from deltabot_cli import BotCli
from deltachat2 import Bot, JsonRpcError, MsgData
from feedparser import FeedParserDict
from feedparser.datetimes import _parse_date
from feedparser.exceptions import CharacterEncodingOverride

from .util import download_image, get_channels, www


def get_last_modified(fdict: FeedParserDict) -> tuple | None:
    return fdict.get("modified") or fdict.get("updated")


def get_feed_logo(fdict: FeedParserDict) -> str | None:
    return fdict.feed.get("image", {}).get("href") or fdict.feed.get("logo")


def _get_entry_image(entry: dict) -> str | None:
    url = entry.get("media_thumbnail", [{}])[0].get("url")
    return url or entry.get("media_content", [{}])[0].get("url")


def parse_feed(
    url: str, etag: Optional[str] = None, modified: Optional[tuple] = None
) -> feedparser.FeedParserDict:
    headers = {"A-IM": "feed", "Accept-encoding": "gzip, deflate"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        if isinstance(modified, str):
            modified = _parse_date(modified)
        elif isinstance(modified, datetime.datetime):
            modified = modified.utctimetuple()
        short_weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        months = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        headers["If-Modified-Since"] = "%s, %02d %s %04d %02d:%02d:%02d GMT" % (  # noqa
            short_weekdays[modified[6]],
            modified[2],
            months[modified[1] - 1],
            modified[0],
            modified[3],
            modified[4],
            modified[5],
        )
    with www.get(url, headers=headers, stream=True) as resp:
        resp.raise_for_status()
        text = get_response_text(resp, 1024**2 * 10)  # 10MB
        dict_ = feedparser.parse(text)
    bozo_exception = dict_.get("bozo_exception", ValueError("Invalid feed"))
    if (
        dict_.get("bozo")
        and not isinstance(bozo_exception, CharacterEncodingOverride)
        and not dict_.get("entries")
    ):
        raise bozo_exception
    return dict_


def get_response_text(resp: requests.Response, max_size: int) -> str:
    """
    Return the response text only if the total payload size does not exceed max_size.
    If the size is unknown or exceeds the limit, an empty string is returned.
    """
    # Try to get the size from the headers
    content_length = int(resp.headers.get("content-length", -1))

    if content_length > max_size:
        return ""  # skip reading the body

    # content_length might be -1/unknown or fake so check manually
    content = bytearray()
    total = 0
    for chunk in resp.iter_content(chunk_size=102400):  # 100KB chunks
        total += len(chunk)
        if total > max_size:
            return ""  # limit exceeded, discard
        content.extend(chunk)

    encoding = resp.encoding or "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        return content.decode(errors="replace")


def _entry2msg(
    name: str,
    entry: dict,
    include_link: bool,
    include_html: bool,
    extract_img: bool,
    social_img: bool,
) -> MsgData:
    msg = MsgData(text="", file=_get_entry_image(entry), override_sender_name=name)
    link = entry.get("link", "")

    for c in entry.get("content", []):
        if c.get("type") == "text/html":
            msg.html = c.get("value")
    if not msg.html:
        msg.html = entry.get("description", "")
    desc_soup = _sanitized_soup(link, msg.html)
    msg.html = str(desc_soup)
    for tag in desc_soup("br"):
        tag.extract()

    title = entry.get("title") or ""
    if title:
        title_soup = _sanitized_soup(link, title.rstrip("."))
        if not " ".join(desc_soup.get_text().split()).startswith(
            " ".join(title_soup.get_text().split())
        ):
            title_soup = _sanitized_soup(link, title)
            msg.html = f"<h1><a href={link}>{title_soup}</a></h1>{msg.html}"
            title = title_soup.get_text().strip()
            if include_html and len(title) > 250:
                title = title[:250] + "..."
            msg.text += title

    if not msg.file and extract_img:
        msg.file = (desc_soup.img or {}).get("src")
        if msg.file and msg.file.endswith((".svg")):
            msg.file = None

    if not msg.file and social_img:
        try:
            msg.file = _get_social_image(link)
        except Exception as ex:
            print("ERROR:", ex)

    desc = desc_soup.get_text().strip()
    if include_html and len(desc) > 250:
        desc = desc[:250] + "..."
    msg.text += "\n\n" + desc
    if include_link and link not in msg.text:
        msg.text += "\n\n" + link
    msg.text = msg.text.strip()

    if not include_html:
        msg.html = None

    return msg


def _get_social_image(url: str) -> str | None:
    with www.get(url) as resp:
        resp.raise_for_status()
        soup = bs4.BeautifulSoup(resp.text, "html.parser")

    tag = soup.find("meta", attrs={"property": "og:image"})
    img_url = tag and tag["content"].strip()
    if img_url and not img_url.startswith("http"):
        img_url = None
    return img_url


def _sanitized_soup(url: str, html: str) -> bs4.BeautifulSoup:
    """Sanitize BeautifulSoup. Fix links inside the HTML."""
    NON_BREAKING_ELEMENTS = [
        "a",
        "abbr",
        "acronym",
        "audio",
        "b",
        "bdi",
        "bdo",
        "big",
        "button",
        "canvas",
        "cite",
        "code",
        "data",
        "datalist",
        "del",
        "dfn",
        "em",
        "embed",
        "i",
        "iframe",
        "img",
        "input",
        "ins",
        "kbd",
        "label",
        "map",
        "mark",
        "meter",
        "noscript",
        "object",
        "output",
        "picture",
        "progress",
        "q",
        "ruby",
        "s",
        "samp",
        "script",
        "select",
        "slot",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "svg",
        "template",
        "textarea",
        "time",
        "u",
        "tt",
        "var",
        "video",
        "wbr",
    ]
    soup = bs4.BeautifulSoup(" ".join(html.replace("\n", " ").split()), "html.parser")
    for element in soup(["script"]):
        element.extract()
    for element in soup.find_all():
        if element.name == "br":
            element.append("\n")
        elif element.name not in NON_BREAKING_ELEMENTS:
            element.append("\n\n")

    index = url.find("/", 8)
    if index == -1:
        root = url
    else:
        root = url[:index]
        url = url.rsplit("/", 1)[0]
    tags = (("a", "href"), ("img", "src"), ("source", "src"), ("link", "href"))
    for tag, attr in tags:
        for element in soup(tag, attrs={attr: True}):
            element[attr] = re.sub(
                r"^(//.*)", rf"{root.split(':', 1)[0]}:\1", element[attr]
            )
            element[attr] = re.sub(r"^(/.*)", rf"{root}\1", element[attr])
            if not re.match(r"^\w+:", element[attr]):
                element[attr] = f"{url}/{element[attr]}"

    return soup


def get_new_entries(entries: list, date: tuple) -> list:
    new_entries = []
    for e in entries:
        d = e.get("published_parsed") or e.get("updated_parsed")
        if d is not None and d > date:
            new_entries.append(e)
    return new_entries


def get_old_entries(entries: list, date: tuple) -> list:
    old_entries = []
    for e in entries:
        d = e.get("published_parsed") or e.get("updated_parsed")
        if d is not None and d <= date:
            old_entries.append(e)
    return old_entries


def get_latest_date(entries: list) -> Optional[str]:
    dates = []
    for e in entries:
        d = e.get("published_parsed") or e.get("updated_parsed")
        if d:
            dates.append(d)
    return " ".join(map(str, max(dates))) if dates else None


def check_feeds(
    cli: BotCli, bot: Bot, accid: int, interval: int, pool_size: int
) -> None:
    admin_chat = cli.get_admin_chat(bot.rpc, accid)
    config_key = "ui.channelsbot.lastchecked"
    lastcheck = float(bot.rpc.get_config(accid, config_key) or 0.0)
    took = max(time.time() - lastcheck, 0)

    with ThreadPool(pool_size) as pool:
        while True:
            delay = interval - took
            if delay > 0:
                bot.logger.info(f"[WORKER] Sleeping for {delay:.0f} seconds")
                time.sleep(delay)
            bot.logger.info("[WORKER] Starting to check feeds")
            lastcheck = time.time()
            bot.rpc.set_config(accid, config_key, str(lastcheck))
            channels = get_channels(bot, accid, admin_chat)
            bot.logger.info(f"[WORKER] There are {len(channels)} channels to check")
            for _ in pool.imap_unordered(
                lambda c: _check_feed_task(bot, accid, c[0], c[1]), channels
            ):
                pass
            took = time.time() - lastcheck
            bot.logger.info(
                f"[WORKER] Done checking {len(channels)} channels after {took:.1f} seconds"
            )


def _check_feed_task(bot: Bot, accid: int, chatid: int, name: str):
    data = json.loads(bot.rpc.get_draft(accid, chatid).text)
    data["name"] = name
    bot.logger.debug(f"[{name}] Checking feed: {data['url']}")
    try:
        _check_feed(bot, accid, chatid, data)
    except Exception as err:
        bot.logger.error(f"[{name}] Error while checking feed: {data['url']}: {err}")
    bot.logger.debug(f"[{name}] Done checking feed: {data['url']}")


def _check_feed(bot: Bot, accid: int, chanid: int, data: dict) -> None:
    fdict = parse_feed(data["url"], etag=data["etag"], modified=data["modified"])

    if fdict.entries and data["latest"]:
        fdict.entries = get_new_entries(
            fdict.entries, tuple(map(int, data["latest"].split()))
        )

    if not fdict.entries:
        return

    send_feed_entries(bot, accid, chanid, data, fdict.entries, 100)

    # update channel metadata
    data["latest"] = get_latest_date(fdict.entries) or data["latest"]
    data["modified"] = get_last_modified(fdict)
    data["etag"] = fdict.get("etag")
    bot.rpc.misc_set_draft(accid, chanid, json.dumps(data), None, None, None, None)


def send_feed_entries(
    bot: Bot, accid: int, chatid: int, data: dict, entries: list[dict], max_count: int
) -> None:
    def send_msg(msg: MsgData) -> None:
        if msg.text or msg.html or msg.file:
            try:
                bot.rpc.send_msg(accid, chatid, msg)
            except JsonRpcError as ex:
                bot.logger.exception(ex)

    msgs = []
    for entry in entries:
        msg = _entry2msg(
            data["name"],
            entry,
            data["includeLink"],
            data["html"],
            data["extractImg"],
            data.get("socialImg", False),
        )
        if data["filter"] not in msg.text and data["filter"] not in msg.html:
            continue

        msgs.append(msg)
        if len(msgs) == max_count:
            break

    for msg in reversed(msgs) if data["reversed"] else msgs:
        if msg.file:
            with download_image(msg.file) as path:
                if not path:
                    bot.logger.error(f"Failed to download: {msg.file}")
                msg.file = path
                send_msg(msg)
        else:
            send_msg(msg)
