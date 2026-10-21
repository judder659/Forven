# Bot sandbox (Windows)

Bots never receive Forven's master encryption key: each spawn gets a one-off
token, and the API hands it only its own provider login. They still run as your
Windows user, though, so on Windows Forven also starts every bot inside a
**low-integrity sandbox**, the same Windows mechanism browsers use for their
sandboxes. There's nothing to set up and no extra account. It is on by default.

## What a sandboxed bot can and cannot do

| | Sandboxed bot |
|---|---|
| Read and write Forven's data folder (`~\.forven`): market data, its memory | Yes |
| Open Forven's database (`forven.db`) | **No**, while only bots started by this version run; it reaches Forven through the API instead |
| Hold exchange keys | **No**: Forven places a live bot's orders for it, with its own keys |
| Reach the API (credential broker, health) and the internet | Yes |
| Change Forven's code, Python, or anything else in your profile | **No** |
| Read the master key (in `%LOCALAPPDATA%\Forven` or `~\.forven`) or `.env` files | **No** |

Bots get a private profile and temp folder under `~\.forven\bot-runtime`.

## How it is set up

The first time a bot starts after an API start, Forven:

1. Labels `~\.forven` so low-integrity processes may write it. This one-off
   pass can take a moment on a large data folder; files created later inherit
   the label.
2. Labels the key's folder, any legacy key file and `.env` files as unreadable
   from low integrity.
3. Starts a short probe in the sandbox. It confirms the probe ran at low
   integrity, can load Forven and write the database, can't write the code
   folder, can't read any protected file, and can't read a file labelled the
   way a closed database is.

Then, whenever a bot starts or stops, Forven closes the database file to the
sandbox if every running bot reaches Forven only through the API (its reads,
writes, AI calls and, for a live bot, its orders), and opens it again while a
bot started by an older version is still running.

## Live bots

A live bot never receives exchange keys. When it decides to open or close a
position, it asks Forven, which reads the bot's settings from its own
database, runs the same live gate stack as the strategy scanner (kill switch,
budgets, the bot's GO LIVE ceiling, a mandatory stop) and places the order
with its own keys. A bot can only close its own live positions. An order is
sent once; if the answer is lost, the bot sees the position on its next tick.

If the probe shows a bot could not work in the sandbox, or the sandbox cannot be
created, bots start the way they always have, and the activity log says why. The
sandbox never stops a bot from starting.

## Checking and turning it off

```powershell
python -m forven bot-sandbox   # prepare and run the check now
```

Set `FORVEN_BOT_SANDBOX=0` in Forven's environment to turn it off, or
`FORVEN_BOT_DB_LOCK=0` to keep the sandbox but leave the database open to bots.

## What it does not cover yet

- Bots can still *read* most of your profile, though not the key or `.env`
  files. A separate Windows account would close that, at the cost of a setup
  step.
- The API trusts any program on this PC that connects over localhost unless
  `FORVEN_API_KEY` and `FORVEN_OPERATOR_KEY` are set.
- With `FORVEN_BOT_WRITES_VIA_API=0`, live bots place their own orders as
  before and receive their Hyperliquid credentials from the API.
- Windows only. On macOS and Linux bots start as before.
