import json
import logging
from base64 import b64decode, b64encode
from subprocess import PIPE, Popen

from rich.logging import RichHandler

from . import apns
from . import ids
from . import imessage

import trio
import feedparser

from datetime import datetime
import urllib.request

logging.basicConfig(
    level=logging.NOTSET, format="%(message)s", datefmt="[%X]", handlers=[RichHandler()]
)

# Set sane log levels
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("py.warnings").setLevel(logging.ERROR)  # Ignore warnings from urllib3
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("jelly").setLevel(logging.INFO)
logging.getLogger("nac").setLevel(logging.INFO)
logging.getLogger("apns").setLevel(logging.INFO)
logging.getLogger("albert").setLevel(logging.INFO)
logging.getLogger("ids").setLevel(logging.DEBUG)
logging.getLogger("bags").setLevel(logging.INFO)
logging.getLogger("imessage").setLevel(logging.INFO)

logging.captureWarnings(True)

process = Popen(["git", "rev-parse", "HEAD"], stdout=PIPE) # type: ignore
(commit_hash, err) = process.communicate()
exit_code = process.wait()
commit_hash = commit_hash.decode().strip()

# Try and load config.json
try:
    with open("config.json", "r") as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    CONFIG = {}


try:
    with open("settings.json", "r") as s:
        SETTINGS = json.load(s)
except FileNotFoundError:
    SETTINGS = {}

# Re-register if the commit hash has changed
##if CONFIG.get("commit_hash") != commit_hash:
##    logging.warning("pypush commit is different, forcing re-registration...")
##    CONFIG["commit_hash"] = commit_hash
##    if "id" in CONFIG:
##        del CONFIG["id"]




def safe_b64decode(s):
    try:
        return b64decode(s)
    except:
        return None

async def main():
    token = CONFIG.get("push", {}).get("token")
    if token is not None:
        token = b64decode(token)
    else:
        token = b""

    push_creds = apns.PushCredentials(
        CONFIG.get("push", {}).get("key", ""), CONFIG.get("push", {}).get("cert", ""), token)

    async with apns.APNSConnection.start(push_creds) as conn:
        await conn.set_state(1)
        await conn.filter(["com.apple.madrid"])

        user = ids.IDSUser(conn)
        user.auth_and_set_encryption_from_config(CONFIG)

        # Write config.json
        CONFIG["encryption"] = {
            "rsa_key": user.encryption_identity.encryption_key,
            "ec_key": user.encryption_identity.signing_key,
        }
        CONFIG["id"] = {
            "key": user._id_keypair.key,
            "cert": user._id_keypair.cert,
        }
        CONFIG["auth"] = {
            "key": user._auth_keypair.key,
            "cert": user._auth_keypair.cert,
            "user_id": user.user_id,
            "handles": user.handles,
        }
        CONFIG["push"] = {
            "token": b64encode(user.push_connection.credentials.token).decode(),
            "key": user.push_connection.credentials.private_key,
            "cert": user.push_connection.credentials.cert,
        }

        with open("config.json", "w") as f:
            json.dump(CONFIG, f, indent=4)

        im = imessage.iMessageUser(conn, user)
        current_effect: str | None = None
        current_participants: list[str] = []

        # Send a message to myself
        async with trio.open_nursery() as nursery:
            nursery.start_soon(input_task, im, current_participants, current_effect)
            nursery.start_soon(output_task, im, current_participants, current_effect)

async def input_task(im: imessage.iMessageUser, current_participants: list[str], current_effect: str):
    #current_effect: str | None = None
    #current_participants: list[str] = []

    def is_cmd(cmd_str: str, name: str) -> bool:
        return cmd_str in [name, name[0]] or cmd_str.startswith(f"{name} ") or cmd_str.startswith(f"{name[0]} ")

    def get_parameters(cmd: str, err_msg: str) -> list[str] | None:
        sections: list[str] = cmd.split(" ")
        if len(sections) < 2 or len(sections[1]) == 0:
            print(err_msg)
            return None
        else:
            return sections[1:]

    def fixup_handle(handle: str) -> str:
        if handle.startswith("tel:+") or handle.startswith("mailto:"):
            return handle
        elif handle.startswith("tel:"):
            return "tel:+" + handle[4:]
        elif handle.startswith("+"):
            return "tel:" + handle
        elif handle[0].isdigit():
            # if the handle is 10 digits, assume it's a US number
            if len(handle) == 10:
                return "tel:+1" + handle
            # otherwise just assume it's a full phone number
            return "tel:+" + handle
        else: # assume it's an email
            return "mailto:" + handle

    while True:
        if (cmd := await trio.to_thread.run_sync(input, ">> ", cancellable=True)) == "":
            continue

        if is_cmd(cmd, "help"):
            print("help (h): show this message")
            print("quit (q): quit")
            print("filter (f) [recipient]: set the current chat")
            print("note: recipient must start with tel: or mailto: and include the country code")
            print("effect (e): adds an iMessage effect to the next sent message")
            print("handle [handle]: set the current handle (for sending messages with)")
            print("\\: escape commands (will be removed from message)")
            print("all other commands will be treated as message text to be sent")
        elif is_cmd(cmd, "quit"):
            exit(0)
        elif is_cmd(cmd, "effect"):
            if (effect := get_parameters(cmd, "effect [effect namespace]")) is not None:
                print(f"next message will be sent with [{effect[0]}]")
                current_effect = effect[0]
        elif is_cmd(cmd, "filter"):
            # set the current chat
            if (participants := get_parameters(cmd, "filter [recipients]")) is not None:
                fixed_participants: list[str] = list(map(fixup_handle, participants))
                print(f"Filtering to {fixed_participants}")
                current_participants = fixed_participants
        elif is_cmd(cmd, "handle"):
            handles: list[str] = im.user.handles
            av_handles: str = "\n".join([f"\t{h}{' (current)' if h == im.user.current_handle else ''}" for h in handles])
            err_str: str = f"handle [handle]\nAvailable handles:\n{av_handles}"

            if (input_handles := get_parameters(cmd, err_str)) is not None:
                handle = fixup_handle(input_handles[0])
                if handle in handles:
                    print(f"Using {handle} as handle")
                    im.user.current_handle = handle
                else:
                    print(f"Handle {handle} not found")
        elif is_cmd(cmd, "typing"):
            if len(current_participants) > 0:
                await im.typing(current_participants)
            else:
                print("No chat selected")
        elif is_cmd(cmd, "typingoff"):
            if len(current_participants) > 0:
                await im.typing(current_participants, False)
            else:
                print("No chat selected")
        elif is_cmd(cmd, "options"):
            print("time")
            print("rss <espn|cnn|ars|macrumors>")
        elif len(current_participants) > 0:
            if cmd.startswith("\\"):
                cmd = cmd[1:]

            await im.send(imessage.iMessage.create(im, cmd, current_participants, current_effect))
            current_effect = None
        else:
            print("No chat selected")
            
