import requests
import time

TOKEN = "YOUR TELEGRAM BOT TOKEN HERE"

URL = f"https://api.telegram.org/bot{TOKEN}/"

def get_updates(offset=None):
    params = {"timeout": 100, "offset": offset}
    response = requests.get(URL + "getUpdates", params=params)
    return response.json()

def send_message(chat_id, text):
    requests.get(URL + "sendMessage", params={"chat_id": chat_id, "text": text})

def main():
    print("Initialized bot, send him a message and he will return your ID.")
    last_update_id = None

    while True:
        updates = get_updates(last_update_id)
        if updates.get("result"):
            for update in updates["result"]:
                chat_id = update["message"]["chat"]["id"]
                username = update["message"]["from"].get("username", "No username")
                first_name = update["message"]["from"].get("first_name", "")
                send_message(chat_id, f"👋 Hello {first_name} (@{username})\nYour ID is: {chat_id}")
                last_update_id = update["update_id"] + 1
        time.sleep(1)

if __name__ == "__main__":
    main()

