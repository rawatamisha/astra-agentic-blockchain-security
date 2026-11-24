"""Signed account transfers and an authority-produced simulation ledger."""

import hashlib
import json
from dataclasses import asdict, dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def public(key):
    return key.public_key().public_bytes_raw().hex()


def verify(key, signature, payload):
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
            bytes.fromhex(signature), encode(payload)
        )
        return True
    except (ValueError, InvalidSignature):
        return False


def valid_address(value):
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


@dataclass(frozen=True)
class Transaction:
    sender: str
    recipient: str
    amount: int
    nonce: int
    timestamp: int
    signature: str

    @classmethod
    def sign(cls, key, recipient, amount, nonce, timestamp):
        payload = dict(sender=public(key), recipient=recipient, amount=amount,
                       nonce=nonce, timestamp=timestamp)
        return cls(**payload, signature=key.sign(encode(payload)).hex())

    def valid(self):
        payload = asdict(self)
        signature = payload.pop("signature")
        return (type(self.amount) is int and self.amount > 0
                and type(self.nonce) is int and self.nonce >= 0
                and type(self.timestamp) is int and self.timestamp >= 0
                and valid_address(self.sender) and valid_address(self.recipient)
                and self.sender != self.recipient
                and verify(self.sender, signature, payload))


@dataclass(frozen=True)
class Block:
    height: int
    previous: str
    timestamp: int
    transactions: tuple[Transaction, ...]
    hash: str

    @classmethod
    def create(cls, height, previous, timestamp, transactions=()):
        transactions = tuple(transactions)
        payload = dict(height=height, previous=previous, timestamp=timestamp,
                       transactions=[asdict(tx) for tx in transactions])
        return cls(height, previous, timestamp, transactions, digest(payload))


class Chain:
    def __init__(self, allocations):
        if any(not valid_address(address) or type(amount) is not int or amount < 0
               for address, amount in allocations.items()):
            raise ValueError("Invalid genesis allocation")
        self.allocations = dict(allocations)
        self.genesis = Block.create(0, digest(self.allocations), 0)
        self.blocks = [self.genesis]

    def replay(self, blocks=None):
        blocks = self.blocks if blocks is None else blocks
        if not blocks or blocks[0] != self.genesis:
            raise ValueError("Genesis mismatch")
        balances = self.allocations.copy()
        nonces = {}
        for height, block in enumerate(blocks[1:], 1):
            previous = blocks[height - 1]
            expected = Block.create(height, previous.hash, block.timestamp, block.transactions)
            if block != expected or block.timestamp < previous.timestamp:
                raise ValueError("Invalid block link, hash or timestamp")
            for tx in block.transactions:
                if (not tx.valid() or tx.timestamp > block.timestamp
                        or tx.nonce != nonces.get(tx.sender, 0)
                        or balances.get(tx.sender, 0) < tx.amount):
                    raise ValueError("Invalid transaction, nonce or balance")
                balances[tx.sender] -= tx.amount
                balances[tx.recipient] = balances.get(tx.recipient, 0) + tx.amount
                nonces[tx.sender] = tx.nonce + 1
        return balances

    def append(self, transactions, timestamp):
        block = Block.create(len(self.blocks), self.blocks[-1].hash, timestamp, transactions)
        self.receive(block)
        return block

    def receive(self, block):
        self.replay([*self.blocks, block])
        self.blocks.append(block)


def identity(wallet=None):
    node_key = Ed25519PrivateKey.generate()
    if wallet is None:
        wallet = Ed25519PrivateKey.generate()
    payload = {"node_key": public(node_key), "wallet": public(wallet)}
    return {**payload, "node_id": digest(payload["node_key"]),
            "signature": node_key.sign(encode(payload)).hex()}
