"""Агент-отельер и имитатор (core §7.2): вопрос → свободный ответ → поля → факт."""

from agents.hotelier.parse_answer import ParsedAnswer, parse_answer
from agents.hotelier.service import hotel_questions, placement_questions, submit_answer
from agents.hotelier.simulator import SIMULATED_NOTE, simulate_answer, simulate_placement

__all__ = [
    "SIMULATED_NOTE",
    "ParsedAnswer",
    "hotel_questions",
    "placement_questions",
    "parse_answer",
    "simulate_answer",
    "simulate_placement",
    "submit_answer",
]
