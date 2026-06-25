"""Event Hooks"""

import json
from argparse import Namespace
from threading import Thread

from deltabot_cli import BotCli
from deltachat2 import (
    Bot,
    ChatType,
    CoreEvent,
    EventType,
    JsonRpcError,
    MsgData,
    NewMsgEvent,
    events,
)
from rich.logging import RichHandler

from ._version import __version__
from .feed import (
    check_feeds,
    get_feed_logo,
    get_last_modified,
    get_latest_date,
    get_old_entries,
    parse_feed,
    send_feed_entries,
)
from .util import download_image, get_channels

cli = BotCli("channelsbot")
cli.add_generic_option("-v", "--version", action="version", version=__version__)
cli.add_generic_option(
    "--interval",
    type=int,
    default=60 * 20,
    help="how many seconds to sleep before checking the feeds again (default: %(default)s)",
)
cli.add_generic_option(
    "--parallel",
    type=int,
    default=10,
    help="how many feeds to check in parallel (default: %(default)s)",
)
cli.add_generic_option(
    "--no-time",
    help="do not display date timestamp in log messages",
    action="store_false",
)


def send_help(bot: Bot, accid: int, chat_id: int) -> None:
    text = (
        "Hello, I'm a bot 🤖, I manage some channels."
        " To stop receiving messages, just leave the channels."
    )
    bot.rpc.send_msg(accid, chat_id, MsgData(text=text))


@cli.on_init
def on_init(bot: Bot, args: Namespace) -> None:
    bot.logger.handlers = [
        RichHandler(show_path=False, omit_repeated_times=False, show_time=args.no_time)
    ]
    for accid in bot.rpc.get_all_account_ids():
        if not bot.rpc.get_config(accid, "displayname"):
            bot.rpc.set_config(accid, "displayname", "ChannelsBot")
            status = "I am a Delta Chat bot, send me /help for more info"
            bot.rpc.set_config(accid, "selfstatus", status)


@cli.on_start
def on_start(bot: Bot, args: Namespace) -> None:
    for accid in bot.rpc.get_all_account_ids():
        Thread(
            target=check_feeds,
            args=(cli, bot, accid, args.interval, args.parallel),
            daemon=True,
        ).start()


@cli.on(events.RawEvent)
def log_event(bot: Bot, _accid: int, event: CoreEvent) -> None:
    if event.kind == EventType.INFO:
        bot.logger.debug(event.msg)
    elif event.kind == EventType.WARNING:
        bot.logger.warning(event.msg)
    elif event.kind == EventType.ERROR:
        bot.logger.error(event.msg)


