import requests
from slack_sdk import WebClient

client = WebClient(token="xoxb-from-env")


def page(text: str):
    client.chat_postMessage(channel="#ops", text=text)
    requests.post("https://hooks.slack.com/services/T000/B000/XXXX", json={"text": text})
