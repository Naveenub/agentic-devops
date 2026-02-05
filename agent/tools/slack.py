from slack_sdk import WebClient
import os

client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

def send_message(channel, text):
    client.chat_postMessage(channel=channel, text=text)
