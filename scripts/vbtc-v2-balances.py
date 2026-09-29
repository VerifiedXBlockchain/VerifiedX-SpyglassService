#!/usr/bin/env python3
"""Snapshot and compare vBTC V2 holder balances as Spyglass serves them.

A backfill rewrites derived rows, so its effect is checked from outside: take
a snapshot before, take one after, and read every difference. Then compare the
result with a Core node, which is the ledger Spyglass mirrors.

Usage:
  scripts/vbtc-v2-balances.py snapshot <mainnet|testnet> <file.json>
  scripts/vbtc-v2-balances.py diff <before.json> <after.json>
  scripts/vbtc-v2-balances.py node <file.json> --node http://<host>:<port> [--token-env NAME]

`diff` and `node` exit 1 when they find a difference. The node token is read
from the environment variable named by --token-env, never from the command line.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from decimal import Decimal

API = {
    "mainnet": "https://data.verifiedx.io/api",
    "testnet": "https://data-testnet.verifiedx.io/api",
}
BALANCE_FIELDS = ("addresses", "available_balances")
TOKEN_FIELDS = ("owner_address", "global_balance", "is_pending_withdrawal")


def get_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def snapshot(network, path):
    url = f"{API[network]}/btc/vbtc-v2/?limit=100"
    tokens = []
    while url:
        page = get_json(url)
        tokens += page["results"]
        url = page.get("next")
    with open(path, "w") as handle:
        json.dump(tokens, handle)
    holders = sum(len(token.get("addresses") or {}) for token in tokens)
    print(f"{len(tokens)} tokens, {holders} holder balances saved to {path}")
    return 0


def load(path):
    with open(path) as handle:
        return {token["sc_identifier"]: token for token in json.load(handle)}


def balances(tokens, field):
    return {
        (sc, address): Decimal(str(amount))
        for sc, token in tokens.items()
        for address, amount in (token.get(field) or {}).items()
    }


def diff(before_path, after_path):
    before, after = load(before_path), load(after_path)
    differences = 0
    for sc in sorted(set(before) ^ set(after)):
        differences += 1
        print(f"token {sc}: only in the {'first' if sc in before else 'second'} snapshot")
    for field in BALANCE_FIELDS:
        old, new = balances(before, field), balances(after, field)
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                differences += 1
                print(f"{field} {key[0]} {key[1]}: {old.get(key)} -> {new.get(key)}")
    for sc in sorted(set(before) & set(after)):
        for field in TOKEN_FIELDS:
            if before[sc].get(field) != after[sc].get(field):
                differences += 1
                print(f"{field} {sc}: {before[sc].get(field)} -> {after[sc].get(field)}")
    print(f"{differences} difference(s)")
    return 1 if differences else 0


def node(path, node_url, token_env):
    headers = {}
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            print(f"error: {token_env} is not set", file=sys.stderr)
            return 2
        headers["apitoken"] = token
    held = balances(load(path), "addresses")
    differences = 0
    for (sc, address), amount in sorted(held.items()):
        url = (
            f"{node_url.rstrip('/')}/vbtcapi/VBTC/GetVBTCBalance/"
            f"{urllib.parse.quote(address)}/{urllib.parse.quote(sc)}"
        )
        answer = get_json(url, headers)
        if isinstance(answer, str):
            answer = json.loads(answer)
        on_node = Decimal(str(answer["Balance"]))
        if on_node != amount:
            differences += 1
            print(f"{sc} {address}: Spyglass {amount}, node {on_node}")
    print(f"{len(held) - differences} match, {differences} differ")
    return 1 if differences else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("snapshot")
    take.add_argument("network", choices=sorted(API))
    take.add_argument("file")
    compare = commands.add_parser("diff")
    compare.add_argument("before")
    compare.add_argument("after")
    against = commands.add_parser("node")
    against.add_argument("file")
    against.add_argument("--node", required=True)
    against.add_argument("--token-env")
    args = parser.parse_args()
    if args.command == "snapshot":
        return snapshot(args.network, args.file)
    if args.command == "diff":
        return diff(args.before, args.after)
    return node(args.file, args.node, args.token_env)


if __name__ == "__main__":
    sys.exit(main())
