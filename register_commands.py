#!/usr/bin/env python3
"""
BrickMargin · register the Discord slash commands
=================================================

Run once from the Actions tab ("Register Discord commands"), and again only
if the commands change. It tells Discord about /price, /track, /tracks and
/untrack; Discord then sends every use of them to
https://www.brickmargin.com/api/discord/interactions, which answers them.

Environment: DISCORD_APP_ID     Developer Portal → General Information → Application ID
             DISCORD_BOT_TOKEN  Developer Portal → Bot → Reset Token
"""
import os, sys
import requests

VERSION = "1.0"

APP_ID = (os.environ.get("DISCORD_APP_ID") or "").strip()
TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()

SET = {"type": 3, "name": "set", "description": "Set number or name, like 75192 or Millennium Falcon",
       "required": True, "autocomplete": True}
GUILD_ONLY = {"contexts": [0], "integration_types": [0]}      # only inside a server

COMMANDS = [
    {"name": "price", "description": "What a LEGO set is worth, and the best deal on it right now",
     "options": [SET], **GUILD_ONLY},
    {"name": "track", "description": "Get pinged when a set hits your price",
     "options": [
         SET,
         {"type": 3, "name": "alert", "description": "When should I ping you?", "required": True,
          "choices": [{"name": "Any deal", "value": "any"},
                      {"name": "At or under a price", "value": "price"},
                      {"name": "At least a % off", "value": "percent"}]},
         {"type": 10, "name": "price", "description": "Your price in dollars (for 'At or under a price')",
          "required": False, "min_value": 1},
         {"type": 4, "name": "percent", "description": "Minimum % off (for 'At least a % off')",
          "required": False, "min_value": 5, "max_value": 90},
     ], **GUILD_ONLY},
    {"name": "tracks", "description": "Show the sets you're tracking", **GUILD_ONLY},
    {"name": "untrack", "description": "Stop tracking a set",
     "options": [{**SET, "description": "The set to stop tracking"}], **GUILD_ONLY},
]


def main():
    print(f"BrickMargin command registration — version {VERSION}")
    if not APP_ID.isdigit():
        sys.exit("ERROR: DISCORD_APP_ID is missing or not a number "
                 "(Developer Portal → General Information → Application ID).")
    if not TOKEN:
        sys.exit("ERROR: DISCORD_BOT_TOKEN is missing (Developer Portal → Bot → Reset Token).")
    r = requests.put(f"https://discord.com/api/v10/applications/{APP_ID}/commands",
                     headers={"Authorization": f"Bot {TOKEN}"}, json=COMMANDS, timeout=30)
    if r.status_code == 401:
        sys.exit("ERROR: Discord rejected the bot token (401). Reset it in the Developer Portal "
                 "and update the DISCORD_BOT_TOKEN secret.")
    if r.status_code >= 300:
        sys.exit(f"ERROR: Discord answered {r.status_code}: {r.text[:300]}")
    print("Registered: " + ", ".join("/" + c["name"] for c in r.json()))
    print("\nAdd the bot to your server — open this link and pick the BrickMargin server:")
    print(f"https://discord.com/oauth2/authorize?client_id={APP_ID}&scope=applications.commands%20bot&permissions=0")


if __name__ == "__main__":
    main()
