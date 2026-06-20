import functools
import mimetypes
import re
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from typing import Generator

import requests
from deltachat2 import Bot

www = requests.Session()
www.headers.update(
    {
        "user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:60.0) Gecko/20100101 Firefox/60.0"
    }
)
www.request = functools.partial(www.request, timeout=15)  # type: ignore


def get_channels(bot: Bot, accid: int, admin_chat: int) -> list[tuple[int, str]]:
    """Get list of active channels.

    Chats that are not channels will be removed.
    """
    chats = []
    for chatid in bot.rpc.get_chatlist_entries(accid, 0x02, None, None):
        chat = bot.rpc.get_basic_chat_info(accid, chatid)
        if chat.chat_type == "OutBroadcast":
            if chat.pinned:
                chats.append((chat.id, chat.name))
        elif chatid != admin_chat:
            bot.rpc.delete_chat(accid, chatid)
    return chats


@contextmanager
def download_image(url: str) -> Generator[str | None, None, None]:
    """Download image.
    If the image was successfully downloaded, its path will be returned.
    If download failed, None is returned.

    NOTE: The image will be deleted when the context is closed.
    """
    try:
        with www.get(url) as resp:
            if resp.status_code < 400 or resp.status_code >= 600:
                with NamedTemporaryFile(suffix=_get_img_ext(resp)) as temp_file:
                    with open(temp_file.name, "wb") as file:
                        file.write(resp.content)
                    yield temp_file.name
                    return
    except Exception:
        pass
    yield None


def _get_img_ext(resp: requests.Response) -> str:
    """Get image file extension from a web response"""
    disp = resp.headers.get("content-disposition")
    if disp is not None and re.findall("filename=(.+)", disp):
        fname = re.findall("filename=(.+)", disp)[0].strip('"')
    else:
        fname = resp.url.split("/")[-1].split("?")[0].split("#")[0]
    if "." in fname:
        ext = "." + fname.rsplit(".", maxsplit=1)[-1]
    else:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "image/jpeg":
            ext = ".jpg"
        else:
            ext = mimetypes.guess_extension(ctype)
    return ext
