import os
from functools import lru_cache

from slack_sdk import WebClient


@lru_cache(maxsize=1)
def _client():
    return WebClient(token=os.getenv("SLACK_BOT_TOKEN"))


def send_message(channel, text):
    _client().chat_postMessage(channel=channel, text=text)
