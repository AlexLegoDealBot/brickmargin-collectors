#!/usr/bin/env python3
"""
BrickMargin · register the Discord slash commands
=================================================

Run once from the Actions tab ("Register Discord commands"), and again only
if the commands change. It tells Discord about /price, /track, /tracks,
/untrack and the staff-only /tracking; Discord then sends every use of them to
https://www.brickmargin.com/api/discord/interactions, which answers them.

Environment: DISCORD_APP_ID     Developer Portal → General Information → Application ID
             DISCORD_BOT_TOKEN  Developer Portal → Bot → Reset Token
"""
import os, sys
import requests

VERSION = "1.3"

APP_ID = (os.environ.get("DISCORD_APP_ID") or "").strip()
TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()

SET = {"type": 3, "name": "set", "description": "Set number or name, like 75192 or Millennium Falcon",
       "required": True, "autocomplete": True}
GUILD_ONLY = {"contexts": [0], "integration_types": [0]}      # only inside a server

COMMANDS = [
    {"name": "price", "description": "What a LEGO set is worth, and the best deal on it right now",
     "options": [SET], **GUILD_ONLY},
    # Three subcommands, so each one shows exactly the boxes it needs (all
    # required). Version 1.0 had one /track with optional price and percent
    # boxes that Discord hides behind "+ more": people picked "At or under a
    # price" and then had nowhere to type it. Amounts are text, so "$650",
    # "1,200" and "25%" all work; the bot reads them.
    {"name": "track", "description": "Get pinged when a set hits your price",
     "options": [
         {"type": 1, "name": "price", "description": "Ping me when a set is at or under my price",
          "options": [SET, {"type": 3, "name": "max", "description": "Your price in dollars, like 650",
                            "required": True, "max_length": 12}]},
         {"type": 1, "name": "percent", "description": "Ping me when a set is at least this % off",
          "options": [SET, {"type": 3, "name": "off", "description": "Percent off, from 5 to 90, like 25",
                            "required": True, "max_length": 10}]},
         {"type": 1, "name": "any", "description": "Ping me for any deal on a set", "options": [SET]},
     ], **GUILD_ONLY},
    {"name": "tracks", "description": "Show the sets you're tracking", **GUILD_ONLY},
    # Staff only. "32" is Discord's Manage Server permission: members without it
    # never see this command (the bot checks again when it runs).
    {"name": "tracking", "description": "Staff only: what members are tracking and which alerts fired",
     "default_member_permissions": "32", **GUILD_ONLY},
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
    # GitHub blanks out every secret's value in this log (it prints ***), and
    # the Application ID is a secret here, so a ready-made link would arrive
    # broken ("Invalid Form Body"). Print the link with a slot instead.
    print("\nIf the bot isn't in your server yet: copy this link, replace YOUR_APP_ID with the Application ID")
    print("(Developer Portal → General Information), open it and pick the BrickMargin server:")
    print("https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=applications.commands%20bot&permissions=0")


if __name__ == "__main__":
    main()
