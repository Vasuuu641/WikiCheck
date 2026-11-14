# Starting Aikakone on a new computer

You need:

- Linux (Ubuntu or Debian) or macOS. On Windows, use WSL (Ubuntu) and follow the Linux steps.
- About 5 GB of free disk space and an internet connection.
- Google Chrome.
- The `.env` file with the API keys. Ask Pacome for it. It holds secrets: never commit it, and never
  post it in a chat.

The first setup takes about 15 minutes. After that, starting takes one command.


## 1. Install the tools (once)

**Ubuntu or Debian**

```sh
sudo apt update && sudo apt install -y git make curl zstd python3-venv gcc libmariadb-dev
curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup | sudo bash -s -- --mariadb-server-version=mariadb-11.8
sudo apt install -y mariadb-server
```

The second line adds MariaDB's own package source. Ubuntu's built-in MariaDB is too old: Aikakone
needs version 11.7 or newer, for vector search.

**macOS** (with [Homebrew](https://brew.sh))

```sh
brew install git make python mariadb mariadb-connector-c ollama
brew services start mariadb
```


## 2. Get the code and the keys

```sh
git clone https://github.com/Vasuuu641/rewrite.git aikakone
cd aikakone
git checkout merge/all
```

Put the `.env` file you received in this `aikakone` folder.


## 3. Set up (once)

```sh
make db-user
make setup
```

- `make db-user` creates the database and its user. It asks for your computer password.
- `make setup` installs and downloads everything else: about 2 GB, 10 to 15 minutes. If it stops
  halfway, run it again: it skips what is already done.


## 4. Start

```sh
make run
```

Leave this terminal open: it is the server. Check it in a browser: http://127.0.0.1:8020/drift
should show the Opinion drift page.

To test everything at once, open a second terminal in the same folder and run `make smoke`. It
should end with `all passed`.


## 5. Add the extension to Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked**, then choose the `extension` folder inside `aikakone`.
4. Pin Aikakone: puzzle icon in the toolbar, then the pin next to Aikakone.

Try it: open https://sv.wikipedia.org/wiki/Regeringen_Aura_II and click the Aikakone icon. Other
good pages: https://en.wikipedia.org/wiki/Hauho (Missing here) and
https://fi.wikipedia.org/wiki/Talvisota (Drift).


## Next time

```sh
cd aikakone
make run
```

MariaDB starts by itself when the computer starts.


## If something goes wrong

| What you see | What to do |
|---|---|
| The popup says the server did not answer | `make run` is not running. Start it, then reopen the popup. |
| `make db-user`: "command not found" or "Can't connect" | MariaDB is not installed or not running. Linux: `sudo systemctl start mariadb`. macOS: `brew services start mariadb`. |
| `make setup` stops at `pip install ... mariadb` | The MariaDB C library is missing: `sudo apt install libmariadb-dev gcc` (Linux) or `brew install mariadb-connector-c` (macOS). Then `make setup` again. |
| `make setup` stops at "Ollama" | Linux: `sudo apt install zstd`, then `make setup` again. macOS: `brew install ollama`. |
| Answers or checks fail with "GEMINI_API_KEY" or "LLM_" | The `.env` file is missing or incomplete. Put the one you received in the `aikakone` folder. |
| Port 8020 is already in use | A server is already running: use it, or stop the old one. |
| You cannot install MariaDB 11.7+, but you have Docker | Run `make db-docker` and set `DB_PORT=3307` in `.env`. Skip `make db-user`, then run `make setup`. |
