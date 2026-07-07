#!/usr/bin/env python3
"""One-shot: approve an existing Hyperliquid agent wallet to trade on behalf
of the main wallet. Must be signed by the MAIN wallet (the one holding funds).

Usage:
    # Set the main wallet's private key, then run:
    export FORVEN_HL_MASTER_SECRET="0x..."
    python scripts/approve_hyperliquid_agent.py

Config is read from Forven's KV store (the same settings the app uses).
"""

import json
import os
import sys

# Make forven importable from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forven.db import kv_get
from forven.config import load_config

from eth_account import Account
from hyperliquid.exchange import Exchange, sign_agent
from hyperliquid.utils import constants

# ---- Load settings -----------------------------------------------------------
load_config()
settings = kv_get("forven:settings", {}) or {}
testnet = bool(settings.get("hyperliquid_testnet", True))
base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
main_wallet = str(settings.get("hyperliquid_wallet", "") or "").strip()
agent_wallet = str(settings.get("hyperliquid_api_address", "") or "").strip()
network = "testnet" if testnet else "mainnet"

print(f"=== Hyperliquid Agent Approval ({network}) ===")
print(f"Main wallet:  {main_wallet}")
print(f"Agent to approve: {agent_wallet}")

if not main_wallet or not agent_wallet:
    print("ERROR: main wallet or agent wallet not configured in Settings.")
    sys.exit(1)

# ---- Main wallet private key (NOT the API key!) ------------------------------
master_secret = os.environ.get("FORVEN_HL_MASTER_SECRET", "") or os.environ.get("HL_MASTER_SECRET", "")
if not master_secret:
    print()
    print("ERROR: FORVEN_HL_MASTER_SECRET env var not set.")
    print("This must be the PRIVATE KEY of the MAIN wallet (0x3F8e...),")
    print("NOT the API agent key you use for daily order signing.")
    print()
    print("  export FORVEN_HL_MASTER_SECRET=\"0x...\"")
    sys.exit(1)

master_secret = master_secret.strip()
if not master_secret.startswith("0x"):
    master_secret = "0x" + master_secret

# ---- Verify the derived address matches --------------------------------------
try:
    master_account = Account.from_key(master_secret)
except Exception as e:
    print(f"ERROR: Invalid private key: {e}")
    sys.exit(1)

derived = master_account.address
if derived.lower() != main_wallet.lower():
    print(f"WARNING: The private key you provided derives to {derived}")
    print(f"         but the configured main wallet is {main_wallet}")
    resp = input("Continue anyway? [y/N]: ").strip().lower()
    if resp != "y":
        sys.exit(1)
    print()

# ---- Build the Exchange with the MAIN wallet ---------------------------------
exchange = Exchange(
    wallet=master_account,
    base_url=base_url,
)

# ---- Approve the EXISTING agent ----------------------------------------------
# We use sign_agent directly to APPROVE AN EXISTING agent address, rather
# than Exchange.approve_agent() which generates a random new key.
import time

timestamp = int(time.time() * 1000)
action = {
    "type": "approveAgent",
    "agentAddress": agent_wallet,
    "agentName": "Forven",
    "nonce": timestamp,
}
sig = sign_agent(master_account, action, is_mainnet=not testnet)
result = exchange._post_action(action, sig, timestamp)

print()
print("Approval result:", result)
print()
print("✓ Agent approved! New live trades should now execute successfully.")
print("  Wait for the next daemon scan cycle (~1 min) and check all-trades.")
