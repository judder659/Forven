# Bot sandbox (Windows)

Bots never receive Forven's master encryption key: each spawn gets a one-off
token, and the API hands it only its own provider login. They still run as your
Windows user, though, so on Windows Forven also starts every bot inside a
**low-integrity sandbox**, the same Windows mechanism browsers use for their
sandboxes. There's nothing to set up and no extra account. It is on by default.

## What a sandboxed bot can and cannot do

| | Sandboxed bot |
|---|---|
| Read and write Forven's data folder (`~\.forven`): database, market data, its memory | Yes |
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
   folder, and can't read any protected file.

If the probe shows a bot could not work in the sandbox, or the sandbox cannot be
created, bots start the way they always have, and the activity log says why. The
sandbox never stops a bot from starting.

## Checking and turning it off

```powershell
python -m forven bot-sandbox   # prepare and run the check now
```

Set `FORVEN_BOT_SANDBOX=0` in Forven's environment to turn it off.

## What it does not cover yet

- Bots can still *read* most of your profile, though not the key or `.env`
  files. A separate Windows account would close that, at the cost of a setup
  step.
- Bots read and write the shared database directly, so a compromised bot could
  change settings stored there. Closing that means moving bot writes behind the
  API.
- The API trusts any program on this PC that connects over localhost unless
  `FORVEN_API_KEY` and `FORVEN_OPERATOR_KEY` are set.
- Live bots still receive their Hyperliquid credentials as environment
  variables from the API.
- Windows only. On macOS and Linux bots start as before.
