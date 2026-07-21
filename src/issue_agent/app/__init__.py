"""Orchestration layer — thin observe→decide→execute→sleep loops.

Wires the pure decision functions (domain) to the adapters. Contains no decision
logic of its own beyond sequencing; everything testable lives in `domain`.
"""
