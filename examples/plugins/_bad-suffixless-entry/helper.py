"""Harmless decoy — a real .py file so the scan list is non-empty. All the malice is in `start`,
which the old suffix-filtered collector skipped. This file does nothing suspicious."""


def greet(name):
    return f"hello, {name}"
