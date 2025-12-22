Ticket Mastery Bot

A modern Discord ticket utility bot focused on simple, reliable support workflows. Built from the ground up after experimenting with existing solutions, Ticket Mastery is designed to help communities manage support tickets efficiently and cleanly.

⸻

Requirements
	•	Python 3.12+
	•	discord.py 2.x
	•	A Discord bot application with required intents enabled

This project targets Python 3.12+. Older Python versions are not officially supported.

⸻

Installation

pip install -r requirements.txt


⸻

Environment Setup

Create a .env file in the project root:

DISCORD_TOKEN=your_bot_token_here


⸻

Automatic File Creation

On first run, the bot will automatically generate the following files if they do not exist:
	•	guild_configs.json – per-server configuration storage
	•	opened_tickets.json – active ticket tracking

No manual setup is required for these files.

⸻

Features
	•	Button-based ticket panels
	•	Multiple ticket categories
	•	Staff role integration
	•	Ticket claiming & closing
	•	Automatic inactivity closing (48h)
	•	Ticket transcripts
	•	Logging to a dedicated channel
	•	Persistent views across restarts

⸻

Logging

All ticket closures (manual or automatic) can be logged to a designated log channel, including optional transcript files.

⸻

Hosting

This bot is hosted using Roverdev

Support Server: https://discord.gg/roverdev

⸻

License

MIT License — see LICENSE file for details.

⸻

Contributing

Contributions are welcome.
	•	Fork the repository
	•	Create a feature branch
	•	Submit a pull request with clear changes

Please keep code style consistent and target Python 3.12+.