@cli.on(events.NewMessage(is_info=False))
def on_message(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    if bot.has_command(event.command):
        return

    msg = event.msg
    chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
    if chat.chat_type == ChatType.SINGLE:
        bot.rpc.markseen_msgs(accid, [msg.id])
        send_help(bot, accid, event.msg.chat_id)


@cli.on(events.NewMessage(command="/help"))
def _help(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    bot.rpc.markseen_msgs(accid, [event.msg.id])
    if cli.get_admin_chat(bot.rpc, accid) != event.msg.chat_id:
        send_help(bot, accid, event.msg.chat_id)
    else:
        text = "\n\n".join(
            (
                "**Commands:**",
                """/create {"url": "",
                           "name": "",
                           "description": "",
                           "deleteTimer": 3024000,
                           "includeLink": true,
                           "text": true,
                           "html": true,
                           "reversed": true,
                           "extractImg": false,
                           "socialImg": false,
                           "filter": ""}
                """
                "  - create a channel, an image can be attached to use it as channel avatar",
                "/remove ID  -  remove a channel",
                "/update ID <JSON like on /create>  - update channel metadata, an image can be attached",
                "/list  -  show list of channels",
                "/relays [addr]  - list relays, if an address is give, it is set as main relay",
            )
        )
        reply = MsgData(text=text, quoted_message_id=event.msg.id)
        bot.rpc.send_msg(accid, event.msg.chat_id, reply)


@cli.on(events.NewMessage(command="/create"))
def _create(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    bot.rpc.markseen_msgs(accid, [event.msg.id])
    if cli.get_admin_chat(bot.rpc, accid) != event.msg.chat_id:
        send_help(bot, accid, event.msg.chat_id)
        return

    try:
        data = json.loads(event.payload)
        data["deleteTimer"] = data.get("deleteTimer", 3024000)  # 5 weeks
        data["filter"] = data.get("filter", "")
        data["includeLink"] = data.get("includeLink", True)
        data["text"] = data.get("text", True)
        data["html"] = data.get("html", True)
        data["reversed"] = data.get("reversed", True)
        data["extractImg"] = data.get("extractImg", False)
        data["socialImg"] = data.get("socialImg", False)

        fdict = parse_feed(data["url"])
        data["name"] = data.get("name", fdict.feed.get("title"))
        data["etag"] = fdict.get("etag")
        data["modified"] = get_last_modified(fdict)
        data["latest"] = get_latest_date(fdict.entries)
        data["description"] = data.get("description", fdict.feed.get("description"))
    except Exception as ex:
        reply = MsgData(text="❌ Invalid feed url", quoted_message_id=event.msg.id)
        bot.rpc.send_msg(accid, event.msg.chat_id, reply)
        bot.logger.exception("Invalid feed: %s", ex)
        return

    if not data["name"]:
        reply = MsgData(text="❌ Missing channel name", quoted_message_id=event.msg.id)
        bot.rpc.send_msg(accid, event.msg.chat_id, reply)
        return

    chanid = bot.rpc.create_broadcast(accid, data["name"])
    if data["deleteTimer"]:
        bot.rpc.set_chat_ephemeral_timer(accid, chanid, data["deleteTimer"])
    if data["description"]:
        bot.rpc.set_chat_description(accid, chanid, data["description"])
    if event.msg.file:
        bot.rpc.set_chat_profile_image(accid, chanid, event.msg.file)
    elif url := get_feed_logo(fdict):
        with download_image(url) as path:
            try:
                path and bot.rpc.set_chat_profile_image(accid, chanid, path)
            except JsonRpcError as ex:
                bot.logger.exception(ex)
    if fdict.entries and data["latest"]:
        send_feed_entries(
            bot,
            accid,
            chanid,
            data,
            get_old_entries(fdict.entries, tuple(map(int, data["latest"].split()))),
            10,
        )

    # save channel metadata
    bot.rpc.misc_set_draft(accid, chanid, json.dumps(data), None, None, None, None)
    # the channel is ready, pin it so the worker thread starts checking it
    bot.rpc.set_chat_visibility(accid, chanid, "Pinned")

    link = bot.rpc.get_chat_securejoin_qr_code(accid, chanid)
    bot.rpc.send_msg(
        accid,
        event.msg.chat_id,
        MsgData(text=f"{data['name']}\n{link}", quoted_message_id=event.msg.id),
    )


@cli.on(events.NewMessage(command="/list"))
def _list(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    bot.rpc.markseen_msgs(accid, [event.msg.id])

    admin_chat = cli.get_admin_chat(bot.rpc, accid)
    if admin_chat != event.msg.chat_id:
        send_help(bot, accid, event.msg.chat_id)
        return

    channels = get_channels(bot, accid, admin_chat)
    reply = MsgData(text="Channels:\n\n", quoted_message_id=event.msg.id)
    ids = set()
    for chan in sorted(channels, key=lambda chan: chan[0]):
        members = bot.rpc.get_chat_contacts(accid, chan[0])
        ids.update(set(members))
        reply.text += f"#{chan[0]} • {chan[1]} — {len(members):,d}👥\n\n"
    if not channels:
        reply.text += "<Empty List>"
    else:
        reply.text += f"Total: {len(ids):,d}👥"
    bot.rpc.send_msg(accid, event.msg.chat_id, reply)


@cli.on(events.NewMessage(command="/remove"))
def _remove(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    bot.rpc.markseen_msgs(accid, [event.msg.id])
    if cli.get_admin_chat(bot.rpc, accid) != event.msg.chat_id:
        send_help(bot, accid, event.msg.chat_id)
        return

    bot.rpc.delete_chat(accid, int(event.payload))
    reply = MsgData(text="✅ Removed", quoted_message_id=event.msg.id)
    bot.rpc.send_msg(accid, event.msg.chat_id, reply)


@cli.on(events.NewMessage(command="/update"))
def _update(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    bot.rpc.markseen_msgs(accid, [event.msg.id])
    if cli.get_admin_chat(bot.rpc, accid) != event.msg.chat_id:
        send_help(bot, accid, event.msg.chat_id)
        return

    try:
        args = event.payload.split(maxsplit=1)
        chanid = int(args[0].strip())
        data = json.loads(bot.rpc.get_draft(accid, chanid).text)
        new_data = json.loads(args[1] if len(args) == 2 else "{}")
    except Exception as ex:
        reply = MsgData(text=f"❌ Error: {ex}", quoted_message_id=event.msg.id)
        bot.rpc.send_msg(accid, event.msg.chat_id, reply)
        bot.logger.exception(ex)
        return

    def changed(key: str) -> bool:
        return new_data.get(key, data.get(key)) != data.get(key)

    if changed("deleteTimer"):
        bot.rpc.set_chat_ephemeral_timer(accid, chanid, new_data.get("deleteTimer"))
    if changed("name"):
        bot.rpc.set_chat_name(accid, chanid, new_data.get("name"))
    if changed("description"):
        bot.rpc.set_chat_description(accid, chanid, new_data.get("description"))

    if event.msg.file:
        bot.rpc.set_chat_profile_image(accid, chanid, event.msg.file)

    # save channel metadata
    data.update(new_data)
    bot.rpc.misc_set_draft(accid, chanid, json.dumps(data), None, None, None, None)

    bot.rpc.send_msg(
        accid,
        event.msg.chat_id,
        MsgData(text="✅ Updated", quoted_message_id=event.msg.id),
    )


@cli.on(events.NewMessage(command="/relays"))
def _relays(bot: Bot, accid: int, event: NewMsgEvent) -> None:
    bot.rpc.markseen_msgs(accid, [event.msg.id])
    if cli.get_admin_chat(bot.rpc, accid) != event.msg.chat_id:
        send_help(bot, accid, event.msg.chat_id)
        return

    key = "configured_addr"
    addrs = [r.addr for r in bot.rpc.list_transports(accid)]

    if event.payload and event.payload in addrs:
        bot.rpc.set_config(accid, key, event.payload)

    main_addr = bot.rpc.get_config(accid, key)
    reply = MsgData(text="Relays:\n\n", quoted_message_id=event.msg.id)
    for addr in addrs:
        reply.text += f"\n\n {addr}"
        if addr == main_addr:
            reply.text += " (main)"

    bot.rpc.send_msg(accid, event.msg.chat_id, reply)
