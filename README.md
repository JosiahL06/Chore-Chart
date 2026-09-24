# Chore Chart

A small self-hosted chore tracker for the house — daily, weekly, monthly, and yearly tasks grouped by area, with checkboxes that sync across every device.

## What it is

Chore Chart is a shared checklist for the whole household. Chores are grouped by where they happen — kitchen, bathroom, living room, etc.,  — and split into four cycles: daily, weekly, monthly, and yearly. Check something off on your phone and it shows up on a wall tablet a moment later.

It's a simple app meant to just work at home; there are no accounts, everyone in the house can open it in a browser, and it's meant for a trusted home network.

## Key features

- **Four cycles that reset themselves** — daily, weekly, monthly, and yearly tabs. When the period rolls over (tomorrow, next Monday, the 1st, Jan 1), the checkmarks come up fresh on their own. No one has to remember to hit "reset."
- **Grouped by area** — chores are organized by where they happen (Kitchen, Bathrooms, Pool, Lawn, Pets, …), so whoever's in a room can see what that room needs.
- **Editable right in the browser** — use the **Edit** button to add, rename, reorder, move between cycles, or remove chores. Your list starts with a sensible default set and becomes your household's own.
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

All you need is Python — there are no dependencies to install:

```bash
git clone https://github.com/JosiahL06/Chore-Chart.git
cd Chore-Chart
HTML_DIR=$PWD/html DATA_DIR=$PWD/data python3 app/server.py
```

Then open it at **http://localhost:8080**.

## Using it

- **Tap a chore** to check it off. It saves automatically and syncs to every other open device.
- **Edit** (top right) to add, rename, reorder, move, or remove chores. Removing keeps your past checkmarks.
- **Resets happen on their own** once the day/week/month/year rolls over. The reset buttons just clear a section early if you want a clean slate right now.

## For developers

Architecture, the JSON API, CI details, backups, and migration notes live in [`docs/technical.md`](docs/technical.md). What changed between releases is in [`CHANGELOG.md`](CHANGELOG.md), and the living task list is [`TODO.md`](TODO.md).

## License

[GNU Affero General Public License v3.0](LICENSE) — AGPL-3.0-only. You're free to run, modify, and share it, including hosting it for your own household.

Because this is a network service, the AGPL's network clause (§13) applies on top of the usual copyleft: if you run a **modified** version and let other people interact with it over a network, you have to offer those users the corresponding source. Running it unmodified for your own house carries no such obligation.

No warranty. By contributing, you agree your contributions are licensed under the same terms. This just makes sure that the project remains open source for everyone.