def test_func():
    print("in test_func()")


    

async def process_msg(msg : imessage.Message, im: imessage.iMessageUser, current_effect: str):

    async def sendRssFeed(im: imessage.iMessageUser, current_effect: str, respondTo: str, name: str):
        if name == "cnn":
            url = "http://rss.cnn.com/rss/cnn_topstories.rss"
        elif name == "espn":
            url = "https://www.espn.com/espn/rss/news"
        elif name == "ars":
            url = "https://feeds.arstechnica.com/arstechnica/index"
        elif name == "macrumors":
            url = "https://feeds.macrumors.com/MacRumors-All"

        msg = getRssFeed(url)
        await sendResponseChunkedIfNeeded(im, current_effect, respondTo, msg)
            

    def getRssFeed(url: str) -> str:
        feed = feedparser.parse(url)
        print("Feed Title:", feed.feed.title)
        msg = feed.feed.title + "\n"
        for entry in feed.entries:
            msg += "-" + entry.title + " - " + entry.link + "\n"
        
        return msg

    async def sendResponseChunkedIfNeeded(im: imessage.iMessageUser, current_effect: str, respondTo: str, msg: str):
        maxLength = 3000
        msgPiece = msg[:maxLength]
        await im.send(imessage.iMessage.create(im, msgPiece, respondTo, current_effect))
        current_effect = None
        msg = msg[maxLength:]
        length = len(msg)
        while length > 0:
            msgPiece = msg[:maxLength]
            await im.send(imessage.iMessage.create(im, msgPiece, respondTo, current_effect))
            current_effect = None
            msg = msg[maxLength:]
            length = len(msg)
        
        

    async def downloadAndSendUrl(im: imessage.iMessageUser, current_effect: str, respondTo: str, pageUrl: str):
        apiKey = SETTINGS.get("extractorApiKey")
        if apiKey == "":
            print("apikey for extractor is missing")
            await sendResponseChunkedIfNeeded(im, current_effect, respondTo, "missing extractor API key")
        else:
            #print("api key is: " + apiKey)
            url = "https://extractorapi.com/api/v1/extractor/?apikey=" + apiKey + "&url=" + pageUrl + "&fields=title"
            with urllib.request.urlopen(url) as url:
                data = json.load(url)
                t = data["text"]
                #print("Got data: " + t)
                await sendResponseChunkedIfNeeded(im, current_effect, respondTo, t)
          
    
    #print("processing msg " + str(msg))
    s = msg.text.strip().lower()
    respondTo = [msg.sender]
    if s == "time":
        now = datetime.now()
        current_time = now.strftime("%m/%d/%Y, %H:%M:%S")
        await im.send(imessage.iMessage.create(im, current_time, respondTo, current_effect))
        current_effect = None
    elif s == "options":
        msg = "time\nrss <espn|cnn|ars|macrumors>\n<url>"
        await im.send(imessage.iMessage.create(im, msg, respondTo, current_effect))
        current_effect = None
    elif s.startswith("rss "):
        rssName = s[4:]
        await sendRssFeed(im, current_effect, respondTo, rssName)
    elif s.startswith("https://"):
        url = s
        await downloadAndSendUrl(im, current_effect, respondTo, url)
            
    else:
        print("unknown msg: " + s)

async def output_task(im: imessage.iMessageUser, current_participants: list[str], current_effect: str):
    
    while True:
        msg = await im.receive()
        print("MSG: " + str(msg))
        #print("about to process msg")
        #test_func()
        await process_msg(msg, im, current_effect)


def entrypoint():
    trio.run(main)


if __name__ == "__main__":
    entrypoint()
