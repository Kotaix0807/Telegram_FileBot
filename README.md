# Telegram-FileBot

A Telegram bot coded in **Python** with **GPT** integration for managing documents and photos on a personal Linux server.

---

## Overview

**Telegram-FileBot** is a personal assistant that allows you to interact with your personal server directly from Telegram.  
You can send files, photos, or even manage directories, all through simple Telegram messages.  
The way you manage files is by creating two folders: **Photos** and **Documents**.  
There are specific commands for both folders for simplicity, practicality, and security reasons.  

Example:  
- `/mkdirp` → Make a directory **ONLY** in the **"Pictures"** folder.  
- `/mkdird` → Make a directory **ONLY** in the **"Documents"** folder.  

---

## Features

- **Receive and save files** directly from Telegram  
- **Receive photos** and store them in your server  
- **Manage folders and files** (list, delete, rename, move, etc.)  
- **Show and send saved photos or files**

---

## Technical Details

- Written in **Python 3**  
- Uses the **python-telegram-bot** (or TeleBot) library  
- Maximum upload size: **20 MB** (Telegram API limit)  
- **Linux only** (not tested on Windows)  
- **Personal use only** — not meant for public hosting or file sharing (but you can modify it however you like!)  
- **Persistent service**: you can create a `.service` file to make the bot start automatically on boot  

---

## Commands List

| Command | Description |
|----------|--------------|
| `/listp` | List photos and folders |
| `/listd` | List documents and folders |
| `/show <name>` | Show image or active selection |
| `/showd <name>` | Search and show document |
| `/gop <dir>` | Change photo folder |
| `/god <dir>` | Change document folder |
| `/rmp <name>` | Delete photos or folders |
| `/rmd <name>` | Delete documents or folders |
| `/mvp` | Move photos or folders with assistant |
| `/mvd` | Move documents or folders with assistant |
| `/renamep` | Rename photo |
| `/renamed` | Rename document |
| `/mkdirp <path>` | Create photos folder |
| `/mkdird <path>` | Create documents folder |
| `/time` | Show current time |
| `/reboot` | Reboot server |

*(Command names may vary depending on your configuration.)*

---

## 🔧 Installation

### 1. Clone the repository
```bash
git clone https://github.com/Kotaix0807/Telegram-FileBot
cd Telegram-FileBot
```

### 2. Create Python virtual environment
You can skip this step at your own risk (not recommended).  
```bash
python3 -m venv myvenv       # Create the virtual environment
source myvenv/bin/activate   # Activate the virtual environment
```

### 3. Install dependencies
```bash
pip install --upgrade pip           # Update pip
pip install -r requirements.txt     # Install all dependencies for this env
```

---

## 4. Link with Telegram

### Create a bot

1. Create an account in Telegram.  
2. Find **"BotFather"** and start a chat by sending `/start`.  
3. Send the command `/newbot` and set a name.  
4. Set your bot username — **remember**: it must **not contain spaces** and **must end with the word "bot"**.  
5. Once you finish, BotFather will give you a **token**.  

Keep this token safe — **do not share it** unless you know what you are doing.  
It will look like this:  
`123456778120:ABcdEfghio_JAISJEORPAIEHOLAMunDOAHSJDKSD-h9`

---

### Get your User ID

*If you already know your Telegram User ID or have another bot that provides it, skip this step.*

1. Open the file **`getID.py`** with your preferred text editor (VSCode, `vi`, `vim`, `nano`, etc.).  
2. Locate the following line and replace it with your token:
   ```python
   TOKEN = "YOUR TELEGRAM BOT TOKEN HERE"
   ```
3. Activate your virtual environment (created in step 2):
   ```bash
   source myvenv/bin/activate
   ```
4. Run the program to obtain your User ID:
   ```bash
   python3 getID.py
   ```
5. Send a message to your bot, and it should answer something like:
   ```
   👋 Hello <you> (@yourusername)
   Your ID is: 1234567890
   ```
6. Copy your ID and save it somewhere safe.  
   Then edit **`TeleBot_<your_preferred_language>.py`** and replace the following lines with your data:
   ```python
   TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")       # <-- TOKEN GOES HERE
   AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "YOUR_USER_ID"))  # <-- ID GOES HERE
   ```

---

### 5. Set your main path

When configuring this path, keep in mind that it will be used to upload and save your files.  
For example, if you set the path to:
```
/home/user/TelebotData
```
The program will automatically create folders **`Pictures`** and **`Documents`** inside **`TelebotData`**.  

Keeping this in mind, look for this line in **`TeleBot_<your_preferred_language>.py`** and update it:
```python
BASE_SAVE_PATH = Path(os.getenv("SAVE_PATH", "YOUR/SAVE/FOLDER")).expanduser()
```

---

**Done!**  
Now you’re ready to start your personal Telegram FileBot
