# Chore Chart

A small self-hosted chore tracker for the house — recurring chores grouped by area, with day/week/month calendars and checkboxes that sync across every device.

## What it is

Chore Chart is a shared checklist for the whole household. Chores are grouped by where they happen — kitchen, bathroom, living room, etc.,  — each with its own repeat schedule (daily, weekly, monthly, yearly, or a one-off), shown on a day/week/month calendar. Check something off on your phone and it shows up on a wall tablet a moment later.

It's a simple app meant to just work at home; there are no accounts, everyone in the house can open it in a browser, and it's meant for a trusted home network.

## Key features

- **Per-chore schedules that reset themselves** — give each chore its own rule (every day, every 2 weeks on Mon/Thu, the last day of the month, …) and occurrence dates roll forward on their own. No one has to remember to hit "reset."
- **Grouped by area** — chores are organized by where they happen (Kitchen, Bathrooms, Pool, Lawn, Pets, …), so whoever's in a room can see what that room needs.
- **Editable right in the browser** — the **Chores** screen adds, reschedules, reorders, and removes chores, and every chore opens in an editor, so its schedule can be changed on any day (due or not). New installations start empty and become your household's own.
- **Syncs across devices** — every open device sees the same chart, so nobody doubles up on a chore someone else already finished. Progress shows in a bar at the top: *7 / 10 done*.
- **Keeps working if the server hiccups** — the last view is cached locally, so the chart still renders when the server is briefly unreachable.
- **Lightweight and dependency-free** — plain Python standard library on the back end, vanilla HTML/CSS/JS on the front end. No packages to install, no build step.

## Getting started

### With Docker (recommended)

Download the [Docker Compose template](docker-compose.yml), make whatever edits you need to fit it to your system, and run a one-line command to start the app:

```bash
docker compose up -d
```

Then open **http://localhost:8080** to see the chore chart. Your data is saved in the `./data` folder next to the compose file, so it survives restarts.

### From source

All you need is Python — there are no dependencies to install. Just run these commands in your terminal:

```bash
git clone https://github.com/JosiahL06/Chore-Chart.git
cd Chore-Chart
HTML_DIR=$PWD/html DATA_DIR=$PWD/data python3 app/server.py
```

Then open the app at **http://localhost:8080**.

If the server exits with `attempt to write a readonly database`, the `data/` folder is owned by root — usually from an earlier Docker run. Fix it once:

```bash
sudo chown -R "$USER" data
```

## Using it

- **Tap a chore** to check it off. It saves automatically and syncs to every other open device.
- **Switch to Chores** to add, rename, reschedule, reorder, or remove. Removing keeps your past checkmarks, and a chore's schedule is editable on any day, whether or not it's due.
- **Occurrences reset on their own** as each chore's schedule rolls forward — there are no reset buttons.

## For developers

Architecture, the JSON API, CI details, backups, and migration notes live in [`docs/technical.md`](docs/technical.md). What changed between releases is in [`CHANGELOG.md`](CHANGELOG.md), and the living task list is [`TODO.md`](TODO.md).

## License

[GNU Affero General Public License v3.0](LICENSE)
