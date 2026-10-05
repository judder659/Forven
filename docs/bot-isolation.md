# Running bots as a separate Windows account

Bots never receive Forven's master encryption key: each spawn gets a one-off
token, and the API hands it only its own provider login. By default, though,
bots still run as your own Windows user. A compromised bot could then read
anything you can, including the key file, your browser profile and Forven's own
code.

This optional setup runs every bot as a dedicated, low-privilege local account
instead. It is off until you turn it on, and nothing else about your install
changes.

## Turning it on

From an **administrator** PowerShell, opened as the same Windows user that runs
Forven, in the Forven folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-bot-user.ps1
```

The script:

1. Creates a local account `forven-bot` with a long random password that nobody
   needs to know, hidden from the sign-in screen.
2. Grants it read-and-run access to Forven's code and Python (no writes), and
   read-write access to Forven's data folder (`~\.forven`): the database, market
   data and bot memory.
3. Explicitly denies it the master key (both locations) and `.env` files.
4. Stores the password, encrypted with Forven's key, beside that key in
   `%LOCALAPPDATA%\Forven`, where bots cannot read or delete it.
5. Runs `python -m forven bot-account check`, which starts a probe as the
   account and confirms it really runs as `forven-bot`, can load Forven and open
   the database, and cannot read any secret file. If the check fails the setup
   is cleared again and bots keep running as you.

Each bot switches to the account the next time it starts. Restart running bots
from the Bot Factory page to switch them straight away.

## Checking and undoing

```powershell
python -m forven bot-account status   # On / Off / Broken
python -m forven bot-account check    # re-run the probe
.\scripts\setup-bot-user.ps1 -Remove  # admin: delete the account and its permissions
```

`python -m forven bot-account clear` turns isolation off without deleting the
account.

## How it behaves

- Fail-closed: once set up, a bot that cannot start as the account is not
  started at all. It never quietly falls back to your user.
- Each isolated bot runs inside a Windows Job Object created by the API before
  the bot runs any code. Stop, kill-all, the heartbeat watchdog and shutdown end
  the bot (and anything it started) through that job, including after an API
  restart. The bot cannot leave the job.
- Bots get a private profile and temp folder under `~\.forven\bot-runtime`.
- Isolated bots are stopped immediately on shutdown rather than being given the
  few seconds' graceful drain, because Windows cannot send them a console
  break across accounts.

## What it does not cover yet

- Bots still read and write the shared database directly, so a compromised bot
  could change settings stored there. Closing that means moving bot writes
  behind the API.
- The API trusts any program on this PC that connects over localhost unless
  `FORVEN_API_KEY` and `FORVEN_OPERATOR_KEY` are set.
- Live bots still receive their Hyperliquid credentials as environment
  variables from the API.
- Windows only. On macOS and Linux the setup is refused.
